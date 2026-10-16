"""
IBWorkerThread – one isolated QThread per TWS / Gateway connection.

Runs its own asyncio event-loop containing two concurrent coroutines:
  1. **Auto-reconnect loop** – checks ``ib.isConnected()`` every 1 s,
     attempts ``connectAsync`` every 5 s when disconnected, and subscribes
     to ``reqAccountSummary`` on (re-)connection.
  2. **Throttler loop** – every 250 ms, dumps a de-duplicated buffer of
     account-summary ticks, stamps each tuple with the connection label,
     and emits the batch via a Qt Signal.
"""
from __future__ import annotations
from src.utils.logger import tws_log, order_log

import asyncio
import copy
import logging
import math
import random
import string
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from src.core.enums import OrderAction, OrderType
from PySide6.QtCore import QThread, Signal

_ET = ZoneInfo("America/New_York")


def _to_eastern(dt: datetime | None) -> datetime:
    """Convert a datetime to US/Eastern.  If *dt* is None, return now(ET)."""
    if dt is None:
        return datetime.now(_ET)
    if dt.tzinfo is None:
        # naive → assume UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ET)


def _generate_order_ref() -> str:
    """Generate a custom orderRef: ``nano_{YYMMDD}_{HHMMSS}_{4 random lowercase letters}``.

    The date carries the year so ``_parse_orderref_time`` doesn't have to
    assume the current one — an order placed on Dec 31 and viewed on Jan 1
    used to come back dated a year late.
    """
    now = datetime.now(_ET)
    tag = "".join(random.choices(string.ascii_lowercase, k=4))
    return f"nano_{now:%y%m%d}_{now:%H%M%S}_{tag}"


# Final order states: an overnight parent in one of these owes its
# deferred exits (for whatever filled) or, unfilled, none.
_PARENT_DONE = ("Filled", "Completed", "Cancelled", "ApiCancelled",
                "Inactive")


def stk_routes_overnight(currency: str = "USD",
                         now: datetime | None = None) -> bool:
    """True when a stock order placed now goes to IBEOS (IBKR Overnight,
    20:00–03:59 ET).  IBEOS is US-only, so non-USD stocks never do.
    IBEOS takes limit orders only — a MKT order there is refused (387)."""
    if (currency or "USD").upper() != "USD":
        return False
    hour = (now or datetime.now(_ET)).astimezone(_ET).hour
    return hour >= 20 or hour < 4


def _round_to_tick(price: float, tick: float) -> float:
    """Snap *price* to the nearest multiple of *tick*.

    Used when deriving bracket-child limit/stop prices from a parent-fill
    reference price and a user-supplied offset — IBKR rejects child prices
    that aren't aligned with the contract's minimum tick.
    """
    if not tick or tick <= 0:
        return round(price, 4)
    return round(round(price / tick) * tick, 10)


def _parse_orderref_time(order_ref: str) -> datetime | None:
    """Extract the submission timestamp embedded in a NanoPulse orderRef.

    Format: ``nano_{YYMMDD}_{HHMMSS}_{4-letter tag}``
    Example: ``nano_260401_103045_abcd`` → 10:30:45 ET on April 1st 2026.

    The legacy ``MMDD`` form is still accepted — refs live on in open
    orders and in the fills snapshot IB replays after a reconnect, so a
    build that only understood the new form would lose their submission
    times.  Those fall back to the current year, which is what they
    always did.

    Returns ``None`` if the format does not match.
    """
    try:
        parts = order_ref.split("_")
        # Expected: ["nano", "YYMMDD" (or legacy "MMDD"), "HHMMSS", "xxxx"]
        if len(parts) != 4 or parts[0] != "nano":
            return None
        date_s, hhmmss = parts[1], parts[2]
        if len(hhmmss) != 6:
            return None
        if len(date_s) == 6:
            year = 2000 + int(date_s[:2])
            month = int(date_s[2:4])
            day = int(date_s[4:])
        elif len(date_s) == 4:                     # legacy MMDD
            year = datetime.now(_ET).year
            month = int(date_s[:2])
            day = int(date_s[2:])
        else:
            return None
        hour = int(hhmmss[:2])
        minute = int(hhmmss[2:4])
        second = int(hhmmss[4:])
        return datetime(year, month, day, hour, minute, second, tzinfo=_ET)
    except Exception:
        return None


def _parse_ib_time(s: str) -> datetime | None:
    """Parse a TWS time string (e.g. ``"20260327-14:30:45"``,
    ``"20260327  14:30:45"``) into an Eastern datetime.
    Returns ``None`` on failure.
    """
    if not s:
        return None
    try:
        # Normalise the various IB separator styles
        cleaned = s.replace("  ", " ").replace("-", " ").strip()
        # Try "yyyymmdd HH:MM:SS" first
        for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d %H:%M:%S.%f"):
            try:
                dt = datetime.strptime(cleaned, fmt)
                # TWS usually returns exchange-local time (US/Eastern for US products)
                return dt.replace(tzinfo=_ET)
            except ValueError:
                continue
    except Exception:
        pass
    return None


# IB uses DBL_MAX (≈1.7976931348623157e+308) as a sentinel for "no data".
# This is a valid finite float, so math.isnan / math.isinf won't catch it.
_IB_DBL_MAX = 1.7e308

# Top-of-book refusals for a background quote stream: 10089 "requires
# additional subscription for API", 10168 "not subscribed, delayed not
# enabled", 354 "not subscribed".  Seen when TWS drops a free venue from
# the account's permissions at a session change (IBEOS after the
# overnight session) and routes the quote to an unsubscribed exchange.
_L1_REFUSED_CODES = frozenset({354, 10089, 10168})
# Re-subscribe a refused quote stream after 30 s, doubling to a 5 min cap.
_L1_RETRY_BASE_S = 30.0
_L1_RETRY_MAX_S = 300.0
# Price tick types that prove a quote stream is live again: bid, ask,
# last, plus their delayed twins (66 / 67 / 68).
_L1_PRICE_TICK_TYPES = frozenset({1, 2, 4, 66, 67, 68})


def _safe_pnl(val) -> float:
    """Return *val* as a float, or 0.0 if it is None, NaN, or IB's DBL_MAX sentinel."""
    if val is None:
        return 0.0
    v = float(val)
    if math.isnan(v) or abs(v) >= _IB_DBL_MAX:
        return 0.0
    return v


def _parse_multiplier(raw) -> float:
    """IB returns ``contract.multiplier`` as a string (e.g. ``"20"`` for
    NQ, ``"2"`` for MNQ) — or empty for stocks.  Return a float, with
    1.0 as the fallback so per-point notionals round-trip for stocks."""
    if raw is None or raw == "":
        return 1.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return v if v > 0 else 1.0


try:
    from ib_async import IB, AccountValue
except ImportError:  # allow UI dev without TWS dependency installed
    IB = None  # type: ignore[assignment,misc]
    AccountValue = None  # type: ignore[assignment,misc]


log = logging.getLogger(__name__)


def _symbol_matches(contract, query_upper: str) -> bool:
    """Return True if *contract*'s symbol or localSymbol matches *query_upper*.

    The user may pass a root symbol (``"MNQ"``) or a localSymbol
    (``"MNQM6"``).  IB positions/trades always carry *both* fields, so we
    check both.  *query_upper* must already be upper-cased.
    """
    local = (getattr(contract, "localSymbol", "") or "").upper()
    root = (getattr(contract, "symbol", "") or "").upper()
    return query_upper in (local, root)


def _contract_to_dict(contract) -> dict:
    """Snapshot an ib_async ``Contract`` into a saved-contract-shaped dict.

    Used by flatten / reverse / close-pct so the UI can route the offsetting
    order against the *position's actual contract* — critical when the user
    has rolled their saved contract row to the next expiration (e.g. NQU6)
    but still holds an open position on the previous one (e.g. NQM6).  The
    saved-contract lookup would miss in that case; this preserves the
    in-flight contract identity end-to-end.

    ``exchange`` requires a fallback because ``ib.positions()`` returns
    Contract objects with an empty exchange field — placeOrder then fails
    with Warning 321 "Missing order exchange".  We pull the exchange (and
    other routing fields that share the same gap) from the in-memory
    contracts cache, matched by root symbol since exchange is constant
    across expirations of the same root.
    """
    d = {
        "conid":        int(getattr(contract, "conId", 0) or 0),
        "symbol":       getattr(contract, "symbol", "") or "",
        "sec_type":     getattr(contract, "secType", "") or "",
        "exchange":     (getattr(contract, "exchange", "")
                         or getattr(contract, "primaryExchange", "")
                         or ""),
        "currency":     getattr(contract, "currency", "") or "",
        "local_symbol": getattr(contract, "localSymbol", "") or "",
        "expiry":       getattr(contract, "lastTradeDateOrContractMonth", "") or "",
        "multiplier":   getattr(contract, "multiplier", "") or "",
    }
    if not d["exchange"]:
        try:
            from src.core import instruments
            # Try the root symbol first (what the chip entries are
            # keyed by), then fall back to the local symbol.
            cached = (instruments.get_contract(d["symbol"])
                      or instruments.get_contract(d["local_symbol"]))
            if cached:
                if not d["exchange"] and cached.get("exchange"):
                    d["exchange"] = cached["exchange"]
                if not d["currency"] and cached.get("currency"):
                    d["currency"] = cached["currency"]
                if not d["multiplier"] and cached.get("multiplier"):
                    d["multiplier"] = cached["multiplier"]
        except Exception:
            log.debug("_contract_to_dict: ReferenceData fallback failed",
                      exc_info=True)
    return d


def _matching_symbols_to_dicts(descriptions) -> list[dict]:
    """Convert reqMatchingSymbols ContractDescription objects to plain
    dicts safe to ship across the thread boundary.  ``name`` (company /
    fund name) is only populated by recent TWS builds, hence getattr."""
    results: list[dict] = []
    for desc in descriptions or []:
        c = getattr(desc, "contract", None)
        if c is None or not getattr(c, "symbol", ""):
            continue
        deriv = list(getattr(desc, "derivativeSecTypes", None) or [])
        name = getattr(c, "description", "") or ""
        results.append({
            "conid": getattr(c, "conId", 0),
            "symbol": c.symbol,
            "sec_type": getattr(c, "secType", ""),
            "primary_exchange": getattr(c, "primaryExchange", ""),
            "currency": getattr(c, "currency", ""),
            "name": name,
            # Aliases consumed by the options-income ticket search.
            "description": name,
            "has_options": "OPT" in [s.upper() for s in deriv],
            "derivative_sec_types": deriv,
        })
    return results


class IBWorkerThread(QThread):
    """One thread ↔ one TWS/Gateway connection."""

    # Signals funnelled to the main-thread model
    # list[tuple[str, str, str, str, str]]
    summary_batch_updated = Signal(list)
    connection_status_changed = Signal(str, bool)  # (label, is_connected)
    # list[dict] from reqContractDetails
    browse_results = Signal(list)
    browse_failed = Signal(str)                    # error message
    # (token, list[dict]) from reqMatchingSymbols — token lets the UI
    # discard responses that arrive after the user has kept typing.
    symbol_search_results = Signal(int, list)
    symbol_search_failed = Signal(int, str)        # (token, error message)
    # ── Options-income ticket plumbing.  All tokened: the token is echoed
    # back so the ticket can drop responses that are stale after the user
    # switched symbol / expiry / strategy.
    # (token, chain dict) from reqSecDefOptParams
    option_chain_results = Signal(int, dict)
    option_chain_failed = Signal(int, str)
    # (token, list[contract dict]) from reqContractDetails — every listed
    # strike for one (symbol, expiry, right)
    option_contracts_results = Signal(int, list)
    option_contracts_failed = Signal(int, str)
    # (token, conid, {bid, ask, last, close, delta}) per subscribed option
    # (the underlying rides along with delta=0)
    option_quote_updated = Signal(int, int, dict)
    option_quotes_failed = Signal(int, str)
    # Result of place_option_order's whatIf pre-check + transmit:
    # {ok, account, symbol, error?, init_margin?, maint_margin?}
    option_order_result = Signal(dict)
    price_updated = Signal(str, float)             # (symbol, last_price)
    bid_ask_updated = Signal(str, float, float)    # (symbol, bid, ask)
    # (symbol) – e.g. error 10197
    market_data_failed = Signal(str)
    # (symbol, available) – TWS refused a background quote stream (False),
    # or it ticks again after a refusal (True)
    quote_status_changed = Signal(str, bool)
    # order status dict → main thread
    order_status_updated = Signal(dict)
    # list[dict] – portfolio position batch
    portfolio_updated = Signal(list)
    # (symbol, last, bid, ask, close) – background cache.  ``close`` is the
    # prior-session close used by the order-entry ticker strip to compute
    # day change / %change; 0.0 when unavailable.
    bg_price_updated = Signal(str, float, float, float, float)
    # (symbol, bid_size, ask_size, last_size) – top-of-book sizes emitted
    # in lockstep with ``bg_price_updated``.  Surfaced for the Book Trader
    # L1-fallback ladder (single-level book when the account has no L2
    # entitlement) and the TWS-style "{size} @ {price}" last-trade stripe.
    # Zero when TWS has no value (NaN / DBL_MAX sentinels are scrubbed).
    bg_sizes_updated = Signal(str, float, float, float)
    # (symbol, bids, asks) – depth-of-market snapshot for the Book Trader.
    # bids / asks are list[tuple[price: float, size: float]], freshly
    # copied out of the ib_async Ticker so the UI thread doesn't see the
    # in-place mutation that ib_async performs on every L2 tick.  Emitted
    # by the 100 ms ``_depth_flush_loop``.
    depth_updated = Signal(str, list, list)
    # {symbol, account, action, qty} → UI places the close order
    flatten_close_needed = Signal(dict)
    # {symbol, account, action, qty} → UI places the reverse order
    reverse_close_needed = Signal(dict)
    # per-position P&L from pnlSingleEvent
    # (account, con_id, daily_pnl, realized_pnl, unrealized_pnl)
    pnl_single_updated = Signal(str, int, float, float, float)
    # Batched form: [(account, con_id, daily, realized, unrealized), …] —
    # one QMetaCallEvent per 250 ms flush instead of one per position.
    pnl_single_batch_updated = Signal(list)
    # account-level P&L from pnlEvent
    # (account, daily_pnl, realized_pnl, unrealized_pnl)
    account_pnl_updated = Signal(str, float, float, float)
    # (label, list_of_account_ids) – emitted once after connection established
    accounts_detected = Signal(str, list)
    # Flattened Fill dict – one emission per execution (startup + live).
    # Keyed by fill.execution.execId so the UI can dedupe across reconnects.
    fill_updated = Signal(dict)
    # One-line user notice (status bar) — e.g. overnight exits placed on
    # reconnect for a parent that filled while NanoPulse was offline.
    exits_notice = Signal(str)

    def __init__(
        self,
        connection_label: str,
        host: str = "127.1.1.1",
        port: int = 1000,
        client_id: int = 100,
        auto_reconnect: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._label = connection_label
        self._host = host
        self._port = port
        self._client_id = client_id
        self._auto_reconnect = auto_reconnect
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ib: IB | None = None
        self._main_task: asyncio.Task | None = None
        self._first_connect_attempted = False

        # De-dupe buffer keyed by (account, tag, currency) → (value, currency).
        # Currency is part of the key so the per-currency ledger rows from the
        # ``$LEDGER:ALL`` summary tag (CashBalance in USD / JPY / CAD / …) are
        # preserved instead of collapsing onto a single (account, tag) slot.
        self._buffer: dict[tuple[str, str, str], tuple[str, str]] = {}

        # Portfolio buffer keyed by (account, conId) → position dict
        self._portfolio_buffer: dict[tuple[str, int], dict] = {}

        # Persistent portfolio cache – keeps ALL last-known position dicts
        # so late-connecting UI consumers can get an instant snapshot.
        self._portfolio_cache: dict[tuple[str, int], dict] = {}

        # Background market data subscription state
        self._active_ticker = None
        self._active_price_symbol: str = ""

        # Options-income ticket quote subscriptions (one batch at a time):
        # conid → Ticker, plus an id(ticker) → conid reverse index for the
        # O(1) pendingTickers hot path.  Separate from _active_ticker /
        # _bg_tickers so the ticket never disturbs order-entry data.
        self._opt_tickers: dict[int, object] = {}
        self._ticker_to_opt_conid: dict[int, int] = {}
        self._opt_quote_token: int = 0
        # Last batch params so the ticket can be resubscribed after an
        # entitlement error, and a sticky flag set once IB reports the
        # account has no live subscription (error 354) — from then on the
        # ticket subscribes delayed-frozen (type 4) instead of frozen (2).
        self._opt_quote_contracts: list[dict] = []
        self._opt_delayed_fallback: bool = False

        # Background market data cache: multiple simultaneous subscriptions
        self._bg_tickers: dict[str, object] = {}  # symbol → ib_async Ticker

        # Last valid bid/ask per background symbol so "last-price-only" ticks
        # (common with IB delayed data for futures) don't blank out the display.
        self._bg_last_bid_ask: dict[str, tuple[float, float]] = {}  # sym → (bid, ask)

        # Depth-of-market (L2) subscription state for the Book Trader.
        #   _depth_tickers       : symbol → ib_async Ticker returned by reqMktDepth
        #   _ticker_to_depth_sym : id(ticker) → symbol reverse index used by
        #                          _on_pending_tickers to O(1)-mark dirty
        #   _depth_dirty         : symbols whose domBids/domAsks have changed
        #                          since the last _depth_flush_loop tick
        self._depth_tickers: dict[str, object] = {}
        self._ticker_to_depth_sym: dict[int, str] = {}
        #   _depth_smart         : symbol → isSmartDepth flag used on reqMktDepth
        #                          (cancelMktDepth must be told the same flag)
        self._depth_smart: dict[str, bool] = {}
        self._depth_dirty: set[str] = set()
        # L1 price coalescing (same 100 ms cadence as depth): raw ticks
        # mark these dirty; ``_depth_flush_loop`` emits at most 10×/s per
        # symbol.  Subscribe paths still emit directly for instant first
        # paint.
        self._active_price_dirty: bool = False
        self._bg_price_dirty: dict[str, object] = {}
        # Background symbols whose quote stream TWS refused (see
        # ``_L1_REFUSED_CODES``): sym → (retries so far, monotonic time of
        # the next re-subscribe).  Retried from the throttler loop.
        self._l1_refused: dict[str, tuple[int, float]] = {}

        # Order tracking: order_id → Trade
        self._trades: dict[str, object] = {}

        # order_ids that have a live per-trade _on_status closure
        # (placed by _do_place_order in the current session).
        # Snapshot-registered trades are in _trades but NOT here.
        self._has_status_handler: set[str] = set()

        # Rejection/error messages keyed by orderRef (nano_...).
        # NOT cleared on reconnect — survives self._trades.clear() cycles
        # so rejection reasons persist through reconnect + refresh.
        self._order_error_messages: dict[str, str] = {}

        # Full order-status dicts for rejected orders, keyed by order_id.
        # TWS does NOT return rejected (Inactive) orders via reqAllOpenOrders
        # or reqCompletedOrders after a short time, so we maintain our own
        # persistent cache and re-emit them on every snapshot/refresh.
        # NOT cleared on reconnect.
        self._rejected_order_cache: dict[str, dict] = {}

        # Latest emitted status dict per order_id.  Lets late subscribers
        # (e.g. BookTraderView, which wires order_status_updated only when
        # the Flash tab first refreshes) replay orders whose snapshot
        # burst fired before they connected — a quietly-working open
        # order never emits another status event on its own.  Updated on
        # the main thread via the queued self-connection below, so
        # readers on the GUI thread never race the worker loop.
        self._order_status_cache: dict[str, dict] = {}
        self.order_status_updated.connect(self._cache_order_status)

        # First valid timestamp observed per orderRef (nano_...), ISO format.
        # Cancelled orders have no completedTime and no fills, so their
        # timestamp is lost after a reconnect.  This cache lets _build_trade_dict
        # and _emit_order_status restore the real time on every subsequent
        # refresh instead of falling back to 00:00:00.
        # NOT cleared on reconnect.
        self._order_times: dict[str, str] = {}

        # Display symbol per orderRef (nano_...).  Option orders are placed
        # on a lean conId-only contract (TWS error 478 workaround), so the
        # Trade kept by ib_async carries no symbol/localSymbol —
        # _build_trade_dict (reject / snapshot / global-status paths) would
        # otherwise emit an empty symbol and blank out the blotter row.
        self._order_symbols: dict[str, str] = {}

        # Fills cache: execId → flattened fill dict.
        # Populated by _on_exec_details and _snapshot_fills.  Lets the UI
        # replay today's fills when the Trades view wires its signals
        # after the worker has already run its initial snapshot.
        self._fills_cache: dict[str, dict] = {}

        # Post-snapshot commission back-fill (see _sweep_late_commissions).
        # Tracked so a re-snapshot restarts the sweep instead of racing it.
        self._commission_sweep_task: asyncio.Task | None = None

        # Contract metadata per conId, captured at order-placement time.
        # The fill-side twin of _order_symbols: ib_async attaches
        # trade.contract to every live fill for an order it placed, and
        # for options that contract is the lean conId-only one — so
        # _flatten_fill would emit fills with a blank symbol/secType and
        # a 1.0 multiplier (the Trades view then shows "—", no Type, and
        # option notionals/P&L understated 100×).  _flatten_fill backfills
        # blank contract fields from here instead.
        self._conid_contract_meta: dict[int, dict] = {}

        # Track per-position PnL subscriptions: set of (account, conId)
        self._pnl_single_subs: set[tuple[str, int]] = set()

        # Last-known pnlSingle values per (account, conId).
        # Persists across reconnects so the UI can re-snapshot missed events.
        self._pnl_single_cache: dict[tuple[str, int], tuple[float, float, float]] = {}

        # RC #1: throttle buffer — stores the LATEST pnl_single values keyed by
        # (account, conId).  Flushed by the throttler every 250 ms so the main
        # thread receives at most 4 batch emissions per second instead of one
        # QMetaCallEvent per raw pnlSingleEvent tick.
        self._pnl_single_buffer: dict[tuple[str, int], tuple[float, float, float]] = {}

        # RC #3: deferred cancellation queue for flat positions.
        # When a position goes to zero we keep reqPnLSingle active for 60 s so
        # the realized P&L is still visible in the UI, then cancel it.
        # key → monotonic deadline (time.monotonic() + 60)
        self._pnl_zero_pending_cancel: dict[tuple[str, int], float] = {}

        # RC #6: reverse index for O(1) background-ticker lookup.
        # Replaces the O(n) scan over _bg_tickers on every pendingTickersEvent.
        self._ticker_to_sym: dict[int, str] = {}  # id(ticker) → symbol

        # RC #5: counter for periodic stale-data pruning in the throttler loop
        # (every 300 ticks × 250 ms ≈ 75 s).
        self._prune_ticker: int = 0

    def set_auto_reconnect(self, auto_reconnect: bool) -> None:
        """Update the auto-reconnect flag natively while running."""
        self._auto_reconnect = auto_reconnect

    @property
    def is_ib_connected(self) -> bool:
        """Thread-safe check: is the underlying IB instance connected?"""
        return self._ib is not None and self._ib.isConnected()

    # ── Public control ────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Request an immediate graceful shutdown.

        Sets the running flag to False and cancels the main asyncio task so
        any sleeping coroutines (reconnect / throttler) are interrupted
        instantly rather than waiting for their next sleep to expire.
        """
        log.debug("Worker %s: stop() requested", self._label)
        self._running = False
        if self._loop is not None and not self._loop.is_closed() and self._main_task is not None:
            self._loop.call_soon_threadsafe(self._main_task.cancel)

    def request_order_snapshot(self) -> None:
        """Thread-safe: re-emit all known open + completed orders.

        Called by the UI when a signal consumer is connected *after* the
        initial snapshot has already fired (e.g. user navigates to Flash tab
        after worker was already connected on the Conn tab).
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._snapshot_existing_orders(), self._loop)
        else:
            log.debug(
                "Worker %s: request_order_snapshot but loop not ready", self._label)

    def request_portfolio_snapshot(self) -> None:
        """Thread-safe: re-emit all cached portfolio positions.

        Called by the UI when the portfolio_updated signal is connected
        *after* the initial positions were already emitted.
        """
        if self._portfolio_cache:
            batch = list(self._portfolio_cache.values())
            self.portfolio_updated.emit(batch)
            log.debug("Worker %s: portfolio snapshot emitted (%d positions)",
                      self._label, len(batch))
        else:
            log.debug("Worker %s: request_portfolio_snapshot but cache is empty",
                      self._label)

    def request_pnl_snapshot(self) -> None:
        """Thread-safe: re-emit all cached pnlSingle values.

        Called by the UI when pnl_single_updated is connected *after* the
        initial pnlSingleEvents were already fired — prevents the
        'missed first event' race where one worker's pnlSingle fires before
        the slot is wired up.
        """
        if self._pnl_single_cache:
            for (acct, con_id), (daily, realized, unrealized) in \
                    list(self._pnl_single_cache.items()):
                self.pnl_single_updated.emit(
                    acct, con_id, daily, realized, unrealized)
            log.debug("Worker %s: pnl snapshot emitted (%d entries)",
                      self._label, len(self._pnl_single_cache))
        else:
            log.debug("Worker %s: request_pnl_snapshot but cache is empty",
                      self._label)

    def request_fills_snapshot(self) -> None:
        """Thread-safe: re-emit every cached fill through ``fill_updated``.

        Called by the Trades view when it is wired after the worker's
        initial fills snapshot has already fired (e.g. user switches to
        the Trades tab after the app has been up for a while).  Reads
        from ``_fills_cache`` — no IB API call is made.
        """
        if not self._fills_cache:
            log.debug("Worker %s: request_fills_snapshot but cache empty",
                      self._label)
            return
        # Sort chronologically so the model's head-insert logic ends up
        # with the newest fill on top.
        items = sorted(
            self._fills_cache.values(),
            key=lambda f: f.get("time_sort", ""),
        )
        for d in items:
            self.fill_updated.emit(d)
        log.debug("Worker %s: fills snapshot emitted %d fill(s) from cache",
                  self._label, len(items))

    def request_portfolio_refresh(self) -> None:
        """Thread-safe: re-subscribe to account updates so IB re-pushes all positions.

        This forces IB to fire ``updatePortfolioEvent`` for every open
        position.  Useful after an order fill when the live event was
        missed or delayed.
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_portfolio_refresh(), self._loop)
        else:
            log.debug(
                "Worker %s: request_portfolio_refresh but loop not ready", self._label)

    async def _do_portfolio_refresh(self) -> None:
        """Request all positions across all accounts simultaneously.

        Uses reqPositionsAsync() which covers every managed account in a
        single call.  After the call completes, builds a definitive batch
        directly from ``ib.positions()`` and emits it immediately via
        ``portfolio_updated``.  This bypasses the
        ``positionEvent → buffer → throttler`` path which can be unreliable
        for multi-account connections where TWS may not stream individual
        position changes after the initial snapshot.

        Also emits zero-qty updates for any cached position that has gone
        flat (IB only returns non-zero positions from reqPositions).
        """
        if self._ib is None or not self._ib.isConnected():
            return
        await self._ib.reqPositionsAsync()
        tws_log.info("[%s] API-CALL  reqPositionsAsync() [refresh, all accounts]",
                     self._label)

        # ── Build a definitive batch from the authoritative IB positions ──
        # Reading directly from ib.positions() guarantees we pick up every
        # qty change, regardless of whether positionEvent fired individually.
        batch: list[dict] = []
        current_keys: set[tuple[str, int]] = set()
        try:
            for pos in self._ib.positions():
                contract = pos.contract
                acct = pos.account
                con_id = getattr(contract, "conId", 0)
                if not acct or not con_id:
                    continue
                current_keys.add((acct, con_id))
                entry = {
                    "label":        self._label,
                    "account":      acct,
                    "con_id":       con_id,
                    "symbol":       getattr(contract, "symbol", ""),
                    "local_symbol": getattr(contract, "localSymbol", ""),
                    "sec_type":     getattr(contract, "secType", ""),
                    "exchange":     getattr(contract, "exchange", ""),
                    "currency":     getattr(contract, "currency", "USD"),
                    "multiplier":   getattr(contract, "multiplier", ""),
                    "position":     float(pos.position),
                    "avg_cost":     float(pos.avgCost),
                    # market_price / market_value / pnl come from pnlSingleEvent;
                    # preserve any cached values so we don't zero out live data.
                    "market_price":    self._portfolio_cache.get(
                        (acct, con_id), {}).get("market_price", 0.0),
                    "market_value":    self._portfolio_cache.get(
                        (acct, con_id), {}).get("market_value", 0.0),
                    "unrealized_pnl":  self._portfolio_cache.get(
                        (acct, con_id), {}).get("unrealized_pnl", 0.0),
                    "realized_pnl":    self._portfolio_cache.get(
                        (acct, con_id), {}).get("realized_pnl", 0.0),
                }
                self._portfolio_cache[(acct, con_id)] = entry
                batch.append(entry)
        except Exception:
            log.debug("Worker %s: portfolio refresh – could not read ib.positions()",
                      self._label)

        # Any entry in our local cache that is NOT in current_keys has gone
        # flat (position closed).  Emit a zero-qty update so the UI model
        # removes or zeros the row.
        zeroed = 0
        for key, cached in list(self._portfolio_cache.items()):
            if key not in current_keys:
                # Only the flat *transition* emits (and logs): entries
                # already at zero were announced on an earlier refresh,
                # and re-emitting every historically-flat position made
                # the batch (and this log) grow monotonically all day.
                if cached.get("position", 0.0) == 0.0:
                    continue
                zero = dict(cached)
                zero["position"]     = 0.0
                zero["market_value"] = 0.0
                zero["market_price"] = 0.0
                self._portfolio_cache[key] = zero
                batch.append(zero)
                zeroed += 1
                tws_log.info("[%s] POSITION-ZERO  account=%s conId=%s "
                             "(no longer in IB positions – emitting qty=0)",
                             self._label, key[0], key[1])

        # Emit the definitive batch directly, bypassing the throttler.
        # This guarantees the UI gets the latest position data immediately
        # even when positionEvent streaming is unreliable (multi-account).
        if batch:
            self._portfolio_buffer.clear()  # discard any stale buffered data
            self.portfolio_updated.emit(batch)
            tws_log.debug("[%s] PORTFOLIO-REFRESH  emitted %d positions "
                          "(%d zeroed) directly",
                          self._label, len(batch), zeroed)

        log.debug("Worker %s: portfolio refresh complete (%d account(s), "
                  "%d positions, %d zeroed)",
                  self._label, len(self._ib.managedAccounts()),
                  len(batch) - zeroed, zeroed)

    def request_browse(self, params: dict) -> None:
        """Thread-safe: browse all contracts matching params via reqContractDetails."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_browse(params), self._loop)
        else:
            log.warning(
                "Worker %s: request_browse but loop not ready", self._label)
            self.browse_failed.emit("Worker event loop not ready.")

    def request_symbol_search(self, token: int, pattern: str) -> None:
        """Thread-safe: fuzzy symbol lookup via reqMatchingSymbols.

        *token* is echoed back on the result/failure signal so the UI
        can ignore responses that are stale by the time they arrive.
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_symbol_search(token, pattern), self._loop)
        else:
            log.warning(
                "Worker %s: request_symbol_search but loop not ready",
                self._label)
            self.symbol_search_failed.emit(
                token, "Worker event loop not ready.")

    def request_market_data(self, params: dict) -> None:
        """Thread-safe: subscribe to live market data for a contract.

        *params* must contain ``conid`` and optionally ``symbol``,
        ``exchange``, ``currency``.
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_market_data(params), self._loop)
        else:
            log.warning(
                "Worker %s: request_market_data but loop not ready", self._label)

    def cancel_market_data(self) -> None:
        """Thread-safe: cancel the current market data subscription."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_cancel_market_data(), self._loop)
        else:
            self._active_ticker = None

    # ── Options-income ticket requests (all tokened) ──────────────────────

    def request_option_chain(self, token: int, params: dict) -> None:
        """Thread-safe: expirations/strikes via reqSecDefOptParams.

        *params*: ``symbol`` (root), ``con_id`` (underlying conId),
        optional ``sec_type`` (default STK).
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_option_chain(token, params), self._loop)
        else:
            self.option_chain_failed.emit(
                token, "Worker event loop not ready.")

    def request_option_contracts(self, token: int, params: dict) -> None:
        """Thread-safe: resolve every listed strike for one
        (symbol, expiry, right) via reqContractDetails.

        *params*: symbol, expiry (YYYYMMDD), right ("C"/"P"),
        optional currency, trading_class.
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_option_contracts(token, params), self._loop)
        else:
            self.option_contracts_failed.emit(
                token, "Worker event loop not ready.")

    def request_option_quotes(self, token: int,
                              contracts: list[dict]) -> None:
        """Thread-safe: subscribe bid/ask/last(/delta) for a batch of
        contracts (the visible strikes + the underlying).  Replaces any
        previous batch."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_option_quotes(token, list(contracts)), self._loop)
        else:
            self.option_quotes_failed.emit(
                token, "Worker event loop not ready.")

    def cancel_option_quotes(self) -> None:
        """Thread-safe: drop the ticket's quote batch (dialog closed)."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_cancel_option_quotes(), self._loop)
        else:
            self._opt_tickers.clear()
            self._ticker_to_opt_conid.clear()

    def place_option_order(self, params: dict) -> None:
        """Thread-safe: whatIf margin pre-check, then place the order.

        Same *params* shape as :meth:`place_order`; emits
        ``option_order_result`` with the pre-check outcome.  The real
        transmit goes through the normal ``_do_place_order`` path so
        order-ref generation / status wiring stay identical.
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_place_option_order(params), self._loop)
        else:
            self.option_order_result.emit({
                "ok": False, "account": params.get("account", ""),
                "symbol": params.get("symbol", ""),
                "error": "Worker event loop not ready."})

    def place_order(self, params: dict) -> None:
        """Thread-safe: place an order on the worker's IB connection.

        *params* keys:
            conid, symbol, sec_type, exchange, currency, local_symbol,
            expiry, multiplier,
            account, action (BUY/SELL), qty, order_type (MKT/LMT),
            limit_price (for LMT), tif (DAY/GTC/…)
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_place_order(params), self._loop)
        else:
            log.warning(
                "Worker %s: place_order but loop not ready", self._label)

    def cancel_order(self, order_id: str) -> None:
        """Thread-safe: cancel an open order by *order_id*."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_cancel_order(order_id), self._loop)
        else:
            log.warning(
                "Worker %s: cancel_order but loop not ready", self._label)

    def cancel_all_for_symbol(self, symbol: str, accounts: set[str]) -> None:
        """Thread-safe: cancel all open orders for *symbol* whose account is in *accounts*."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_cancel_all_for_symbol(symbol, accounts), self._loop)
        else:
            log.warning(
                "Worker %s: cancel_all_for_symbol but loop not ready", self._label)

    def flatten(self, symbol: str, account: str) -> None:
        """Thread-safe: cancel all open orders for *symbol*/*account*, then
        close the position with an offsetting MKT order."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_flatten(symbol, account), self._loop)
        else:
            log.warning("Worker %s: flatten but loop not ready", self._label)

    def reverse(self, symbol: str, account: str) -> None:
        """Thread-safe: cancel all open orders for *symbol*/*account*, then
        flip the position (long→short or short→long) with a 2× MKT order."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_reverse(symbol, account), self._loop)
        else:
            log.warning("Worker %s: reverse but loop not ready", self._label)

    def close_tier(self, symbol: str, account: str, side: str,
                   offset_pts: float, requested_qty: int,
                   current_price: float, cap_offset: float = 0.0) -> None:
        """Thread-safe: close the current position on *symbol*/*account*
        using a single LMT (tp) or STP/STP LMT (sl) exit order.

        * side       : "tp" (Limit at profit price) or "sl" (Stop).
        * offset_pts : price-point distance from *current_price* for the
                       exit price.
        * requested_qty : user's configured qty; the order is sized to
                          min(|position|, requested_qty).
        * current_price : reference price (UI's last_price snapshot).
        * cap_offset  : if >0 and side=="sl", uses STP LMT with the limit
                        sitting cap_offset further into the loss zone than
                        the stop trigger.
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_close_tier(symbol, account, side, offset_pts,
                                    requested_qty, current_price,
                                    cap_offset), self._loop)
        else:
            log.warning(
                "Worker %s: close_tier but loop not ready", self._label)

    def close_pct(self, symbol: str, account: str, pct: int) -> None:
        """Thread-safe: close *pct* percent (25/50/75) of the position for
        *symbol*/*account* with an offsetting MKT order.  No open orders are
        cancelled — only the position is partially closed."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_close_pct(symbol, account, pct), self._loop)
        else:
            log.warning("Worker %s: close_pct but loop not ready", self._label)

    def cancel_all_for_account(self, account: str) -> None:
        """Thread-safe: cancel *every* open order for *account* regardless of
        symbol.  Used by the account-level Flatten so that resting orders with
        no matching position yet are also swept up."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_cancel_all_for_account(account), self._loop)
        else:
            log.warning("Worker %s: cancel_all_for_account but loop not ready",
                        self._label)

    # ── Background market data (multi-symbol cache) ───────────────────────────

    def subscribe_background_symbols(self, params_list: list[dict]) -> None:
        """Thread-safe: subscribe to background market data for multiple symbols.

        Each dict in *params_list* must have ``conid``, ``symbol``, and
        optionally ``sec_type``, ``exchange``, ``currency``, ``local_symbol``,
        ``expiry``, ``multiplier``.  Symbols already subscribed are skipped.
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_subscribe_bg_symbols(params_list), self._loop)
        else:
            log.warning(
                "Worker %s: subscribe_background_symbols but loop not ready", self._label)

    def unsubscribe_background_symbol(self, symbol: str) -> None:
        """Thread-safe: unsubscribe one background market-data stream."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_unsubscribe_bg_symbol(symbol), self._loop)

    def unsubscribe_all_background(self) -> None:
        """Thread-safe: unsubscribe all background market-data streams."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_unsubscribe_all_bg(), self._loop)

    # ── Depth of market (L2) subscriptions ────────────────────────────────────

    def subscribe_depth(self, symbol: str, num_rows: int = 10) -> None:
        """Thread-safe: subscribe to IBKR Level-2 depth for *symbol*.

        The Book Trader typically keeps exactly one depth subscription
        active at a time — the currently-active ticker — so TWS's
        ~3-concurrent-L2 limit without "Level 2 booster" isn't hit.
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_subscribe_depth(symbol, num_rows), self._loop)
        else:
            log.warning(
                "Worker %s: subscribe_depth but loop not ready", self._label)

    def unsubscribe_depth(self, symbol: str) -> None:
        """Thread-safe: unsubscribe a Level-2 depth stream."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_unsubscribe_depth(symbol), self._loop)
        else:
            log.warning(
                "Worker %s: unsubscribe_depth but loop not ready", self._label)

    def unsubscribe_all_depth(self) -> None:
        """Thread-safe: unsubscribe every live L2 stream."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_unsubscribe_all_depth(), self._loop)

    # ── Thread entry-point ────────────────────────────────────────────────────

    def run(self) -> None:  # noqa: D401 – Qt override
        if IB is None:
            log.warning(
                "ib_async not available – worker %s exiting.", self._label)
            return

        self._running = True
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        self._ib = IB()
        self._ib.accountSummaryEvent += self._on_account_summary
        # Per-account, per-currency account values (CashBalance in USD / JPY /
        # CAD / …).  accountSummary's $LEDGER tag is group-consolidated
        # (account="All"), so per-account cash has to come from the
        # reqAccountUpdatesMulti stream which fires accountValueEvent keyed by
        # the real account.
        self._ib.accountValueEvent += self._on_account_value
        self._ib.pnlEvent += self._on_pnl
        self._ib.pnlSingleEvent += self._on_pnl_single
        self._ib.errorEvent += self._on_ib_error
        self._ib.pendingTickersEvent += self._on_pending_tickers
        # Global order-status listener – catches ALL order updates
        # (including orders placed before a reconnect and TWS manual orders)
        self._ib.orderStatusEvent += self._on_global_order_status
        # positionEvent fires for ALL accounts simultaneously.
        # updatePortfolioEvent only supports one account at a time and
        # cancels the previous subscription on each new call — broken for
        # multi-account connections.
        self._ib.positionEvent += self._on_position_update
        # Live execution stream – feeds the Trades view.  commissionReport
        # arrives on a separate callback a few hundred ms after
        # execDetails / execDetailsEnd, so we subscribe to it too and
        # re-emit fill_updated with the populated commission.
        self._ib.execDetailsEvent += self._on_exec_details
        self._ib.commissionReportEvent += self._on_commission_report

        tws_log.info("[%s] IB instance created – event subscriptions: "
                     "accountSummaryEvent, pnlEvent, pnlSingleEvent, errorEvent, "
                     "pendingTickersEvent, orderStatusEvent, positionEvent, "
                     "execDetailsEvent, commissionReportEvent",
                     self._label)

        try:
            loop.run_until_complete(self._main_loop())
        except asyncio.CancelledError:
            log.debug("Worker %s: main loop cancelled (shutdown)", self._label)
        except Exception:
            log.exception("Worker %s crashed", self._label)
        finally:
            # Graceful disconnection
            if self._ib is not None:
                if self._ib.isConnected():
                    try:
                        self._ib.disconnect()
                        log.debug(
                            "Worker %s disconnected from IB", self._label)
                        tws_log.info(
                            "[%s] API-CALL  disconnect() → OK", self._label)
                    except Exception as e:
                        log.exception(
                            "Worker %s: error during IB disconnect: %s", self._label, e)
                        tws_log.error(
                            "[%s] API-ERR   disconnect() failed: %s", self._label, e)

            self._main_task = None
            self._loop = None
            loop.close()
            log.info("Worker %s: thread shutdown complete", self._label)
            tws_log.info(
                "[%s] SHUTDOWN  thread shutdown complete", self._label)
            self.connection_status_changed.emit(self._label, False)

    # ── Async core ────────────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        self._main_task = asyncio.current_task()
        results = await asyncio.gather(
            self._reconnect_loop(),
            self._throttler_loop(),
            self._depth_flush_loop(),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                coro_name = ("_reconnect_loop", "_throttler_loop",
                             "_depth_flush_loop")[i]
                log.exception("Worker %s: %s crashed: %s",
                              self._label, coro_name, result,
                              exc_info=result)

    async def _reconnect_loop(self) -> None:
        """Check connectivity every 1 s; reconnect every 5 s when down."""
        reconnect_countdown = 0.0
        while self._running:
            if self._ib.isConnected():
                reconnect_countdown = 0.0
                await asyncio.sleep(1)
                continue

            if reconnect_countdown <= 0:
                if self._first_connect_attempted and not self._auto_reconnect:
                    log.info("Worker %s disconnected and auto-reconnect is OFF. Quitting.", self._label)
                    self._running = False
                    break
                    
                self._first_connect_attempted = True

                try:
                    log.info("Worker %s connecting to %s:%d (cid=%d) …",
                             self._label, self._host, self._port, self._client_id)
                    tws_log.info("[%s] API-CALL  connectAsync(host=%s, port=%d, clientId=%d)",
                                 self._label, self._host, self._port, self._client_id)
                    await self._ib.connectAsync(
                        self._host, self._port, clientId=self._client_id,
                    )
                except Exception as exc:
                    log.exception("Worker %s connect failed: %s",
                                  self._label, exc)
                    tws_log.error("[%s] API-ERR   connectAsync FAILED: %s",
                                  self._label, exc)
                    self.connection_status_changed.emit(self._label, False)
                    
                    if not self._auto_reconnect:
                        log.info("Worker %s: Auto-reconnect is OFF. Quitting.", self._label)
                        self._running = False
                        break

                    reconnect_countdown = 5.0
                    await asyncio.sleep(1)
                    reconnect_countdown -= 1.0
                    continue

                # Connection established — update status immediately
                self.connection_status_changed.emit(self._label, True)
                # Notify UI of discovered account IDs for auto-type detection
                managed = list(self._ib.managedAccounts())
                if managed:
                    self.accounts_detected.emit(self._label, managed)
                log.info(
                    "Worker %s connected – seeding initial snapshot", self._label)
                tws_log.info(
                    "[%s] API-RESP  connectAsync → CONNECTED", self._label)

                # Old Trade objects and their _on_status closures are invalid
                # after a disconnect — ib_async clears its internal collection.
                # Reset tracking so the global handler doesn't skip status
                # updates for orders restored from the upcoming snapshot.
                self._trades.clear()
                self._has_status_handler.clear()
                self._bg_tickers.clear()
                self._bg_last_bid_ask.clear()   # bid/ask cache stale after reconnect
                self._ticker_to_sym.clear()         # RC #6: clear reverse index
                self._opt_tickers.clear()           # options ticket subs dropped
                self._ticker_to_opt_conid.clear()
                # Depth-of-market state (BookTrader) — IB drops L2 subs on
                # disconnect so the local state becomes stale.  UI will
                # re-subscribe on the next active-symbol change.
                self._depth_tickers.clear()
                self._ticker_to_depth_sym.clear()
                self._depth_smart.clear()
                self._depth_dirty.clear()
                # Pending coalesced L1 emits reference dead tickers.
                self._active_price_dirty = False
                self._bg_price_dirty.clear()
                self._clear_l1_refusals()
                # Clear pnlSingle subscriptions so they are re-established
                # after reconnect (IB drops all subscriptions on disconnect).
                self._pnl_single_subs.clear()
                self._pnl_single_buffer.clear()        # RC #1: discard stale buffered data
                self._pnl_zero_pending_cancel.clear()  # RC #3: no pending cancels after reconnect

                # Post-connect setup: errors here should NOT reset the
                # connection status — the IB link is alive; only the
                # setup step failed and will be retried on next cycle.
                try:
                    # Forward manual TWS orders + surviving orders to this client
                    self._ib.reqAutoOpenOrders(True)
                    tws_log.info(
                        "[%s] API-CALL  reqAutoOpenOrders(True)", self._label)

                    # Seed the buffer from the snapshot that connectAsync
                    # already fetched during synchronisation.
                    tws_log.info(
                        "[%s] API-CALL  accountSummaryAsync()", self._label)
                    for v in await self._ib.accountSummaryAsync():
                        self._buffer[(v.account, v.tag, v.currency)] = (
                            v.value, v.currency)
                    log.info("Worker %s buffer seeded with %d tag(s)",
                             self._label, len(self._buffer))
                    tws_log.info("[%s] API-RESP  accountSummaryAsync → %d tag(s)",
                                 self._label, len(self._buffer))

                    # Per-account, per-currency cash via reqAccountUpdatesMulti,
                    # which streams CashBalance per currency through
                    # accountValueEvent — unlike $LEDGER:ALL in the account
                    # summary, which only reports the group total.  One request
                    # per account: the account="" wildcard is rejected on FA
                    # connections ("Group name cannot be null", error 321) and
                    # ib_async classifies that reject as a warning, leaving the
                    # returned future unresolved — awaiting it hung this whole
                    # setup block (reqPnL, positions, snapshots) forever.  The
                    # subscription is live as soon as the request is sent, so
                    # don't await the initial-download future at all; just
                    # swallow its result so a late error can't warn unretrieved.
                    for acct in self._ib.managedAccounts():
                        fut = self._ib.reqAccountUpdatesMultiAsync(acct, "")
                        fut.add_done_callback(
                            lambda f: f.cancelled() or f.exception())
                        tws_log.info(
                            "[%s] API-CALL  reqAccountUpdatesMulti(account=%s)",
                            self._label, acct)

                    # Subscribe to P&L stream for DailyPnL
                    for acct in self._ib.managedAccounts():
                        self._ib.reqPnL(acct)
                        log.info(
                            "Worker %s subscribed to pnlEvent for %s", self._label, acct)
                        tws_log.info(
                            "[%s] API-CALL  reqPnL(account=%s)", self._label, acct)

                    # reqPositionsAsync subscribes to ALL accounts simultaneously.
                    # reqAccountUpdates only covers one account at a time and
                    # cancels the previous subscription on each new call.
                    await self._ib.reqPositionsAsync()
                    log.info("Worker %s: subscribed to positions for all %d account(s)",
                             self._label, len(self._ib.managedAccounts()))
                    tws_log.info("[%s] API-CALL  reqPositionsAsync() [all accounts]",
                                 self._label)

                    # Snapshot existing open + completed orders
                    tws_log.info(
                        "[%s] API-CALL  _snapshot_existing_orders()", self._label)
                    await self._snapshot_existing_orders()

                    # Snapshot today's fills so the Trades view is populated
                    # on startup and after a reconnect.
                    await self._snapshot_fills()

                    # Overnight parents that finished while we were away
                    # still owe their exits.
                    await self._reconcile_pending_exits()

                except Exception as exc:
                    log.exception("Worker %s post-connect setup error (connection still alive): %s",
                                  self._label, exc)

                reconnect_countdown = 5.0

            await asyncio.sleep(1)
            reconnect_countdown -= 1.0

    async def _throttler_loop(self) -> None:
        """Flush the buffer every 250 ms, stamping the connection label."""
        while self._running:
            await asyncio.sleep(0.25)

            # Emit account summary batch
            if self._buffer:
                batch: list[tuple[str, str, str, str, str]] = []
                for (account, tag, _ccy), (value, currency) in self._buffer.items():
                    batch.append((self._label, account, tag, value, currency))
                self._buffer.clear()
                if batch:
                    self.summary_batch_updated.emit(batch)

            # Emit portfolio batch
            if self._portfolio_buffer:
                p_batch = list(self._portfolio_buffer.values())
                self._portfolio_buffer.clear()
                self.portfolio_updated.emit(p_batch)

            # RC #1: flush pnl_single buffer — ONE list-payload signal per
            # 250 ms window instead of one QMetaCallEvent per position.
            # The dict naturally de-duplicates: only the latest value per
            # (account, conId) is kept, so rapid ticks collapse into one
            # batch entry.
            if self._pnl_single_buffer:
                pnl_batch = [
                    (acct, con_id, daily, realized, unrealized)
                    for (acct, con_id), (daily, realized, unrealized)
                    in self._pnl_single_buffer.items()
                ]
                self._pnl_single_buffer.clear()
                self.pnl_single_batch_updated.emit(pnl_batch)

            # RC #3: process deferred pnlSingle cancellations for flat positions.
            # Positions that reached zero ≥60 s ago have their reqPnLSingle
            # cancelled here so the subscription count doesn't grow all day.
            if self._pnl_zero_pending_cancel \
                    and self._ib is not None and self._ib.isConnected():
                now = time.monotonic()
                for key, deadline in list(self._pnl_zero_pending_cancel.items()):
                    if now >= deadline:
                        acct, con_id = key
                        try:
                            self._ib.cancelPnLSingle(acct, "", con_id)
                            tws_log.info(
                                "[%s] API-CALL  cancelPnLSingle(account=%s, conId=%s)"
                                " [pos flat ≥60 s]",
                                self._label, acct, con_id)
                        except Exception as exc:
                            log.debug(
                                "Worker %s: cancelPnLSingle failed for %s/%s: %s",
                                self._label, acct, con_id, exc)
                        self._pnl_single_subs.discard(key)
                        del self._pnl_zero_pending_cancel[key]

            if self._l1_refused:
                self._retry_refused_quotes()

            # RC #5: periodic stale-data pruning (every ~75 s: 300 × 250 ms).
            self._prune_ticker += 1
            if self._prune_ticker >= 300:
                self._prune_ticker = 0
                self._prune_stale_data()

    async def _do_browse(self, params: dict) -> None:
        """Find contracts matching *params*.

        - **STK** → uses ``qualifyContractsAsync`` (fast, single result).
        - **FUT / OPT** → uses ``reqContractDetailsAsync`` (full browse).

        Results are always emitted via ``browse_results`` as a list[dict].
        Scheduled from the main thread via ``run_coroutine_threadsafe``.
        """
        if self._ib is None or not self._ib.isConnected():
            self.browse_failed.emit(
                "Not connected to TWS. Please check your connection on the Conn page.")
            return

        _TIMEOUT = 15  # seconds – bail out if IB doesn't respond

        try:
            from ib_async import Contract
            sec_type = params.get("sec_type", "STK")

            c = Contract()
            c.secType = sec_type
            c.symbol = params.get("symbol", "")
            c.exchange = params.get("exchange") or (
                "SMART" if sec_type == "STK" else "")
            c.currency = params.get("currency", "USD")
            if params.get("expiry"):
                c.lastTradeDateOrContractMonth = params["expiry"]
            if params.get("strike") and float(params["strike"]) > 0:
                c.strike = float(params["strike"])
            if params.get("right"):
                c.right = params["right"]

            if sec_type == "STK":
                # ── STK: fast qualify path ─────────────────────────────────
                log.info("Worker %s qualifying STK: %s %s",
                         self._label, c.symbol, c.exchange)
                tws_log.info("[%s] API-CALL  qualifyContractsAsync(secType=STK, symbol=%s, exchange=%s)",
                             self._label, c.symbol, c.exchange)
                qualified = await asyncio.wait_for(
                    self._ib.qualifyContractsAsync(c), timeout=_TIMEOUT,
                )
                if qualified and qualified[0].conId:
                    q = qualified[0]
                    results = [{
                        "conid": q.conId,
                        "symbol": q.symbol,
                        "sec_type": q.secType,
                        "exchange": q.exchange,
                        "currency": q.currency,
                        "expiry": getattr(q, "lastTradeDateOrContractMonth", ""),
                        "strike": float(getattr(q, "strike", 0.0)),
                        "right": getattr(q, "right", ""),
                        "local_symbol": q.localSymbol,
                        "multiplier": getattr(q, "multiplier", ""),
                        "primary_exchange": getattr(q, "primaryExchange", ""),
                    }]
                    log.info("Worker %s qualified STK: %s → conId=%s",
                             self._label, q.symbol, q.conId)
                    tws_log.info("[%s] API-RESP  qualifyContractsAsync → STK %s conId=%s exchange=%s",
                                 self._label, q.symbol, q.conId, q.exchange)
                    self.browse_results.emit(results)
                else:
                    tws_log.warning("[%s] API-RESP  qualifyContractsAsync → no match for STK %s",
                                    self._label, c.symbol)
                    self.browse_failed.emit(
                        f"No match found for STK {c.symbol} on {c.exchange}")
            else:
                # ── FUT / OPT: full browse via reqContractDetails ──────────
                log.info("Worker %s browsing: %s %s",
                         self._label, c.secType, c.symbol)
                tws_log.info("[%s] API-CALL  reqContractDetailsAsync(secType=%s, symbol=%s, exchange=%s, expiry=%s)",
                             self._label, c.secType, c.symbol, c.exchange,
                             getattr(c, "lastTradeDateOrContractMonth", ""))
                details = await asyncio.wait_for(
                    self._ib.reqContractDetailsAsync(c), timeout=_TIMEOUT,
                )

                if not details:
                    tws_log.warning("[%s] API-RESP  reqContractDetailsAsync → 0 contracts for %s %s",
                                    self._label, c.secType, c.symbol)
                    self.browse_failed.emit(
                        f"No contracts found for {c.secType} {c.symbol}")
                    return

                results = []
                for d in details:
                    q = d.contract
                    results.append({
                        "conid": q.conId,
                        "symbol": q.symbol,
                        "sec_type": q.secType,
                        "exchange": q.exchange,
                        "currency": q.currency,
                        "expiry": getattr(q, "lastTradeDateOrContractMonth", ""),
                        "strike": float(getattr(q, "strike", 0.0)),
                        "right": getattr(q, "right", ""),
                        "local_symbol": q.localSymbol,
                        "multiplier": getattr(q, "multiplier", ""),
                        "primary_exchange": getattr(q, "primaryExchange", ""),
                        # Real tick size from ContractDetails — persisted
                        # on the chip entry (min_tick) so UI ladders stop
                        # relying on hardcoded per-symbol tables.
                        "min_tick": float(getattr(d, "minTick", 0) or 0),
                    })

                log.info("Worker %s browse returned %d contract(s) for %s",
                         self._label, len(results), c.symbol)
                tws_log.info("[%s] API-RESP  reqContractDetailsAsync → %d contract(s) for %s %s",
                             self._label, len(results), c.secType, c.symbol)
                self.browse_results.emit(results)

        except asyncio.TimeoutError:
            log.warning("Worker %s browse timed out after %ds for %s %s",
                        self._label, _TIMEOUT,
                        params.get("sec_type"), params.get("symbol"))
            tws_log.error("[%s] API-ERR   browse timed out after %ds for %s %s",
                          self._label, _TIMEOUT,
                          params.get("sec_type"), params.get("symbol"))
            self.browse_failed.emit(
                f"Search timed out after {_TIMEOUT}s — TWS may have lost "
                f"connectivity. Please try again.")
        except Exception as exc:
            log.exception("Worker %s browse failed: %s", self._label, exc)
            tws_log.error("[%s] API-ERR   browse failed: %s", self._label, exc)
            self.browse_failed.emit(str(exc))

    # ── Options-income ticket coroutines ──────────────────────────────────────

    async def _do_option_chain(self, token: int, params: dict) -> None:
        """Fetch option expirations + strikes for one underlying.

        Emits ``option_chain_results(token, dict)`` with sorted
        ``expirations`` (YYYYMMDD) and ``strikes`` (floats) from the SMART
        chain (fallback: first exchange returned).
        """
        if self._ib is None or not self._ib.isConnected():
            self.option_chain_failed.emit(token, "Not connected to TWS.")
            return

        _TIMEOUT = 15
        symbol = params.get("symbol", "")
        con_id = int(params.get("con_id", 0))
        try:
            # A chip symbol with no position has no known conId — qualify
            # the stock first (reqSecDefOptParams requires underlyingConId).
            if con_id <= 0:
                from ib_async import Stock
                qualified = await asyncio.wait_for(
                    self._ib.qualifyContractsAsync(
                        Stock(symbol, "SMART", "USD")),
                    timeout=_TIMEOUT)
                if not qualified or not qualified[0].conId:
                    self.option_chain_failed.emit(
                        token, f"Could not resolve underlying {symbol}.")
                    return
                con_id = int(qualified[0].conId)

            tws_log.info("[%s] API-CALL  reqSecDefOptParamsAsync(symbol=%s, "
                         "underlyingConId=%d)", self._label, symbol, con_id)
            chains = await asyncio.wait_for(
                self._ib.reqSecDefOptParamsAsync(
                    underlyingSymbol=symbol,
                    futFopExchange="",
                    underlyingSecType=params.get("sec_type", "STK"),
                    underlyingConId=con_id),
                timeout=_TIMEOUT)
            if not chains:
                self.option_chain_failed.emit(
                    token, f"No option chain for {symbol}.")
                return
            # reqSecDefOptParams returns one row per (exchange, trading
            # class) — ETFs like QQQ have several SMART rows (standard
            # class plus quarterlies/flex classes with only a handful of
            # expirations).  Taking the first SMART row blindly can land
            # on a sparse class → one expiry chip and near-empty ladder.
            # Prefer SMART, then the underlying's own trading class,
            # then whichever class lists the most expirations.
            pool = [c for c in chains if c.exchange == "SMART"] \
                or list(chains)
            oc = max(pool, key=lambda c: (
                (c.tradingClass or "") == symbol,
                len(c.expirations or ()),
            ))
            result = {
                "symbol": symbol,
                "underlying_conid": con_id,
                "exchange": oc.exchange,
                "trading_class": oc.tradingClass,
                "multiplier": oc.multiplier,
                "expirations": sorted(oc.expirations),
                "strikes": sorted(float(s) for s in oc.strikes),
            }
            tws_log.info("[%s] API-RESP  reqSecDefOptParamsAsync → %s: %d "
                         "expiration(s), %d strike(s)", self._label, symbol,
                         len(result["expirations"]), len(result["strikes"]))
            self.option_chain_results.emit(token, result)
        except asyncio.TimeoutError:
            self.option_chain_failed.emit(
                token, f"Option chain timed out after {_TIMEOUT}s.")
        except Exception as exc:
            log.exception("Worker %s option chain failed: %s",
                          self._label, exc)
            self.option_chain_failed.emit(token, str(exc))

    async def _do_option_contracts(self, token: int, params: dict) -> None:
        """Resolve every listed strike for one (symbol, expiry, right).

        One reqContractDetails call without a strike filter — the ticket
        filters to its visible strikes locally.  Emits contract dicts
        (browse shape) plus ``min_tick``.
        """
        if self._ib is None or not self._ib.isConnected():
            self.option_contracts_failed.emit(token, "Not connected to TWS.")
            return

        _TIMEOUT = 15
        try:
            from ib_async import Contract
            c = Contract()
            c.secType = "OPT"
            c.symbol = params.get("symbol", "")
            c.lastTradeDateOrContractMonth = params.get("expiry", "")
            c.right = params.get("right", "")
            c.exchange = "SMART"
            c.currency = params.get("currency") or "USD"
            if params.get("trading_class"):
                c.tradingClass = params["trading_class"]

            tws_log.info("[%s] API-CALL  reqContractDetailsAsync(OPT %s %s "
                         "%s tc=%s)", self._label, c.symbol,
                         c.lastTradeDateOrContractMonth, c.right,
                         c.tradingClass)
            details = await asyncio.wait_for(
                self._ib.reqContractDetailsAsync(c), timeout=_TIMEOUT)

            results = []
            for d in details or []:
                q = d.contract
                results.append({
                    "conid": q.conId,
                    "symbol": q.symbol,
                    "sec_type": q.secType,
                    "exchange": q.exchange,
                    "currency": q.currency,
                    "expiry": getattr(q, "lastTradeDateOrContractMonth", ""),
                    "strike": float(getattr(q, "strike", 0.0)),
                    "right": getattr(q, "right", ""),
                    "local_symbol": q.localSymbol,
                    "multiplier": getattr(q, "multiplier", ""),
                    "trading_class": getattr(q, "tradingClass", ""),
                    "min_tick": float(getattr(d, "minTick", 0.0) or 0.0),
                })
            tws_log.info("[%s] API-RESP  reqContractDetailsAsync → %d option "
                         "contract(s) for %s", self._label, len(results),
                         c.symbol)
            self.option_contracts_results.emit(token, results)
        except asyncio.TimeoutError:
            self.option_contracts_failed.emit(
                token, f"Contract lookup timed out after {_TIMEOUT}s.")
        except Exception as exc:
            log.exception("Worker %s option contracts failed: %s",
                          self._label, exc)
            self.option_contracts_failed.emit(token, str(exc))

    async def _do_option_quotes(self, token: int,
                                contracts: list[dict]) -> None:
        """Subscribe the ticket's quote batch (visible strikes + underlying)."""
        if self._ib is None or not self._ib.isConnected():
            self.option_quotes_failed.emit(token, "Not connected to TWS.")
            return

        await self._do_cancel_option_quotes()
        self._opt_quote_token = token
        self._opt_quote_contracts = list(contracts)
        try:
            from ib_async import Contract
            # 2 = frozen: live when entitled, and after the close IB serves
            # the LAST RECORDED bid/ask instead of the -1 "no market"
            # sentinel — so the ladder keeps meaningful bid/ask outside
            # market hours.  Type 4 (delayed-frozen) does NOT do this for
            # live-entitled accounts: IB upgrades it to plain live (1) and
            # the frozen close quotes never arrive.  If the account has no
            # live subscription, type 2 fails with error 354 — _on_ib_error
            # then flips _opt_delayed_fallback and resubscribes with 4.
            self._ib.reqMarketDataType(
                4 if self._opt_delayed_fallback else 2)
            for params in contracts:
                conid = int(params.get("conid", 0))
                if conid <= 0:
                    continue
                c = Contract(conId=conid)
                c.exchange = params.get("exchange") or "SMART"
                c.currency = params.get("currency") or "USD"
                if params.get("sec_type"):
                    c.secType = params["sec_type"]
                if params.get("local_symbol"):
                    c.localSymbol = params["local_symbol"]
                ticker = self._ib.reqMktData(c, genericTickList="",
                                             snapshot=False)
                self._opt_tickers[conid] = ticker
                self._ticker_to_opt_conid[id(ticker)] = conid
                # Emit immediately when ib_async already has cached data.
                self._emit_opt_quote(conid, ticker)
            tws_log.info("[%s] API-CALL  reqMktData ×%d [options ticket]",
                         self._label, len(self._opt_tickers))
        except Exception as exc:
            log.warning("Worker %s: option quote batch failed: %s",
                        self._label, exc)
            self.option_quotes_failed.emit(token, str(exc))

    async def _do_cancel_option_quotes(self) -> None:
        """Cancel every ticket quote subscription."""
        tickers = list(self._opt_tickers.values())
        self._opt_tickers.clear()
        self._ticker_to_opt_conid.clear()
        if not tickers:
            return
        if self._ib is not None and self._ib.isConnected():
            for ticker in tickers:
                try:
                    self._ib.cancelMktData(ticker.contract)
                except Exception as exc:
                    log.debug("Worker %s: cancel option quote error: %s",
                              self._label, exc)
            tws_log.info("[%s] API-CALL  cancelMktData ×%d [options ticket]",
                         self._label, len(tickers))

    def _emit_opt_quote(self, conid: int, ticker) -> None:
        """Scrub sentinels and emit one option_quote_updated.

        Runs on the worker's asyncio thread — must only emit Qt signals.
        """
        def _clean(v) -> float | None:
            """None = IB sent no data (nan / -1 sentinel / DBL_MAX).
            0.0 passes through — a deep-OTM option with no buyers has a
            REAL zero bid, and the ladder shows it as 0.00, not "—"."""
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            if math.isnan(f) or f < 0 or f >= _IB_DBL_MAX:
                return None
            return f

        # IB streams separate greek computations for model / last / bid /
        # ask (ticks 13 / 12 / 10 / 11) and any of them can carry an empty
        # delta.  Prefer the model computation, then fall back to the
        # others so the ladder fills in as soon as ANY computation lands.
        delta = 0.0
        for attr in ("modelGreeks", "lastGreeks", "bidGreeks", "askGreeks"):
            greeks = getattr(ticker, attr, None)
            if greeks is None:
                continue
            try:
                d = float(getattr(greeks, "delta", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if d and math.isfinite(d) and abs(d) <= 1.0:
                delta = d
                break

        self.option_quote_updated.emit(self._opt_quote_token, conid, {
            "bid": _clean(getattr(ticker, "bid", None)),
            "ask": _clean(getattr(ticker, "ask", None)),
            "last": _clean(getattr(ticker, "last", None)),
            "close": _clean(getattr(ticker, "close", None)),
            "delta": delta,
        })

    async def _do_symbol_search(self, token: int, pattern: str) -> None:
        """Fuzzy-match *pattern* against IB's symbol universe.

        Emits ``symbol_search_results(token, list[dict])`` on success or
        ``symbol_search_failed(token, msg)`` on any error.  IB rate-limits
        reqMatchingSymbols to roughly one call per second — the callers
        (SymbolSearchPopup, options ticket) debounce keystrokes to stay
        under that.
        """
        if self._ib is None or not self._ib.isConnected():
            self.symbol_search_failed.emit(token, "Not connected to TWS.")
            return

        _TIMEOUT = 10  # seconds

        try:
            tws_log.info("[%s] API-CALL  reqMatchingSymbolsAsync(pattern=%s)",
                         self._label, pattern)
            descriptions = await asyncio.wait_for(
                self._ib.reqMatchingSymbolsAsync(pattern), timeout=_TIMEOUT)
            results = _matching_symbols_to_dicts(descriptions)
            tws_log.info(
                "[%s] API-RESP  reqMatchingSymbolsAsync → %d match(es) "
                "for '%s'", self._label, len(results), pattern)
            self.symbol_search_results.emit(token, results)
        except asyncio.TimeoutError:
            tws_log.error(
                "[%s] API-ERR   symbol search timed out after %ds for '%s'",
                self._label, _TIMEOUT, pattern)
            self.symbol_search_failed.emit(
                token, f"Search timed out after {_TIMEOUT}s.")
        except Exception as exc:
            log.exception("Worker %s symbol search failed: %s",
                          self._label, exc)
            tws_log.error("[%s] API-ERR   symbol search failed: %s",
                          self._label, exc)
            self.symbol_search_failed.emit(token, str(exc))

    async def _do_place_option_order(self, params: dict) -> None:
        """whatIf margin pre-check, then transmit through _do_place_order.

        The whatIf probe uses the same conId-first contract and a plain
        LimitOrder mirror of the real order.  If IB rejects the probe (or
        it times out) nothing is transmitted and ``option_order_result``
        carries the error; on success the margin deltas ride along.
        """
        account = params.get("account", "")
        symbol = params.get("symbol", "")
        if self._ib is None or not self._ib.isConnected():
            self.option_order_result.emit({
                "ok": False, "account": account, "symbol": symbol,
                "error": "Not connected to TWS."})
            return

        # Pre-generate the orderRef so the pre-check row in the Orders
        # view and the transmitted order share one blotter key — the
        # real order's first status simply takes over the row.  A caller
        # may pre-supply the ref (the Options Income ticket does, to
        # track its session orders); it must follow the nano_* format.
        params = dict(params)
        order_ref = params.get("order_ref") or _generate_order_ref()
        params["order_ref"] = order_ref
        order_key = f"{account}_{order_ref}"
        self._emit_opt_precheck_status(order_key, order_ref, params,
                                       "Margin check")

        def _precheck_failed(msg: str) -> None:
            self._order_error_messages[order_ref] = msg
            self._emit_opt_precheck_status(order_key, order_ref, params,
                                           "Rejected")
            self.option_order_result.emit({
                "ok": False, "account": account, "symbol": symbol,
                "error": msg})

        try:
            from ib_async import Contract, LimitOrder, MarketOrder
            # conId + exchange ONLY — TWS rejects option requests that
            # carry redundant identifiers (secType/localSymbol) next to
            # a conId with error 478 ("Parameters in request conflicts
            # with contract parameters received by contract id").
            c = Contract(conId=int(params.get("conid", 0)))
            c.exchange = params.get("exchange") or "SMART"
            c.currency = params.get("currency") or "USD"

            if (params.get("order_type") or "LMT") == "MKT":
                # A zero-price LimitOrder probe would be rejected outright.
                probe = MarketOrder(
                    params.get("action", "SELL"),
                    int(params.get("qty", 0)))
            else:
                probe = LimitOrder(
                    params.get("action", "SELL"),
                    int(params.get("qty", 0)),
                    float(params.get("limit_price", 0.0)))
            probe.account = account
            probe.tif = params.get("tif", "DAY")

            tws_log.info("[%s] API-CALL  whatIfOrderAsync(%s %s ×%s @ %s "
                         "acct=%s)", self._label, probe.action, symbol,
                         probe.totalQuantity,
                         getattr(probe, "lmtPrice", "") or "MKT", account)
            state = await asyncio.wait_for(
                self._ib.whatIfOrderAsync(c, probe), timeout=15)

            def _margin(v) -> float:
                try:
                    f = float(v)
                    return 0.0 if abs(f) >= _IB_DBL_MAX else f
                except (TypeError, ValueError):
                    return 0.0

            if state is None or not getattr(state, "initMarginChange", ""):
                _precheck_failed("Margin pre-check rejected by TWS.")
                tws_log.warning("[%s] API-RESP  whatIfOrder → rejected",
                                self._label)
                return

            result = {
                "ok": True, "account": account, "symbol": symbol,
                "init_margin": _margin(getattr(state, "initMarginChange", 0)),
                "maint_margin": _margin(
                    getattr(state, "maintMarginChange", 0)),
            }
            tws_log.info("[%s] API-RESP  whatIfOrder → ok initMargin=%s "
                         "maintMargin=%s", self._label,
                         result["init_margin"], result["maint_margin"])
        except asyncio.TimeoutError:
            _precheck_failed("Margin pre-check timed out.")
            return
        except Exception as exc:
            log.exception("Worker %s whatIf pre-check failed: %s",
                          self._label, exc)
            _precheck_failed(str(exc))
            return

        await self._do_place_order(params)
        self.option_order_result.emit(result)

    def _emit_opt_precheck_status(self, order_id: str, order_ref: str,
                                  params: dict, status: str) -> None:
        """Synthetic Orders-view row for the options whatIf pre-check.

        Keyed exactly like the real order (``account_orderRef``) so the
        transmitted order's first status update takes over the row; a
        failed pre-check leaves it as Rejected with the reason."""
        now = datetime.now(_ET)
        lmt = params.get("limit_price", 0)
        self.order_status_updated.emit({
            "order_id": order_id,
            "order_ref": order_ref,
            "symbol": (params.get("local_symbol")
                       or params.get("symbol", "")),
            # Contract identity — lets the UI jump the Book Trader ladder
            # to this order's instrument even when it isn't in contracts.db
            # (options are placed from live chains, never saved).
            "con_id": int(params.get("conid", 0) or 0),
            "sec_type": params.get("sec_type", "") or "",
            "account": params.get("account", ""),
            "action": params.get("action", ""),
            "order_type": params.get("order_type", "LMT"),
            "quantity": str(int(params.get("qty", 0) or 0)),
            "limit_price": str(lmt) if lmt else "–",
            "stop_price": "–",
            "tif": params.get("tif", "DAY"),
            "fill_price": "–",
            "status": status,
            "time": now.strftime("%H:%M:%S"),
            "time_sort": now.isoformat(),
            "error_message": self._order_error_messages.get(order_ref, ""),
        })

    # ── Order placement ───────────────────────────────────────────────────────

    async def _do_place_order(self, params: dict) -> None:
        """Build Contract + Order, place via ib_async, bind trade.statusEvent."""
        if self._ib is None or not self._ib.isConnected():
            log.warning(
                "Worker %s: place_order but not connected", self._label)
            return

        try:
            from ib_async import Contract, MarketOrder, LimitOrder, TagValue

            # ── Build contract ─────────────────────────────────────────────
            conid = int(params.get("conid", 0))
            if conid <= 0:
                log.warning("Worker %s: invalid conid for order: %s",
                            self._label, conid)
                return

            c = Contract(conId=conid)
            c.exchange = params.get("exchange") or "SMART"
            c.currency = params.get("currency", "USD")
            sec_type = params.get("sec_type", "")
            # Remember this contract's metadata so fills can be enriched.
            # Matters for options (lean conId-only contract below → the
            # Trade, and therefore every live Fill, carries no
            # symbol/secType); harmless for the rest.
            self._conid_contract_meta[conid] = {
                "symbol": params.get("symbol", "") or "",
                "local_symbol": params.get("local_symbol", "") or "",
                "sec_type": sec_type,
                "expiry": params.get("expiry", "") or "",
                "multiplier": params.get("multiplier", "") or "",
                "strike": params.get("strike", 0.0) or 0.0,
                "right": params.get("right", "") or "",
            }
            if sec_type == "OPT":
                # Options: conId + exchange ONLY.  TWS rejects option
                # orders whose request carries redundant identifiers
                # (symbol/secType/localSymbol/expiry) alongside a conId
                # with error 478.  The blotter reads its fields from
                # *params*, so the lean contract loses nothing.
                pass
            else:
                # Populate symbol fields so the Trade object kept by
                # ib_async retains them across reqAllOpenOrdersAsync()
                # snapshots (ib_async does NOT overwrite the contract on
                # existing Trade objects).
                c.symbol = params.get("symbol", "")
                c.secType = sec_type
                c.localSymbol = params.get("local_symbol", "")
                if params.get("expiry"):
                    c.lastTradeDateOrContractMonth = params["expiry"]
                if params.get("multiplier"):
                    c.multiplier = params["multiplier"]

            # Stocks: route to IBEOS (IBKR Overnight ATS) during overnight
            # session (20:00–03:59 ET), otherwise SMART.  IBEOS is a
            # US-only venue — non-USD stocks (e.g. Toronto/CAD) always
            # stay on SMART or TWS would reject the order.
            if sec_type == "STK":
                now_et = datetime.now(_ET)
                if stk_routes_overnight(c.currency, now_et):
                    c.exchange = "IBEOS"
                    log.info("Worker %s: STK overnight → exchange=IBEOS (ET %02d:%02d)",
                             self._label, now_et.hour, now_et.minute)
                else:
                    c.exchange = "SMART"

            # ── Build order ───────────────────────────────────────────────
            action = params.get("action", OrderAction.BUY)
            qty = int(params.get("qty", 1))
            order_type = params.get("order_type", OrderType.MKT)
            account = params.get("account", "")
            tif = params.get("tif", "DAY")

            if order_type == OrderType.ADAPTIVE:
                # IB Adaptive Algo — a LimitOrder with the Adaptive algo
                # strategy attached.  IB works the order at mid and sweeps
                # toward the limit price based on the urgency parameter.
                limit_price = float(params.get("limit_price", 0))
                priority = params.get("adaptive_priority", "Normal")
                order = LimitOrder(action, qty, limit_price)
                order.algoStrategy = "Adaptive"
                order.algoParams = [TagValue("adaptivePriority", priority)]
                log.info("Worker %s: Adaptive order %s %d @ limit %.4f "
                         "priority=%s",
                         self._label, action, qty, limit_price, priority)
            elif order_type == OrderType.PEG_PRIM:
                # Pegged to Primary (REL) — pegs to the NBBO with an
                # optional offset, capped by a limit price.
                #   orderType  = "REL"
                #   auxPrice   = offset from the primary (bid for BUY, ask
                #                for SELL) — must be >= 0
                #   lmtPrice   = hard price cap (ceiling BUY / floor SELL)
                from ib_async import Order
                limit_price = float(params.get("limit_price", 0))
                peg_offset = float(params.get("peg_offset", 0))
                order = Order(
                    action=action,
                    totalQuantity=qty,
                    orderType="REL",
                    lmtPrice=limit_price,
                    auxPrice=peg_offset,
                )
                log.info("Worker %s: PEG_PRIM order %s %d lmt=%.4f "
                         "auxPrice(offset)=%.4f",
                         self._label, action, qty, limit_price, peg_offset)
            elif order_type == OrderType.LMT:
                limit_price = float(params.get("limit_price", 0))
                order = LimitOrder(action, qty, limit_price)
            else:
                order = MarketOrder(action, qty)

            order.account = account
            order.tif = tif
            # place_option_order pre-generates the ref so its pre-check
            # blotter row and the real order share one key.
            order.orderRef = params.get("order_ref") or _generate_order_ref()

            # Stocks: configure extended-hours flags based on exchange and
            # current time of day (Eastern).
            #   20:00–03:59  → IBEOS overnight venue, no RTH flags
            #   04:00–09:29  → pre-market,   outsideRth=True
            #   09:30–15:59  → regular hours, outsideRth=False
            #   16:00–19:59  → after-hours,  outsideRth=True
            if sec_type == "STK":
                if c.exchange == "IBEOS":
                    # IBEOS is an overnight venue — no RTH flags needed.
                    # advancedErrorOverride bypasses TWS precautionary
                    # Error 10329 that blocks direct-routed IBEOS orders.
                    order.advancedErrorOverride = "10329"
                else:
                    now_et = datetime.now(_ET)
                    h, m = now_et.hour, now_et.minute
                    in_rth = (h > 9 or (h == 9 and m >= 30)) and h < 16
                    order.outsideRth = not in_rth
                    order.allowPreOpen = False
                    log.info("Worker %s: STK RTH flags → outsideRth=%s "
                             "(ET %02d:%02d, in_rth=%s)",
                             self._label, order.outsideRth, h, m, in_rth)

            # ── Bracket (TP / SL / Trail-stop attached children) ──────────
            # A parent with any enabled tier becomes the parent of a bracket:
            # children share parentId and are transmitted together (only the
            # last child has transmit=True, per ib_async's documented pattern).
            tp_tier = params.get("profit_take")
            sl_tier = params.get("stop_loss")
            tr_tier = params.get("trailing")
            bracket_children: list = []
            # (trade, key, blotter-params) triples appended as children
            # are placed below — the parent's fill callback re-anchors
            # them to the actual fill price.
            bracket_reanchor: list = []
            bracket_ref_used = 0.0
            # IBEOS (overnight) takes plain limits only: an attached STP /
            # STP LMT / TRAIL child would be refused (387).  The exits
            # are then held back and placed after the parent fills, as
            # standalone OCA orders routed SMART — IB accepts them and
            # holds the stops until the regular session opens.
            defer_exits = sec_type == "STK" and c.exchange == "IBEOS"
            deferred_ref = 0.0
            # …except a take profit alone: IBEOS takes limit children, so
            # it rides WITH the parent as two attached TPs — one on IBEOS
            # for tonight, one SMART (outsideRth) for 04:00 on.  Attached
            # siblings are OCA'd by IB itself, so it all lives at IB and
            # no NanoPulse step is needed after the fill.  A rejection
            # retries once with the parent alone (``_no_tp_bundle``).
            tp_bundle = (defer_exits and bool(tp_tier) and not sl_tier
                         and not tr_tier
                         and not params.get("_no_tp_bundle"))
            bundle_children: list = []      # (contract, child) in order
            if any((tp_tier, sl_tier, tr_tier)):
                # FlashTradeView sets bracket_ref_price to the expected-fill
                # anchor — last_price for Adaptive / PEG / cap-wrapped MKT
                # orders, limit_price only for true BID_LMT / ASK_LMT.  Fall
                # back to last_price if the caller didn't set it, which
                # keeps bracket children anchored to the market rather than
                # to a cap that will not reflect the actual fill.
                ref_price = float(params.get("bracket_ref_price")
                                  or params.get("last_price") or 0.0)
                # When neither side supplied a price, fall back to the
                # live bid/ask midpoint cached on this worker for the
                # symbol.  Common for futures right after a symbol
                # switch — the L1 stream has bid/ask but the user
                # clicked Buy/Sell before the next "last" tick landed.
                # Without this fallback the bracket children silently
                # vanish because ref_price stays at 0.
                if ref_price <= 0:
                    sym = params.get("symbol", "")
                    cached_bid, cached_ask = self._bg_last_bid_ask.get(
                        sym, (0.0, 0.0))
                    if cached_bid > 0 and cached_ask > 0:
                        ref_price = (cached_bid + cached_ask) / 2.0
                        order_log.info(
                            "[%s] BRACKET-REF  using bid/ask mid %.4f "
                            "for %s (last_price was 0)",
                            self._label, ref_price, sym)
                if ref_price > 0 and tp_bundle:
                    order.orderId = self._ib.client.getReqId()
                    order.transmit = False
                    bracket_ref_used = ref_price
                    bundle_children = self._build_tp_bundle(
                        order, c, params, ref_price,
                        float(params.get("tick_size") or 0.01))
                    if bundle_children:
                        order_log.info(
                            "[%s] BRACKET-BUNDLE  overnight TP-only — "
                            "IBEOS + SMART take profit attached, "
                            "orderRef=%s", self._label, order.orderRef)
                    else:
                        order.transmit = True
                elif ref_price > 0 and defer_exits:
                    deferred_ref = ref_price
                    order_log.info(
                        "[%s] BRACKET-DEFER  overnight (IBEOS) — exits "
                        "placed after the fill, routed SMART, orderRef=%s",
                        self._label, order.orderRef)
                elif ref_price > 0:
                    if tr_tier and self._convert_mkt_parent_for_trail(
                            order, params, ref_price):
                        order_type = "LMT"
                    order.orderId = self._ib.client.getReqId()
                    order.transmit = False
                    bracket_ref_used = ref_price
                    bracket_children = self._build_bracket_children(
                        order, params, ref_price,
                        float(params.get("tick_size") or 0.01),
                    )
                else:
                    order_log.warning(
                        "[%s] BRACKET-SKIP  no reference price "
                        "(bracket_ref=%s last=%s, no cached bid/ask) — "
                        "parent-only submit, orderRef=%s", self._label,
                        params.get("bracket_ref_price"),
                        params.get("last_price"), order.orderRef)

            # ── Place ─────────────────────────────────────────────────────
            # Log every parameter sent to ib_async for debugging
            log.info(
                "Worker %s: placeOrder params → "
                "Contract(conId=%s, exchange=%s) | "
                "Order(action=%s, totalQuantity=%s, orderType=%s, "
                "lmtPrice=%s, auxPrice=%s, tif=%s, account=%s, "
                "outsideRth=%s, allowPreOpen=%s, "
                "advancedErrorOverride=%s, "
                "algoStrategy=%s, algoParams=%s, orderRef=%s)",
                self._label,
                c.conId, c.exchange,
                order.action, order.totalQuantity, order.orderType,
                getattr(order, "lmtPrice", "N/A"),
                getattr(order, "auxPrice", "N/A"),
                order.tif, order.account,
                getattr(order, "outsideRth", False),
                getattr(order, "allowPreOpen", "N/A"),
                getattr(order, "advancedErrorOverride", ""),
                getattr(order, "algoStrategy", ""),
                getattr(order, "algoParams", []),
                order.orderRef,
            )

            # TWS log: full API call detail
            tws_log.info(
                "[%s] API-CALL  placeOrder → "
                "Contract(conId=%s, symbol=%s, secType=%s, exchange=%s, currency=%s, "
                "localSymbol=%s, expiry=%s, multiplier=%s) | "
                "Order(action=%s, qty=%s, orderType=%s, lmtPrice=%s, auxPrice=%s, "
                "tif=%s, account=%s, outsideRth=%s, allowPreOpen=%s, "
                "advancedErrorOverride=%s, algoStrategy=%s, algoParams=%s, orderRef=%s)",
                self._label,
                c.conId, c.symbol, c.secType, c.exchange, c.currency,
                c.localSymbol, getattr(c, "lastTradeDateOrContractMonth", ""),
                getattr(c, "multiplier", ""),
                order.action, order.totalQuantity, order.orderType,
                getattr(order, "lmtPrice", "N/A"),
                getattr(order, "auxPrice", "N/A"),
                order.tif, order.account,
                getattr(order, "outsideRth", False),
                getattr(order, "allowPreOpen", "N/A"),
                getattr(order, "advancedErrorOverride", ""),
                getattr(order, "algoStrategy", ""),
                getattr(order, "algoParams", []),
                order.orderRef,
            )

            # Order log: submission
            order_log.info(
                "[%s] ORDER-SUBMIT  %s %s %d %s | orderType=%s lmt=%s "
                "account=%s orderRef=%s conId=%s exchange=%s",
                self._label, action, params.get("symbol", "?"), qty,
                sec_type, order_type,
                getattr(order, "lmtPrice", "N/A"),
                account, order.orderRef, c.conId, c.exchange,
            )

            # Full-detail audit dump for the debug-mode build.  Captures
            # the request payload, the resolved Order object's vars, and
            # every attached bracket child so the log alone is enough to
            # reconstruct exactly what got sent to TWS.
            if order_log.isEnabledFor(logging.DEBUG):
                try:
                    parent_vars = {
                        k: v for k, v in vars(order).items()
                        if not k.startswith("_")
                    }
                except Exception:
                    parent_vars = {}
                order_log.debug(
                    "[%s] ORDER-PLACE-DETAIL  orderRef=%s account=%s\n"
                    "  request_params=%s\n"
                    "  parent_order=%s",
                    self._label, order.orderRef, account,
                    params, parent_vars,
                )
                for ch in bracket_children:
                    try:
                        ch_vars = {
                            k: v for k, v in vars(ch).items()
                            if not k.startswith("_")
                        }
                    except Exception:
                        ch_vars = {}
                    order_log.debug(
                        "[%s] ORDER-PLACE-CHILD  parentRef=%s "
                        "childOrderRef=%s child=%s",
                        self._label, order.orderRef,
                        getattr(ch, "orderRef", "?"), ch_vars,
                    )

            trade = self._ib.placeOrder(c, order)

            # Account + orderRef = immutable canonical key.
            # Account numbers never change; connection labels can be renamed.
            order_id = f"{account}_{order.orderRef}"
            self._trades[order_id] = trade
            self._has_status_handler.add(order_id)
            if deferred_ref > 0:
                # On disk until the parent is done, so a fill while
                # NanoPulse is offline is caught on the next connect.
                try:
                    from src.core import pending_exits
                    pending_exits.add(
                        order_id, label=self._label, account=account,
                        order_ref=order.orderRef, params=params,
                        ref_price=deferred_ref)
                except Exception:
                    order_log.exception(
                        "[%s] BRACKET-DEFER  could not save pending exits "
                        "for %s — only this session will place them",
                        self._label, order_id)

            log.info("Worker %s: placed %s %s %d %s for account %s → %s",
                     self._label, action, order_type, qty,
                     params.get("symbol", "?"), account, order_id)

            tws_log.info("[%s] API-RESP  placeOrder → orderId=%s orderRef=%s",
                         self._label, trade.order.orderId, order.orderRef)
            order_log.info("[%s] ORDER-ACCEPTED  orderId=%s orderRef=%s → key=%s",
                           self._label, trade.order.orderId, order.orderRef, order_id)

            # ── Emit initial status to UI ─────────────────────────────────
            status = trade.orderStatus.status if trade.orderStatus.status else "Submitted"
            self._emit_order_status(trade, order_id, params, status)

            order_log.info("[%s] ORDER-STATUS  %s | status=%s filled=%s "
                           "avgFill=%s symbol=%s account=%s orderRef=%s",
                           self._label, order_id, status,
                           trade.orderStatus.filled, trade.orderStatus.avgFillPrice,
                           params.get("symbol", "?"), account,
                           getattr(trade.order, "orderRef", ""))

            # ── Bind trade.statusEvent for live updates ───────────────────
            # The key never changes (orderRef-based), so the closure is
            # trivially simple — no migration, no supersedes.
            frozen_params = dict(params)
            frozen_oid = order_id
            # One-shot latch: Filled can be reported more than once
            # (Filled then Completed) — re-anchor only on the first.
            _reanchored = [False]
            _exits_placed = [False]
            _bundle_retry = [False]
            _placed_at = time.monotonic()

            def _bundle_watch(t, _p=frozen_params):
                # Parent or child of a TP bundle reached a final state.
                if bundle_children:
                    self._maybe_retry_tp_bundle(
                        t, trade, _p, _placed_at, _bundle_retry)

            def _on_status(t, _oid=frozen_oid, _p=frozen_params):
                new_status = t.orderStatus.status
                self._emit_order_status(t, _oid, _p, new_status)
                # Log every status change to both TWS and order logs
                tws_log.info("[%s] EVENT     orderStatusEvent → orderId=%s "
                             "status=%s filled=%s avgFillPrice=%s",
                             self._label, t.order.orderId, new_status,
                             t.orderStatus.filled, t.orderStatus.avgFillPrice)
                order_log.info("[%s] ORDER-STATUS  %s | status=%s filled=%s "
                               "avgFill=%s symbol=%s account=%s orderRef=%s",
                               self._label, _oid, new_status,
                               t.orderStatus.filled, t.orderStatus.avgFillPrice,
                               _p.get("symbol", "?"), _p.get("account", ""),
                               getattr(t.order, "orderRef", ""))

                # On fill: compute the new position from fill data and emit
                # immediately.  positionEvent only sends the changed position
                # (not a full snapshot), so a closed position would simply be
                # absent and the UI would never learn it went to zero.
                if new_status in ("Filled", "Completed"):
                    if bracket_reanchor and not _reanchored[0]:
                        _reanchored[0] = True
                        try:
                            self._reanchor_bracket_children(
                                c, t, bracket_reanchor,
                                bracket_ref_used, _p)
                        except Exception:
                            order_log.exception(
                                "[%s] BRACKET-REANCHOR failed "
                                "(children keep submit-time prices)",
                                self._label)
                    self._apply_fill_to_position(t, _p)

                _bundle_watch(t)

                # Deferred overnight exits: once the parent is done — a
                # full fill, or a cancel / expiry after a partial one —
                # protect whatever filled.
                if (deferred_ref > 0 and not _exits_placed[0]
                        and new_status in _PARENT_DONE):
                    _exits_placed[0] = True
                    try:
                        from src.core import pending_exits
                        pending_exits.take(_oid)    # this session owns it
                        if float(t.orderStatus.filled or 0) > 0:
                            self._place_deferred_exits(c, t, _p,
                                                       deferred_ref)
                    except Exception:
                        order_log.exception(
                            "[%s] BRACKET-DEFER failed — no exits placed "
                            "for orderRef=%s", self._label,
                            getattr(t.order, "orderRef", ""))

            trade.statusEvent += _on_status

            # ── Place bracket children (if any) ────────────────────────────
            # Children are placed on the same contract, each with its own
            # fresh orderRef (the time-encoded format keeps the blotter's
            # submission-time parser working) and parentId pointing at the
            # parent's IB orderId.  Only the last child has transmit=True,
            # which flushes the whole bundle to TWS atomically.
            bracket_reanchor.extend(self._submit_exit_orders(
                c, bracket_children, params, order.orderRef))
            # TP bundle: each child on its own venue, the last transmits.
            for kid_contract, kid in bundle_children:
                bracket_reanchor.extend(self._submit_exit_orders(
                    kid_contract, [kid], params, order.orderRef,
                    on_status=_bundle_watch))

        except Exception as exc:
            log.exception("Worker %s: place_order failed: %s",
                          self._label, exc)
            tws_log.error(
                "[%s] API-ERR   placeOrder failed: %s", self._label, exc)
            order_log.error("[%s] ORDER-ERROR  placeOrder failed: %s | params=%s",
                            self._label, exc, params)

    def _submit_exit_orders(self, contract, children: list, params: dict,
                            parent_ref: str, on_status=None) -> list:
        """Place exit orders (bracket children or deferred overnight
        exits) and bind their status streams.  Returns the
        ``(trade, order_key, blotter_params)`` triples — the blotter
        params dict is the one the status closure holds, so a re-anchor
        that updates it in place shows in the Orders panel."""
        placed: list = []
        for ch in children:
            ch_trade = self._ib.placeOrder(contract, ch)
            ch_key = f"{ch.account}_{ch.orderRef}"
            self._trades[ch_key] = ch_trade
            self._has_status_handler.add(ch_key)

            order_log.info(
                "[%s] BRACKET-CHILD  parentRef=%s childRef=%s "
                "type=%s action=%s qty=%s lmt=%s aux=%s "
                "oca=%s transmit=%s",
                self._label, parent_ref, ch.orderRef,
                ch.orderType, ch.action, ch.totalQuantity,
                getattr(ch, "lmtPrice", "N/A"),
                getattr(ch, "auxPrice", "N/A"),
                getattr(ch, "ocaGroup", ""), ch.transmit)
            tws_log.info(
                "[%s] API-RESP  placeOrder(child) → orderId=%s orderRef=%s",
                self._label, ch_trade.order.orderId, ch.orderRef)

            # Bind each child's statusEvent so blotter/positions pick up
            # fills and cancels without any extra plumbing.  Start from
            # the parent's params (symbol / account / conid / sec_type)
            # then overlay the child's own identity — without this the
            # blotter would show the parent's BUY/SELL action and price
            # for every child row.
            ch_params = dict(params)
            ch_params["order_type"] = ch.orderType
            ch_params["action"]     = ch.action
            ch_params["qty"]        = int(ch.totalQuantity)
            # Child-specific price columns for the blotter.
            ch_params.pop("limit_price", None)
            ch_params.pop("stop_price", None)
            ch_params.update(self._child_price_params(
                ch.orderType,
                getattr(ch, "lmtPrice", None),
                getattr(ch, "auxPrice", None)))
            ch_frozen_oid = ch_key
            # Same ch_params dict the status closure holds — the
            # re-anchor updates it in place so the blotter row
            # reflects the new prices.
            placed.append((ch_trade, ch_key, ch_params))

            def _on_child_status(t, _oid=ch_frozen_oid, _p=ch_params):
                new_status = t.orderStatus.status
                self._emit_order_status(t, _oid, _p, new_status)
                order_log.info(
                    "[%s] ORDER-STATUS  %s | status=%s filled=%s "
                    "avgFill=%s symbol=%s account=%s orderRef=%s",
                    self._label, _oid, new_status,
                    t.orderStatus.filled, t.orderStatus.avgFillPrice,
                    _p.get("symbol", "?"), _p.get("account", ""),
                    getattr(t.order, "orderRef", ""))
                if new_status in ("Filled", "Completed"):
                    self._apply_fill_to_position(t, _p)
                if on_status is not None:
                    on_status(t)

            ch_trade.statusEvent += _on_child_status

            # Emit the child's initial status so the UI sees it right away.
            init_status = ch_trade.orderStatus.status or "Submitted"
            self._emit_order_status(ch_trade, ch_key, ch_params,
                                    init_status)
        return placed

    def _place_deferred_exits(self, contract, parent_trade, params: dict,
                              ref_price: float) -> int:
        """Place an overnight parent's exits after it fills in this session."""
        st = parent_trade.orderStatus
        return self._submit_deferred_exits(
            contract, parent_trade.order,
            int(abs(float(st.filled or 0))),
            float(getattr(st, "avgFillPrice", 0.0) or 0.0),
            params, ref_price)

    def _submit_deferred_exits(self, contract, parent, filled: int,
                               fill_px: float, params: dict,
                               ref_price: float) -> int:
        """Place a filled overnight parent's TP / SL / trail exits.

        IBEOS takes limit orders only, so ``_do_place_order`` held the
        exits back.  They go out here as standalone orders — no parentId,
        one OCA group so the first to fill cancels the rest — sized to
        *filled*, priced off the fill, and routed SMART with
        ``outsideRth`` (a TP limit works from the 04:00 pre-market; IB
        holds the stops until the regular session).  Overnight the take
        profit is twinned on IBEOS so it can fill tonight.  Returns how
        many exit orders were placed.
        """
        if filled <= 0:
            return 0
        anchor = fill_px if 0.0 < fill_px < _IB_DBL_MAX else ref_price
        if anchor <= 0:
            order_log.warning(
                "[%s] BRACKET-DEFER  no fill price — no exits placed for "
                "orderRef=%s", self._label, parent.orderRef)
            return 0
        shadow = copy.copy(parent)
        shadow.totalQuantity = filled
        shadow.outsideRth = True
        shadow.allowPreOpen = False
        children = self._build_bracket_children(
            shadow, params, anchor, float(params.get("tick_size") or 0.01))
        if not children:
            return 0
        oca = f"oca_{parent.orderRef}"
        for ch in children:
            ch.parentId = 0
            ch.transmit = True
            ch.ocaGroup = oca
            ch.ocaType = 1          # cancel the rest once one fills
        # Overnight, the take profit also goes to IBEOS so it can fill
        # tonight — IBEOS takes limits, just not stops.  It is a twin of
        # the SMART take profit in the same OCA group: IBEOS orders end
        # with the overnight session (~03:50 ET) and the SMART one, held
        # until 04:00, takes over from the pre-market.  The two venues'
        # hours don't overlap, and ocaType 1 blocks an overfill anyway;
        # a fill on either cancels everything else.
        overnight_tp = []
        if stk_routes_overnight(getattr(contract, "currency", "") or "USD"):
            for ch in children:
                if ch.orderType == "LMT":
                    twin = copy.copy(ch)
                    twin.orderId = self._ib.client.getReqId()
                    twin.orderRef = _generate_order_ref()
                    while twin.orderRef == ch.orderRef:
                        twin.orderRef = _generate_order_ref()
                    twin.outsideRth = False
                    twin.allowPreOpen = False
                    twin.advancedErrorOverride = "10329"
                    overnight_tp.append(twin)
        if overnight_tp:
            ibeos_contract = copy.copy(contract)
            ibeos_contract.exchange = "IBEOS"
            order_log.info(
                "[%s] BRACKET-DEFER  overnight take profit → IBEOS "
                "LMT %.4f oca=%s", self._label,
                overnight_tp[0].lmtPrice, oca)
            self._submit_exit_orders(ibeos_contract, overnight_tp, params,
                                     parent.orderRef)
        exit_contract = copy.copy(contract)
        exit_contract.exchange = "SMART"
        order_log.info(
            "[%s] BRACKET-DEFER  parent filled %d @ %.4f → %d exit(s) "
            "SMART oca=%s", self._label, filled, anchor, len(children), oca)
        self._submit_exit_orders(exit_contract, children, params,
                                 parent.orderRef)
        return len(children) + len(overnight_tp)

    def _build_tp_bundle(self, parent, contract, params: dict,
                         ref_price: float, tick: float) -> list:
        """The two take-profit children of an overnight TP-only bundle:
        ``[(IBEOS contract, tp), (SMART contract, tp twin)]``.

        Both attach to *parent* (same parentId), so IB OCA's them itself
        — no ocaGroup, which IB rejects on attached children.  The IBEOS
        one works tonight and ends with the overnight session; the SMART
        twin (``outsideRth``) is held until 04:00.  Only the last child
        transmits, flushing the bundle.
        """
        built = self._build_bracket_children(parent, params, ref_price, tick)
        night = next((ch for ch in built if ch.orderType == "LMT"), None)
        if night is None:
            return []
        night.transmit = False
        night.advancedErrorOverride = "10329"
        day = copy.copy(night)
        day.orderId = self._ib.client.getReqId()
        day.orderRef = _generate_order_ref()
        while day.orderRef == night.orderRef:
            day.orderRef = _generate_order_ref()
        day.outsideRth = True
        day.allowPreOpen = False
        day.advancedErrorOverride = ""
        day.transmit = True
        smart = copy.copy(contract)
        smart.exchange = "SMART"
        return [(contract, night), (smart, day)]

    # Error codes on a cancelled order that mean "the user (or our own
    # flow) cancelled it", not "TWS refused it".
    _CANCEL_CODES = (0, 202, 10147, 10148)

    def _maybe_retry_tp_bundle(self, t, parent_trade, params: dict,
                               placed_at: float, latch: list) -> None:
        """A part of an overnight TP bundle was refused at submission:
        cancel what is left of it and resend the parent alone — its take
        profit then follows the fill (deferred exits) instead.

        Only a refusal counts: within 30 s of placing, nothing filled,
        and the order's last log entry carries an error code other than
        a cancel acknowledgement — a user cancel, or the parent expiring
        unfilled at the end of the session, never resends.
        """
        if latch[0]:
            return
        status = getattr(t.orderStatus, "status", "") or ""
        if status not in ("Cancelled", "ApiCancelled", "Inactive"):
            return
        if time.monotonic() - placed_at > 30.0:
            return
        if float(getattr(parent_trade.orderStatus, "filled", 0) or 0) > 0:
            return
        entries = list(getattr(t, "log", None) or [])
        code = int(getattr(entries[-1], "errorCode", 0) or 0) if entries \
            else 0
        if code in self._CANCEL_CODES:
            return
        latch[0] = True
        parent = parent_trade.order
        order_log.warning(
            "[%s] BRACKET-BUNDLE  refused (error %s on orderRef=%s) — "
            "resending orderRef=%s alone, take profit after the fill",
            self._label, code, getattr(t.order, "orderRef", ""),
            parent.orderRef)
        parent_status = getattr(parent_trade.orderStatus, "status", "") or ""
        if parent_status not in _PARENT_DONE:
            try:
                self._ib.cancelOrder(parent)
            except Exception:
                order_log.exception(
                    "[%s] BRACKET-BUNDLE  cancel of orderRef=%s failed",
                    self._label, parent.orderRef)
        retry = dict(params)
        retry["_no_tp_bundle"] = True
        retry.pop("order_ref", None)
        asyncio.ensure_future(self._do_place_order(retry))

    # ── Pending overnight exits (fills seen after a restart / reconnect) ──

    @staticmethod
    def _exit_contract(params: dict):
        from ib_async import Contract
        c = Contract(conId=int(params.get("conid") or 0))
        c.exchange = "SMART"
        c.currency = params.get("currency") or "USD"
        c.symbol = params.get("symbol", "") or ""
        c.secType = params.get("sec_type", "") or "STK"
        c.localSymbol = params.get("local_symbol", "") or ""
        return c

    def _settle_pending_exits(self, key: str, filled: float,
                              fill_px: float, why: str) -> None:
        """A recorded overnight parent is done: place its exits for
        *filled* shares (or drop the record when nothing filled)."""
        from src.core import pending_exits
        rec = pending_exits.take(key)
        if rec is None:
            return
        params = rec.get("params") or {}
        sym = params.get("symbol", "?")
        account = rec.get("account", "")
        qty = int(abs(float(filled or 0)))
        if qty <= 0:
            order_log.info(
                "[%s] BRACKET-DEFER  %s ended unfilled (%s) — exits dropped",
                self._label, key, why)
            return
        parent = SimpleNamespace(
            action=params.get("action", "BUY"), totalQuantity=qty,
            account=account, tif=params.get("tif") or "DAY", orderId=0,
            orderRef=rec.get("order_ref", ""), outsideRth=True,
            allowPreOpen=False)
        n = self._submit_deferred_exits(
            self._exit_contract(params), parent, qty, float(fill_px or 0.0),
            params, float(rec.get("ref_price") or 0.0))
        order_log.info("[%s] BRACKET-DEFER  %s settled (%s) → %d exit(s)",
                       self._label, key, why, n)
        if n:
            self.exits_notice.emit(
                f"{sym} ({account}): filled {qty:,} while NanoPulse was "
                f"offline — its exits are now placed.")

    def _maybe_settle_pending_from_trade(self, trade) -> None:
        """Global order-status hook: a recorded overnight parent seen
        after a reconnect (no session closure) reaching a final state."""
        status = getattr(trade.orderStatus, "status", "") or ""
        if status not in _PARENT_DONE:
            return
        ref = getattr(trade.order, "orderRef", "") or ""
        account = getattr(trade.order, "account", "") or ""
        if not ref.startswith("nano_") or not account:
            return
        from src.core import pending_exits
        key = f"{account}_{ref}"
        if not pending_exits.has(key):
            return
        filled, px = self._parent_fill_totals(trade, account, ref)
        self._settle_pending_exits(key, filled, px, f"status {status}")

    def _parent_fill_totals(self, trade, account: str,
                            ref: str) -> tuple[float, float]:
        """(shares, avg price) a parent filled — its own status, or the
        execution reports when a replayed status carries no fill data."""
        filled = px = 0.0
        if trade is not None:
            filled = float(getattr(trade.orderStatus, "filled", 0) or 0)
            px = float(getattr(trade.orderStatus, "avgFillPrice", 0) or 0)
        shares = notional = 0.0
        try:
            for f in self._ib.fills():
                ex = f.execution
                if (getattr(ex, "orderRef", "") == ref
                        and getattr(ex, "acctNumber", "") == account):
                    shares += float(ex.shares or 0)
                    notional += float(ex.shares or 0) * float(ex.price or 0)
        except Exception:
            pass
        if shares > filled:
            filled = shares
            px = notional / shares if shares else 0.0
        return filled, px

    async def _reconcile_pending_exits(self) -> None:
        """On connect: settle every recorded overnight parent of this
        connection's accounts that finished while NanoPulse was away.

        Still-working parents are left for ``_on_global_order_status``.
        A record whose exits are already out (crash between placing and
        clearing) is just cleared.  One that can no longer be found —
        no open order, no fill in TWS's replay — is dropped after a day
        with a notice, since its outcome can't be verified any more.
        """
        if self._ib is None or not self._ib.isConnected():
            return
        from src.core import pending_exits
        recs = pending_exits.records()
        if not recs:
            return
        managed = set(self._ib.managedAccounts())
        trades = list(self._ib.trades())
        now = time.time()
        for key, rec in recs.items():
            account = rec.get("account", "")
            ref = rec.get("order_ref", "")
            if account not in managed or not ref:
                continue
            mine = [t for t in trades
                    if getattr(t.order, "account", "") == account]
            if any(getattr(t.order, "ocaGroup", "") == f"oca_{ref}"
                   for t in mine):
                pending_exits.take(key)
                continue
            parent = next((t for t in mine
                           if getattr(t.order, "orderRef", "") == ref), None)
            status = (getattr(parent.orderStatus, "status", "") or ""
                      if parent is not None else "")
            if parent is not None and status not in _PARENT_DONE:
                continue        # still working — the global hook settles it
            filled, px = self._parent_fill_totals(parent, account, ref)
            if filled > 0 or parent is not None:
                self._settle_pending_exits(key, filled, px, "reconnect")
            elif now - float(rec.get("created") or now) > 86400:
                pending_exits.take(key)
                sym = (rec.get("params") or {}).get("symbol", "?")
                order_log.warning(
                    "[%s] BRACKET-DEFER  %s not found in TWS after a day — "
                    "record dropped, exits NOT placed", self._label, key)
                self.exits_notice.emit(
                    f"{sym} ({account}): couldn't confirm whether the "
                    "overnight order filled — exits NOT placed, check the "
                    "position.")

    @staticmethod
    def _child_price_params(order_type: str, lmt_val, aux_val) -> dict:
        """Blotter price columns for a bracket child / exit order.

        LMT → limit only; STP → stop only; STP LMT → both; TRAIL →
        the trail *distance* (auxPrice) in the stop column — the Type
        column reads TRAIL so the cell is unambiguous (same convention
        TWS uses for the aux field).  IB's DBL_MAX "no data" sentinel
        is dropped.
        """
        def _ok(v) -> bool:
            return bool(v) and v < _IB_DBL_MAX
        out: dict = {}
        if order_type in ("LMT", "STP LMT") and _ok(lmt_val):
            out["limit_price"] = lmt_val
        if order_type in ("STP", "STP LMT", "TRAIL") and _ok(aux_val):
            out["stop_price"] = aux_val
        return out

    def _convert_mkt_parent_for_trail(self, order, params: dict,
                                      ref_price: float) -> bool:
        """Convert a MKT bracket parent to a marketable capped LMT.

        IB error 328: a TRAIL child can only attach to a LMT / STP LMT
        parent — on a MKT parent the trailing leg is auto-cancelled and,
        worse, the rejected (transmitting) child can strand the whole
        bracket.  The cap is the limit-cap preference when set, else 10
        ticks through the market — fills like a market order but
        bounded, and IB accepts the TRAIL.

        Mutates *order* (orderType/lmtPrice) and *params*
        (order_type/limit_price — blotter rows freeze `params`, so the
        converted parent must be reflected there too).  Returns True if
        the order was converted; False leaves both untouched (non-MKT
        parent, or no valid limit price could be built).
        """
        if getattr(order, "orderType", "") != "MKT":
            return False
        tick_sz = float(params.get("tick_size") or 0.01)
        cap = float(params.get("limit_cap_offset") or 0.0)
        if cap <= 0:
            cap = 10 * tick_sz
        sign = 1 if order.action == "BUY" else -1
        lmt_px = _round_to_tick(ref_price + sign * cap, tick_sz)
        if lmt_px <= 0:
            return False
        order.orderType = "LMT"
        order.lmtPrice = lmt_px
        params["order_type"] = "LMT"
        params["limit_price"] = lmt_px
        order_log.info(
            "[%s] BRACKET-TRAIL  MKT parent converted to marketable "
            "LMT @ %.4f (cap %.4f) — TRAIL children need a LMT/STP LMT "
            "parent (IB error 328), orderRef=%s",
            self._label, lmt_px, cap, getattr(order, "orderRef", ""))
        return True

    @staticmethod
    def _reanchored_child_prices(order_type: str, parent_action: str,
                                 params: dict, fill_price: float,
                                 tick: float) -> dict:
        """Absolute prices for a bracket child re-anchored to *fill_price*.

        Children are priced at submit time from the reference price (the
        limit price for resting LMT parents), so a fill with price
        improvement leaves them offset from the wrong anchor.  This
        recomputes the same tier math as ``_build_bracket_children``
        against the actual fill.  The child kind is inferred from its
        order type — the builder emits TP as LMT, SL as STP / STP LMT,
        and the trailing leg as TRAIL (already fill-relative → ``{}``).
        Returns a ``{attr: price}`` dict to apply to the child order, or
        ``{}`` when nothing applies / a price would go non-positive.
        """
        sign = 1 if parent_action == "BUY" else -1

        def _offset_points(tier: dict) -> float:
            v = float(tier.get("value", 0.0))
            return v * tick if tier.get("unit") == "ticks" else v

        if order_type == "LMT":
            tier = params.get("profit_take")
            if not tier:
                return {}
            px = _round_to_tick(fill_price + sign * _offset_points(tier),
                                tick)
            return {"lmtPrice": px} if px > 0 else {}

        if order_type in ("STP", "STP LMT"):
            tier = params.get("stop_loss")
            if not tier:
                return {}
            aux = _round_to_tick(fill_price - sign * _offset_points(tier),
                                 tick)
            if aux <= 0:
                return {}
            out = {"auxPrice": aux}
            if order_type == "STP LMT":
                cap = float(params.get("limit_cap_offset") or 0.0)
                lmt = _round_to_tick(aux - sign * cap, tick)
                if lmt <= 0:
                    return {}
                out["lmtPrice"] = lmt
            return out

        return {}

    def _reanchor_bracket_children(self, contract, parent_trade,
                                   children, ref_price: float,
                                   params: dict) -> None:
        """Re-price live TP / SL children to the parent's actual fill.

        Called once from the parent's statusEvent when it reaches
        Filled.  The bracket is transmitted whole before any fill
        exists, so the children were anchored to the submit-time
        reference; when the fill lands elsewhere (resting limit filled
        with price improvement, market moved), each still-working child
        is modified in place — same orderId, new price — which IB
        treats as an order modification and the OCA linkage survives.
        *children* is a list of ``(trade, order_key, blotter_params)``
        triples captured at placement.
        """
        fill = float(getattr(parent_trade.orderStatus,
                             "avgFillPrice", 0.0) or 0.0)
        if not (0.0 < fill < _IB_DBL_MAX):
            return
        tick = float(params.get("tick_size") or 0.01)
        if ref_price > 0 and abs(fill - ref_price) < tick / 2:
            return  # filled at the anchor — children already correct
        parent_action = getattr(parent_trade.order, "action", "BUY")

        for ch_trade, ch_key, ch_params in children:
            st = getattr(ch_trade.orderStatus, "status", "") or ""
            if st in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                continue
            ch_order = ch_trade.order
            new_px = self._reanchored_child_prices(
                ch_order.orderType, parent_action, params, fill, tick)
            if not new_px:
                continue
            if all(abs(float(getattr(ch_order, a, 0.0) or 0.0) - v) < 1e-9
                   for a, v in new_px.items()):
                continue
            for a, v in new_px.items():
                setattr(ch_order, a, v)
            # Modify = re-place with the same orderId; each modify
            # transmits on its own (the bundle-level staging only
            # applies to the initial submission).
            ch_order.transmit = True
            # A TP bundle's children sit on different venues — modify
            # each on the contract it was placed with.
            self._ib.placeOrder(
                getattr(ch_trade, "contract", None) or contract, ch_order)
            # Refresh the blotter's frozen price columns and push the
            # row so the UI shows the re-anchored prices immediately.
            ch_params.pop("limit_price", None)
            ch_params.pop("stop_price", None)
            ch_params.update(self._child_price_params(
                ch_order.orderType,
                getattr(ch_order, "lmtPrice", None),
                getattr(ch_order, "auxPrice", None)))
            self._emit_order_status(ch_trade, ch_key, ch_params, st)
            order_log.info(
                "[%s] BRACKET-REANCHOR  %s child re-priced to fill "
                "%.4f (submit ref %.4f): %s, orderRef=%s",
                self._label, ch_order.orderType, fill, ref_price,
                new_px, getattr(ch_order, "orderRef", ""))

    def _build_bracket_children(self, parent, params: dict,
                                ref_price: float, tick: float) -> list:
        """Construct attached TP / SL / Trail-stop children for *parent*.

        Each child reverses the parent's action, shares its quantity and
        account, gets a fresh unique ``orderId`` from the IB client and a
        fresh ``orderRef`` (suffix-based refs would break the 4-part parser
        used by the blotter).  All children except the last have
        ``transmit=False``; the last has ``transmit=True`` so IB activates
        the entire bundle atomically.

        When both stop-loss and trailing are enabled, they are placed in a
        common OCA group so one firing cancels the other — TP auto-OCAs
        against whichever loss-side exit fills via the shared parentId.
        """
        from ib_async import LimitOrder, StopOrder, StopLimitOrder, Order

        exit_action = "SELL" if parent.action == "BUY" else "BUY"
        sign = 1 if parent.action == "BUY" else -1
        qty = parent.totalQuantity
        is_stk = params.get("sec_type") == "STK"
        cap_offset = float(params.get("limit_cap_offset") or 0.0)

        def _offset_points(tier: dict) -> float:
            v = float(tier.get("value", 0.0))
            return v * tick if tier.get("unit") == "ticks" else v

        built: list[tuple[str, object]] = []

        tp_tier = params.get("profit_take")
        if tp_tier:
            px = _round_to_tick(ref_price + sign * _offset_points(tp_tier),
                                tick)
            if px > 0:
                built.append(("tp", LimitOrder(exit_action, qty, px)))
            else:
                order_log.warning(
                    "[%s] TP-SKIP  non-positive price %.4f (ref=%.4f)",
                    self._label, px, ref_price)

        sl_tier = params.get("stop_loss")
        if sl_tier:
            # auxPrice is the stop trigger — what traders and TWS call
            # "the stop price".  The chip's offset applies here so the
            # displayed stop matches the user's intent (chip 10 ⇒ stop
            # 10 pts from ref).  When a cap_offset is configured we
            # upgrade to STP LMT and place the limit cap_offset *further
            # into the loss zone* from the stop, giving the resulting
            # limit order a small execution window.
            #   BUY parent  (SELL STP LMT): aux > lmt  (stop above floor)
            #   SELL parent (BUY  STP LMT): aux < lmt  (stop below ceiling)
            aux_px = _round_to_tick(
                ref_price - sign * _offset_points(sl_tier), tick)
            if aux_px > 0:
                if cap_offset > 0:
                    lmt_px = _round_to_tick(aux_px - sign * cap_offset, tick)
                    if lmt_px > 0:
                        built.append(("sl", StopLimitOrder(
                            exit_action, qty, lmt_px, aux_px)))
                    else:
                        order_log.warning(
                            "[%s] SL-SKIP  non-positive lmt %.4f "
                            "(stop=%.4f cap=%.4f)", self._label,
                            lmt_px, aux_px, cap_offset)
                else:
                    # No cap preference configured → plain stop-market.
                    built.append(("sl", StopOrder(exit_action, qty, aux_px)))
            else:
                order_log.warning(
                    "[%s] SL-SKIP  non-positive stop %.4f (ref=%.4f)",
                    self._label, aux_px, ref_price)

        tr_tier = params.get("trailing")
        if tr_tier:
            aux = _round_to_tick(_offset_points(tr_tier), tick)
            if aux > 0:
                built.append(("tr", Order(
                    orderType="TRAIL",
                    action=exit_action,
                    totalQuantity=qty,
                    auxPrice=aux,
                )))
            else:
                order_log.warning(
                    "[%s] TRAIL-SKIP  non-positive offset %.4f", self._label,
                    aux)

        if not built:
            return []

        # No explicit ocaGroup: attached children (shared parentId) are
        # already OCA'd by IB at the bracket level — when any one fills,
        # TWS auto-cancels the siblings.  Setting an ocaGroup on top of
        # that is redundant AND rejected by IBKR as "OCA handling method
        # mismatch" (confirmed against paper-account error 201).

        parent_outside_rth = getattr(parent, "outsideRth", False)
        parent_allow_pre = getattr(parent, "allowPreOpen", False)

        for _tag, ch in built:
            ch.parentId = parent.orderId
            ch.orderId = self._ib.client.getReqId()
            ch.account = parent.account
            ch.tif = parent.tif
            ch.orderRef = _generate_order_ref()
            ch.transmit = False
            if is_stk:
                ch.outsideRth = parent_outside_rth
                ch.allowPreOpen = parent_allow_pre
            elif ch.orderType in ("STP", "STP LMT", "TRAIL"):
                # Futures: STP / STP LMT / TRAIL orders are held by TWS
                # until the next RTH open by default (Warning 399 /
                # "placed at the exchange until 08:30 US/Central").  For
                # CME products that trade ~23 hours this leaves a stop
                # dormant overnight — long enough for the market to blow
                # past it before the flag un-holds.  outsideRth=True keeps
                # the child armed during extended hours.  Plain LMT
                # children are exempt: CME ignores the attribute for
                # them and emits Warning 2109, which ib_async surfaces
                # as a scary "ValidationError" status in the blotter.
                # Stocks keep their RTH semantics (parent_outside_rth).
                ch.outsideRth = True

        # Last child flushes the bundle to TWS.
        built[-1][1].transmit = True
        return [ch for _, ch in built]

    async def _do_cancel_order(self, order_id: str) -> None:
        """Cancel an open order by its *order_id*.

        Uses a multi-tier fallback strategy so that cancel works even when
        the order wasn't captured by the initial snapshot (e.g. TWS was
        still syncing after a restart when the snapshot ran).
        """
        trade = self._trades.get(order_id)

        # ── Fallback 1: search ib_async's local open-trade cache ──────────
        # The order may exist in ib_async's collection (delivered via
        # reqAutoOpenOrders) but not yet in our _trades dict.
        if trade is None and self._ib is not None and self._ib.isConnected():
            # Extract orderRef: "L-paper-ib_nano_0331_..." → "nano_0331_..."
            parts = order_id.split("_", 1)
            target_ref = parts[1] if len(parts) == 2 else ""
            if target_ref.startswith("nano_"):
                for t in self._ib.openTrades():
                    ref = getattr(t.order, "orderRef", "") or ""
                    if ref == target_ref:
                        trade = t
                        self._trades[order_id] = trade
                        log.info("Worker %s: found %s via local orderRef lookup",
                                 self._label, order_id)
                        break

        # ── Fallback 2: fresh request to TWS ──────────────────────────────
        # If TWS was still booting when we reconnected, this picks up
        # orders that have since become available.
        if trade is None and self._ib is not None and self._ib.isConnected():
            try:
                open_trades = await self._ib.reqAllOpenOrdersAsync()
                parts = order_id.split("_", 1)
                target_ref = parts[1] if len(parts) == 2 else ""
                for t in open_trades:
                    ref = getattr(t.order, "orderRef", "") or ""
                    if target_ref.startswith("nano_") and ref == target_ref:
                        trade = t
                        self._trades[order_id] = trade
                        log.info("Worker %s: found %s via fresh TWS request",
                                 self._label, order_id)
                        break
            except Exception:
                log.debug("Worker %s: cancel fallback reqAllOpenOrders failed",
                          self._label, exc_info=True)

        if trade is None:
            log.warning("Worker %s: cancel_order – unknown order_id %s",
                        self._label, order_id)
            order_log.warning("[%s] ORDER-CANCEL  FAILED – unknown order_id %s",
                              self._label, order_id)
            return

        # ── Status guard (two-tier) ───────────────────────────────────────────
        #
        # The race condition: our local state is a cache updated asynchronously
        # via orderStatusEvent.  If TWS fills the order while the fill event is
        # still in transit, we may read "Submitted" and fire a cancel that TWS
        # will reject with error 202.  Error 202 is handled gracefully in
        # _on_ib_error (log-only, no tooltip); the fill event arrives moments
        # later and updates the UI row correctly.  A TWS round-trip
        # (reqAllOpenOrdersAsync) would eliminate the race but adds 50-200 ms
        # latency — unacceptable for a fast cancel button.
        #
        # Tier 1 — ib_async openTrades() (most authoritative local source):
        #   ib_async removes a trade from openTrades() the moment it processes
        #   a terminal status event, so this is the best local signal we have.
        _open_ids = {getattr(t.order, "orderId", None)
                     for t in self._ib.openTrades()}
        _order_ib_id = getattr(trade.order, "orderId", None)
        if _order_ib_id is not None and _order_ib_id not in _open_ids:
            _status = (getattr(trade.orderStatus, "status", "?")
                       if getattr(trade, "orderStatus", None) else "?")
            log.warning("Worker %s: cancel_order skipped for %s — orderId=%s "
                        "not in ib.openTrades() (last known status=%s)",
                        self._label, order_id, _order_ib_id, _status)
            order_log.warning("[%s] ORDER-CANCEL  SKIPPED – %s not in "
                              "openTrades() (status=%s)",
                              self._label, order_id, _status)
            return

        # Tier 2 — cached trade.orderStatus.status (same race window as tier 1,
        #   but catches orders whose orderId we couldn't resolve above):
        _status = (getattr(trade.orderStatus, "status", "")
                   if getattr(trade, "orderStatus", None) else "")
        if _status in ("Filled", "Cancelled", "Canceled",
                       "Inactive", "ApiCancelled", "Completed"):
            log.warning("Worker %s: cancel_order skipped for %s – "
                        "status is already '%s'", self._label, order_id, _status)
            order_log.warning("[%s] ORDER-CANCEL  SKIPPED – %s already '%s'",
                              self._label, order_id, _status)
            return

        try:
            self._ib.cancelOrder(trade.order)
            log.info("Worker %s: cancel requested for order %s",
                     self._label, order_id)
            tws_log.info("[%s] API-CALL  cancelOrder(orderId=%s) for key=%s",
                         self._label, trade.order.orderId, order_id)
            order_log.info("[%s] ORDER-CANCEL  requested for %s | orderId=%s "
                           "symbol=%s orderRef=%s",
                           self._label, order_id, trade.order.orderId,
                           getattr(trade.contract, "symbol", "?"),
                           getattr(trade.order, "orderRef", ""))
            asyncio.create_task(
                self._force_cancel_if_stuck(order_id, trade, _status))
        except Exception as exc:
            log.exception("Worker %s: cancel_order failed: %s",
                          self._label, exc)
            tws_log.error("[%s] API-ERR   cancelOrder failed for %s: %s",
                          self._label, order_id, exc)
            order_log.error("[%s] ORDER-CANCEL  ERROR for %s: %s",
                            self._label, order_id, exc)

    # Order statuses meaning "held at IB, not working at an exchange".
    # Outside RTH a new order sits PreSubmitted at IB (TWS warning 399:
    # "will not be placed at the exchange until <open>").
    _HELD_STATUSES = ("PreSubmitted", "PendingSubmit")

    async def _force_cancel_if_stuck(self, order_id: str, trade,
                                     pre_status: str,
                                     delay: float = 3.0) -> None:
        """Re-issue cancelOrder when a held order's cancel stalls.

        For an order held at IB (outside RTH), TWS answers the first
        cancelOrder with PendingCancel only and defers the actual
        cancellation to the exchange open; a SECOND cancelOrder while
        PendingCancel makes TWS cancel the order locally right away —
        the API equivalent of the user clicking Cancel twice.  Only
        orders that were held (no exchange exposure) are retried:
        force-cancelling an exchange-working order locally could mask a
        late fill, which is exactly why TWS prompts for confirmation in
        that case.
        """
        if pre_status not in self._HELD_STATUSES:
            return
        await asyncio.sleep(delay)
        if self._ib is None or not self._ib.isConnected():
            return
        status = (getattr(trade.orderStatus, "status", "")
                  if getattr(trade, "orderStatus", None) else "")
        if status != "PendingCancel":
            return  # cancel completed (or order filled) — nothing to do
        open_ids = {getattr(t.order, "orderId", None)
                    for t in self._ib.openTrades()}
        if getattr(trade.order, "orderId", None) not in open_ids:
            return
        try:
            self._ib.cancelOrder(trade.order)
            log.info("Worker %s: forcing local cancel for held order %s "
                     "(stuck PendingCancel)", self._label, order_id)
            order_log.info("[%s] ORDER-CANCEL  re-issued for %s — held "
                           "order stuck PendingCancel (local cancel)",
                           self._label, order_id)
        except Exception as exc:
            log.warning("Worker %s: forced cancel failed for %s: %s",
                        self._label, order_id, exc)

    def _trade_matches_symbol(self, trade, symbol_upper: str) -> bool:
        """True when *trade* is for *symbol_upper* (root or localSymbol).

        Option orders are placed on a lean conId-only contract (TWS
        error 478 workaround), so the ib_async Trade carries no
        symbol/localSymbol and ``_symbol_matches`` can never hit — fall
        back to the display symbol cached at placement time, keyed by
        orderRef (same cache the blotter uses).
        """
        contract = getattr(trade, "contract", None)
        if contract is None:
            return False
        if _symbol_matches(contract, symbol_upper):
            return True
        ref = getattr(trade.order, "orderRef", "") or ""
        cached = (self._order_symbols.get(ref, "") or "").upper()
        return bool(cached) and cached == symbol_upper

    async def _do_cancel_all_for_symbol(self, symbol: str,
                                        accounts: set[str]) -> None:
        """Cancel every open order for *symbol* whose account is in *accounts*."""
        if self._ib is None or not self._ib.isConnected():
            return
        symbol_upper = symbol.upper()
        cancelled = 0

        # Build the authoritative open-order set once from ib_async's own
        # tracking.  Same race window as trade.orderStatus.status (both are
        # local caches), but openTrades() is ib_async's primary source of truth
        # and is updated immediately when a terminal status event is processed.
        open_ids: set = {getattr(t.order, "orderId", None)
                         for t in self._ib.openTrades()}

        for oid, trade in list(self._trades.items()):
            if not self._trade_matches_symbol(trade, symbol_upper):
                continue
            # ── Account filter ────────────────────────────────────────────
            trade_acct = getattr(trade.order, "account", "") or ""
            if trade_acct not in accounts:
                continue
            # ── Status guard tier 1: ib_async openTrades() ───────────────
            ib_id = getattr(trade.order, "orderId", None)
            if ib_id is not None and ib_id not in open_ids:
                continue
            # ── Status guard tier 2: cached orderStatus ───────────────────
            status = (getattr(trade.orderStatus, "status", "")
                      if getattr(trade, "orderStatus", None) else "")
            if status in ("Filled", "Cancelled", "Canceled",
                          "Inactive", "ApiCancelled", "Completed"):
                continue
            try:
                self._ib.cancelOrder(trade.order)
                cancelled += 1
                tws_log.info("[%s] API-CALL  cancelOrder(orderId=%s) [cancel_all_for_symbol '%s']",
                             self._label, trade.order.orderId, symbol)
                order_log.info("[%s] ORDER-CANCEL  %s (cancel-all for %s) | orderId=%s",
                               self._label, oid, symbol, trade.order.orderId)
                asyncio.create_task(
                    self._force_cancel_if_stuck(oid, trade, status))
            except Exception as exc:
                log.warning("Worker %s: cancel_all_for_symbol failed for %s: %s",
                            self._label, oid, exc)
                order_log.error("[%s] ORDER-CANCEL  ERROR for %s (cancel-all %s): %s",
                                self._label, oid, symbol, exc)
        log.info("Worker %s: cancel_all_for_symbol '%s' accounts=%s → cancelled %d order(s)",
                 self._label, symbol,
                 sorted(accounts) if accounts else "all", cancelled)
        order_log.info("[%s] ORDER-CANCEL-ALL  symbol=%s accounts=%s → %d order(s) cancelled",
                       self._label, symbol,
                       sorted(accounts) if accounts else "all", cancelled)

    async def _do_cancel_all_for_account(self, account: str) -> None:
        """Cancel every open order for *account* regardless of symbol."""
        if self._ib is None or not self._ib.isConnected():
            return
        cancelled = 0

        # Build the authoritative open-order set once from ib_async's own
        # tracking.  Same race window as trade.orderStatus.status (both are
        # local caches), but openTrades() is ib_async's primary source of truth
        # and is updated immediately when a terminal status event is processed.
        open_ids: set = {getattr(t.order, "orderId", None)
                         for t in self._ib.openTrades()}

        for oid, trade in list(self._trades.items()):
            # ── Account filter ────────────────────────────────────────────
            trade_acct = getattr(trade.order, "account", "") or ""
            if trade_acct != account:
                continue
            # ── Status guard tier 1: ib_async openTrades() ───────────────
            ib_id = getattr(trade.order, "orderId", None)
            if ib_id is not None and ib_id not in open_ids:
                continue
            # ── Status guard tier 2: cached orderStatus ───────────────────
            status = (getattr(trade.orderStatus, "status", "")
                      if getattr(trade, "orderStatus", None) else "")
            if status in ("Filled", "Cancelled", "Canceled",
                          "Inactive", "ApiCancelled", "Completed"):
                continue
            try:
                self._ib.cancelOrder(trade.order)
                cancelled += 1
                tws_log.info("[%s] API-CALL  cancelOrder(orderId=%s) [cancel_all_for_account '%s']",
                             self._label, trade.order.orderId, account)
                order_log.info("[%s] ORDER-CANCEL  %s (cancel-all for account %s) | orderId=%s",
                               self._label, oid, account, trade.order.orderId)
            except Exception as exc:
                log.warning("Worker %s: cancel_all_for_account failed for %s: %s",
                            self._label, oid, exc)
                order_log.error("[%s] ORDER-CANCEL  ERROR for %s (cancel-all account %s): %s",
                                self._label, oid, account, exc)
        log.info("Worker %s: cancel_all_for_account '%s' → cancelled %d order(s)",
                 self._label, account, cancelled)
        order_log.info("[%s] ORDER-CANCEL-ALL  account=%s → %d order(s) cancelled",
                       self._label, account, cancelled)

    async def _do_flatten(self, symbol: str, account: str) -> None:
        """Cancel all open orders for *symbol*/*account*, then signal the UI
        to close the position via the normal order-placement path (which
        applies limit-price caps, RTH routing, trading preferences, etc.).

        Step 1 delegates to ``_do_cancel_all_for_symbol`` (reuses the
        two-tier status guard and account filter).
        Step 2 reads the current position and emits ``flatten_close_needed``
        so the UI thread routes the close order through the same logic as
        Buy/Sell @MKT.
        """
        if self._ib is None or not self._ib.isConnected():
            return

        symbol_upper = symbol.upper()

        # ── Step 1: cancel open orders ────────────────────────────────────────
        await self._do_cancel_all_for_symbol(symbol, {account})

        # Brief pause so cancel ACKs arrive before reading positions.
        await asyncio.sleep(0.1)

        # ── Step 2: read position and signal UI to close ──────────────────────
        try:
            positions = self._ib.positions()
        except Exception:
            log.warning(
                "Worker %s: flatten could not read positions", self._label)
            return

        for pos in positions:
            if getattr(pos, "account", "") != account:
                continue
            contract = pos.contract
            if not _symbol_matches(contract, symbol_upper):
                continue

            net_pos = float(getattr(pos, "position", 0))
            if net_pos == 0:
                continue

            action = OrderAction.SELL if net_pos > 0 else OrderAction.BUY
            qty = abs(int(net_pos))

            order_log.info("[%s] ORDER-FLATTEN  %s %s %d | account=%s",
                           self._label, action, symbol, qty, account)

            # Emit to UI thread — the UI will route through the normal
            # order-placement path (limit cap, RTH, prefs, etc.).  Include
            # the position's contract so the UI can build the order without
            # depending on the saved-contracts DB matching the open position
            # (the user may have rolled their saved row to a later expiry).
            self.flatten_close_needed.emit({
                "symbol":  symbol,
                "account": account,
                "action":  action,
                "qty":     qty,
                "_resolved_contract": _contract_to_dict(contract),
            })

    async def _do_reverse(self, symbol: str, account: str) -> None:
        """Cancel all open orders for *symbol*/*account*, then signal the UI
        to reverse (flip) the position via the normal order-placement path.

        Reverse means: if currently Long 5, send SELL 10 → end up Short 5.
        If currently Short 3, send BUY 6 → end up Long 3.
        Qty = 2 × abs(position).  If there is no position, nothing is sent.

        Step 1 delegates to ``_do_cancel_all_for_symbol``.
        Step 2 reads the current position and emits ``reverse_close_needed``
        so the UI thread routes the order through the same logic as
        Buy/Sell @MKT (limit cap, RTH, prefs, etc.).
        """
        if self._ib is None or not self._ib.isConnected():
            return

        symbol_upper = symbol.upper()

        # ── Step 1: cancel open orders ────────────────────────────────────────
        await self._do_cancel_all_for_symbol(symbol, {account})

        # Brief pause so cancel ACKs arrive before reading positions.
        await asyncio.sleep(0.1)

        # ── Step 2: read position and signal UI to reverse ────────────────────
        try:
            positions = self._ib.positions()
        except Exception:
            log.warning(
                "Worker %s: reverse could not read positions", self._label)
            return

        for pos in positions:
            if getattr(pos, "account", "") != account:
                continue
            contract = pos.contract
            if not _symbol_matches(contract, symbol_upper):
                continue

            net_pos = float(getattr(pos, "position", 0))
            if net_pos == 0:
                continue

            # Opposite direction, double quantity to flip through zero.
            action = OrderAction.SELL if net_pos > 0 else OrderAction.BUY
            qty = 2 * abs(int(net_pos))

            order_log.info("[%s] ORDER-REVERSE  %s %s %d (pos=%+d) | account=%s",
                           self._label, action, symbol, qty,
                           int(net_pos), account)

            # Emit to UI thread — the UI will route through the normal
            # order-placement path (limit cap, RTH, prefs, etc.).  See
            # _do_flatten for why _resolved_contract is included.
            self.reverse_close_needed.emit({
                "symbol":  symbol,
                "account": account,
                "action":  action,
                "qty":     qty,
                "_resolved_contract": _contract_to_dict(contract),
            })

    async def _do_close_pct(self, symbol: str, account: str, pct: int) -> None:
        """Close *pct* percent of the current position for *symbol*/*account*.

        No open orders are cancelled — only a partial offsetting MKT order
        is sent.  Emits ``flatten_close_needed`` (same shape as flatten)
        so the UI routes it through the normal order-placement path with
        limit-price caps, RTH routing, etc.

        The close quantity is ``math.ceil(abs(position) * pct / 100)``,
        rounded up to prioritise reducing risk, but never more than the
        full position size.  If the result is 0, nothing is sent.
        """
        if self._ib is None or not self._ib.isConnected():
            return

        symbol_upper = symbol.upper()

        try:
            positions = self._ib.positions()
        except Exception:
            log.warning(
                "Worker %s: close_pct could not read positions", self._label)
            return

        for pos in positions:
            if getattr(pos, "account", "") != account:
                continue
            contract = pos.contract
            if not _symbol_matches(contract, symbol_upper):
                continue

            net_pos = float(getattr(pos, "position", 0))
            if net_pos == 0:
                continue

            abs_pos = abs(int(net_pos))
            action = OrderAction.SELL if net_pos > 0 else OrderAction.BUY
            # ceil → always round up to prioritise taking profit / reducing risk
            qty = math.ceil(abs_pos * pct / 100.0)
            # Safety: never close more than you actually hold
            qty = min(qty, abs_pos)
            if qty <= 0:
                continue

            order_log.info("[%s] ORDER-CLOSE-%d%%  %s %s %d (pos=%+d) | account=%s",
                           self._label, pct, action, symbol, qty,
                           int(net_pos), account)

            # Reuse flatten_close_needed — same data shape, same UI handling.
            # See _do_flatten for why _resolved_contract is included.
            self.flatten_close_needed.emit({
                "symbol":  symbol,
                "account": account,
                "action":  action,
                "qty":     qty,
                "_resolved_contract": _contract_to_dict(contract),
            })

    async def _do_close_tier(self, symbol: str, account: str, side: str,
                             offset_pts: float, requested_qty: int,
                             current_price: float,
                             cap_offset: float) -> None:
        """Read the position for *symbol*/*account* and submit a single
        LMT (tp) / STP / STP LMT (sl) / TRAIL (tr) exit order.

        Sizing: ``qty = min(|position|, requested_qty)``.  A zero-position
        account is silently skipped so we never submit an empty order.
        No open orders are cancelled — this is strictly additive.

        TP price:
          long  (pos>0): target = current + offset   (sell above current)
          short (pos<0): target = current - offset   (buy below current)

        SL trigger (auxPrice — "the stop price" TWS displays):
          long  (pos>0): stop   = current - offset   (sell below current)
          short (pos<0): stop   = current + offset
        And when ``cap_offset > 0`` the limit sits that far further
        into the loss zone from the stop, giving the post-trigger limit
        a small execution window.

        TS trail (auxPrice — the trail *distance*, not a level):
          aux = offset (unsigned; IB tracks the stop server-side at
          high-water ∓ aux).  ``current_price`` and ``cap_offset`` play
          no role in the maths — the price guard just mirrors tp/sl.
        """
        if self._ib is None or not self._ib.isConnected():
            return
        if (side not in ("tp", "sl", "tr")
                or offset_pts <= 0 or current_price <= 0):
            order_log.warning(
                "[%s] ORDER-CLOSE-TIER  invalid args side=%s offset=%.4f "
                "ref=%.4f — skipping", self._label, side, offset_pts,
                current_price)
            return

        from ib_async import LimitOrder, Order, StopLimitOrder, StopOrder

        symbol_upper = symbol.upper()
        try:
            positions = self._ib.positions()
        except Exception:
            log.warning(
                "Worker %s: close_tier could not read positions", self._label)
            return

        for pos in positions:
            if getattr(pos, "account", "") != account:
                continue
            contract = pos.contract
            if not _symbol_matches(contract, symbol_upper):
                continue

            net_pos = float(getattr(pos, "position", 0))
            if net_pos == 0:
                continue

            abs_pos = abs(int(net_pos))
            req = max(0, int(requested_qty))
            qty = min(abs_pos, req) if req > 0 else abs_pos
            if qty <= 0:
                continue

            is_long = net_pos > 0
            exit_action = "SELL" if is_long else "BUY"
            sign = 1 if is_long else -1

            sec_type = getattr(contract, "secType", "") or ""
            tick = self._tick_size_for_contract(contract)

            # ib.positions() returns contracts without `exchange` populated
            # (among other routing fields), which causes placeOrder to fail
            # with Warning 321 "Missing order exchange".  The exchange lives
            # on the chip's instrument entry already — look it up locally
            # instead of doing a round-trip to TWS.
            if not getattr(contract, "exchange", ""):
                from src.core import instruments
                cached = instruments.get_contract(symbol_upper)
                if cached and cached.get("exchange"):
                    contract.exchange = cached["exchange"]
                else:
                    order_log.warning(
                        "[%s] ORDER-CLOSE-TIER  no exchange for %s in "
                        "local contracts — IBKR will reject the order",
                        self._label, symbol)

            if side == "tp":
                # Profit direction: long → up, short → down.
                target = _round_to_tick(
                    current_price + sign * offset_pts, tick)
                if target <= 0:
                    order_log.warning(
                        "[%s] ORDER-CLOSE-TIER TP-SKIP  non-positive target "
                        "%.4f (ref=%.4f offset=%.4f)",
                        self._label, target, current_price, offset_pts)
                    continue
                order = LimitOrder(exit_action, qty, target)
                tp_desc = f"LMT @ {target:.4f}"
            elif side == "tr":
                # Trailing stop: auxPrice is the unsigned trail DISTANCE
                # — IB tracks the stop level server-side.
                aux = _round_to_tick(offset_pts, tick)
                if aux <= 0:
                    order_log.warning(
                        "[%s] ORDER-CLOSE-TIER TR-SKIP  non-positive "
                        "trail %.4f", self._label, aux)
                    continue
                order = Order(orderType="TRAIL", action=exit_action,
                              totalQuantity=qty, auxPrice=aux)
                tp_desc = f"TRAIL dist={aux:.4f}"
            else:  # side == "sl"
                # Loss direction: long → down, short → up.
                stop_px = _round_to_tick(
                    current_price - sign * offset_pts, tick)
                if stop_px <= 0:
                    order_log.warning(
                        "[%s] ORDER-CLOSE-TIER SL-SKIP  non-positive stop "
                        "%.4f (ref=%.4f offset=%.4f)",
                        self._label, stop_px, current_price, offset_pts)
                    continue
                if cap_offset > 0:
                    lmt_px = _round_to_tick(
                        stop_px - sign * cap_offset, tick)
                    if lmt_px <= 0:
                        order_log.warning(
                            "[%s] ORDER-CLOSE-TIER SL-SKIP  non-positive "
                            "lmt %.4f (stop=%.4f cap=%.4f)",
                            self._label, lmt_px, stop_px, cap_offset)
                        continue
                    order = StopLimitOrder(exit_action, qty, lmt_px, stop_px)
                    tp_desc = f"STP LMT stop={stop_px:.4f} lmt={lmt_px:.4f}"
                else:
                    order = StopOrder(exit_action, qty, stop_px)
                    tp_desc = f"STP @ {stop_px:.4f}"

            order.account = account
            order.tif = "DAY"
            order.orderRef = _generate_order_ref()
            # Mirror STK RTH handling used by the main place_order path so
            # exit orders don't get rejected after hours.
            if sec_type == "STK":
                exch = getattr(contract, "exchange", "")
                if exch == "IBEOS":
                    order.advancedErrorOverride = "10329"
                else:
                    now_et = datetime.now(_ET)
                    h, m = now_et.hour, now_et.minute
                    in_rth = (h > 9 or (h == 9 and m >= 30)) and h < 16
                    order.outsideRth = not in_rth
                    order.allowPreOpen = False
            else:
                # Futures: keep STP / STP LMT exits armed during extended
                # hours.  Without this flag TWS holds the order until the
                # next 08:30 CT open, so a stop placed overnight won't
                # trigger even if the price reaches it.  Safe for LMT
                # exits too — CME honours outsideRth=True across all
                # order types.  See _build_bracket_children for the
                # matching logic on attached bracket children.
                order.outsideRth = True

            order_log.info(
                "[%s] ORDER-CLOSE-TIER  side=%s %s %s %d (pos=%+d, "
                "req=%d) | account=%s orderRef=%s %s",
                self._label, side.upper(), exit_action, symbol, qty,
                int(net_pos), req, account, order.orderRef, tp_desc)

            trade = self._ib.placeOrder(contract, order)
            order_id = f"{account}_{order.orderRef}"
            self._trades[order_id] = trade
            self._has_status_handler.add(order_id)

            tws_log.info(
                "[%s] API-RESP  placeOrder(close-tier) → "
                "orderId=%s orderRef=%s",
                self._label, trade.order.orderId, order.orderRef)

            status_params = {
                "symbol":   symbol,
                "account":  account,
                "action":   exit_action,
                "qty":      qty,
                "conid":    int(getattr(contract, "conId", 0) or 0),
                "sec_type": sec_type,
                "order_type": order.orderType,
            }
            if side == "tp":
                status_params["limit_price"] = target
            elif side == "tr":
                # The Type column reads TRAIL, so the STP cell
                # unambiguously shows the trail distance (same
                # convention TWS uses for the aux field).
                status_params["stop_price"] = aux
            elif cap_offset > 0:
                status_params["stop_price"] = stop_px
                status_params["limit_price"] = lmt_px
            else:
                status_params["stop_price"] = stop_px

            init_status = trade.orderStatus.status or "Submitted"
            self._emit_order_status(trade, order_id, status_params,
                                    init_status)

            frozen_oid = order_id
            frozen_params = dict(status_params)

            def _on_tier_status(t, _oid=frozen_oid, _p=frozen_params):
                new_status = t.orderStatus.status
                self._emit_order_status(t, _oid, _p, new_status)
                order_log.info(
                    "[%s] ORDER-STATUS  %s | status=%s filled=%s "
                    "avgFill=%s symbol=%s account=%s orderRef=%s",
                    self._label, _oid, new_status,
                    t.orderStatus.filled, t.orderStatus.avgFillPrice,
                    _p.get("symbol", "?"), _p.get("account", ""),
                    getattr(t.order, "orderRef", ""))
                if new_status in ("Filled", "Completed"):
                    self._apply_fill_to_position(t, _p)

            trade.statusEvent += _on_tier_status

    def _tick_size_for_contract(self, contract) -> float:
        """Best-effort minimum-tick lookup for a Contract object.

        Uses ``minTick`` if TWS populated it, otherwise falls back to a
        coarse secType-based default (FUT ⇒ 0.25, everything else ⇒ 0.01).
        Centralised here so close_tier can round child prices to the grid
        without needing the UI-side _tick_size_for_symbol helper.
        """
        for attr in ("minTick", "tick_size", "min_tick"):
            val = getattr(contract, attr, None)
            if val:
                try:
                    v = float(val)
                    if v > 0:
                        return v
                except (TypeError, ValueError):
                    pass
        sec = getattr(contract, "secType", "") or ""
        return 0.25 if sec == "FUT" else 0.01

    def _on_trade_status(self, trade, order_id: str, params: dict) -> None:
        """Callback fired by trade.statusEvent – runs in the worker's event loop.

        Emits ``order_status_updated`` Signal to safely pass data to the GUI thread.
        """
        status = trade.orderStatus.status
        log.info("Worker %s: order %s status → %s",
                 self._label, order_id, status)
        self._emit_order_status(trade, order_id, params, status)

    def _emit_order_status(self, trade, order_id: str, params: dict,
                           status: str, *, supersedes: str | None = None) -> None:
        """Build the status dict and emit the signal.

        Parameters
        ----------
        supersedes:
            If given, the old ``order_id`` that this entry replaces
            (orderId → permId migration).  The UI should remove the
            stale row before upserting the new one.
        """
        fill_price = "–"
        avg = getattr(trade.orderStatus, "avgFillPrice", 0.0)
        if avg and avg > 0:
            fill_price = f"{avg:.2f}"

        # Fallback: compute from trade.fills when avgFillPrice is 0
        if fill_price == "–" and getattr(trade, "fills", None):
            total_qty_f = 0.0
            weighted_sum = 0.0
            for f in trade.fills:
                ex = f.execution
                if ex.shares and ex.price:
                    total_qty_f += ex.shares
                    weighted_sum += ex.shares * ex.price
            if total_qty_f > 0:
                fill_price = f"{weighted_sum / total_qty_f:.2f}"

        qty = params.get("qty", 0) or trade.order.totalQuantity
        quantity = str(int(qty)) if qty else "0"

        lmt = params.get("limit_price", "–")
        stp = params.get("stop_price", "–")

        # Timestamp from trade.log (Eastern) – for live orders the log
        # is always populated, so now() is a safe final fallback here.
        et_dt = None
        log_entries = getattr(trade, "log", None)
        if log_entries:
            last_entry = log_entries[-1]
            et_dt = _to_eastern(getattr(last_entry, "time", None))

        if et_dt is None and getattr(trade, "fills", None):
            try:
                ft = getattr(trade.fills[-1].execution, "time", None)
                if ft:
                    et_dt = _to_eastern(ft)
            except Exception:
                pass

        if et_dt is None:
            et_dt = datetime.now(_ET)
        timestamp = et_dt.strftime("%H:%M:%S")
        time_sort = et_dt.isoformat()

        # Cache the timestamp keyed by orderRef so _build_trade_dict's step 5
        # can recover the real time on post-reconnect snapshots.  Live orders
        # always have a valid time here (fallback is now(), not midnight).
        order_ref = getattr(trade.order, "orderRef", "") or ""
        if order_ref and order_ref.startswith("nano_"):
            self._order_times.setdefault(order_ref, et_dt.isoformat())
            sym = params.get("local_symbol") or params.get("symbol", "")
            if sym:
                self._order_symbols.setdefault(order_ref, sym)

        d = {
            "order_id": order_id,
            "order_ref": order_ref,
            # Prefer localSymbol ("NQM6") over bare root symbol ("NQ") so the
            # order blotter is consistent with the positions screen and with
            # orders that arrive via _build_trade_dict (snapshot / reconnect).
            # Falls back to bare symbol for instruments that have no localSymbol
            # (most stocks), then to "" if params are empty.
            "symbol": (params.get("local_symbol") or params.get("symbol", "")),
            # Contract identity for jump-to-Book-Trader (see
            # _emit_opt_precheck_status).
            "con_id": int(params.get("conid", 0) or 0),
            "sec_type": params.get("sec_type", "") or "",
            "account": params.get("account", ""),
            "action": params.get("action", ""),
            "order_type": params.get("order_type", "MKT"),
            "quantity": quantity,
            "limit_price": str(lmt) if lmt else "–",
            "stop_price": str(stp) if stp else "–",
            "tif": trade.order.tif or params.get("tif", "DAY"),
            "fill_price": fill_price,
            "status": status,
            "time": timestamp,
            "time_sort": time_sort,
            # Persisted rejection reason keyed by orderRef — survives reconnect
            "error_message": self._order_error_messages.get(order_ref, ""),
        }
        if supersedes:
            d["supersedes"] = supersedes
        self.order_status_updated.emit(d)

    def _cache_order_status(self, d: dict) -> None:
        """Queued slot on the main thread — mirror every emitted status
        dict into ``_order_status_cache`` for late subscribers."""
        if not isinstance(d, dict):
            return
        order_id = d.get("order_id", "")
        if not order_id:
            return
        superseded = d.get("supersedes")
        if superseded:
            self._order_status_cache.pop(superseded, None)
        self._order_status_cache[order_id] = dict(d)

    def order_status_snapshot(self) -> list[dict]:
        """Copies of the latest status dict per order, for consumers that
        wire ``order_status_updated`` after the post-connect snapshot
        burst already fired.  Call from the GUI thread only."""
        return [dict(d) for d in self._order_status_cache.values()]

    # ── ib_async callbacks ────────────────────────────────────────────────────

    def _apply_fill_to_position(self, trade, params: dict) -> None:
        """Immediately adjust the cached position after an order fills.

        TWS sends ``positionEvent`` only for the *changed* position, not a
        full snapshot.  For a position that goes to zero TWS simply omits
        it from subsequent events, so the UI would never learn it closed.
        Computing the new qty from the fill data guarantees an immediate
        and correct update.

        The next ``positionEvent`` or manual refresh will reconcile.
        """
        try:
            account = params.get("account", "")
            con_id = int(params.get("conid", 0))
            if not account or not con_id:
                return

            action = (params.get("action", "") or "").upper()
            filled_qty = float(getattr(trade.orderStatus, "filled", 0) or 0)
            if filled_qty <= 0:
                filled_qty = float(params.get("qty", 0) or 0)
            if filled_qty <= 0:
                return

            key = (account, con_id)
            cached = self._portfolio_cache.get(key)
            if cached is None:
                return  # unknown position – positionEvent will handle it

            old_pos = float(cached.get("position", 0))
            if action == "BUY":
                new_pos = old_pos + filled_qty
            elif action == "SELL":
                new_pos = old_pos - filled_qty
            else:
                return

            cached["position"] = new_pos
            if new_pos == 0:
                cached["market_value"] = 0.0
                # A flat position has no unrealized P&L — everything is
                # realized on close.  TWS omits flat positions from later
                # events, so a stale pre-close unrealized figure would
                # otherwise stick on the zero row forever.
                cached["unrealized_pnl"] = 0.0
            self._portfolio_cache[key] = cached

            # Emit immediately so the UI updates without waiting for positionEvent
            self.portfolio_updated.emit([cached])

            # RC #3: mirror the deferred-cancellation logic from _on_position_update.
            # Fill events resolve the position before positionEvent arrives, so
            # we schedule the cancellation here too.
            pnl_key = (account, con_id)
            if new_pos == 0 and pnl_key in self._pnl_single_subs:
                self._pnl_zero_pending_cancel[pnl_key] = time.monotonic() + 60
            elif new_pos != 0 and pnl_key in self._pnl_zero_pending_cancel:
                del self._pnl_zero_pending_cancel[pnl_key]

            tws_log.info(
                "[%s] FILL-POS-UPDATE  account=%s conId=%s %s %.0f → "
                "pos %.0f→%.0f",
                self._label, account, con_id, action, filled_qty,
                old_pos, new_pos)

        except Exception:
            log.debug("Worker %s: _apply_fill_to_position failed", self._label,
                      exc_info=True)

    def _on_price_update(self, ticker) -> None:
        """Called by ib_async when a subscribed ticker receives a new tick."""
        try:
            sym = self._active_price_symbol
            if not sym:
                return

            price = float('nan')
            source = ""

            # 1. Last trade price (most accurate for live/delayed)
            last = getattr(ticker, "last", float('nan'))
            if not math.isnan(last) and last > 0:
                price = last
                source = "last"

            # 2. Extract bid / ask once (reused for midpoint fallback + signal)
            bid = getattr(ticker, "bid", float('nan'))
            ask = getattr(ticker, "ask", float('nan'))
            bid_valid = not math.isnan(bid) and bid > 0 and bid < 1e12
            ask_valid = not math.isnan(ask) and ask > 0 and ask < 1e12

            if math.isnan(price):
                if bid_valid and ask_valid:
                    price = (bid + ask) / 2.0
                    source = "bid/ask mid"

            # 3. Fallback: close (previous session – only as last resort)
            if math.isnan(price):
                close = getattr(ticker, "close", float('nan'))
                if not math.isnan(close) and close > 0:
                    price = close
                    source = "close"

            if not math.isnan(price):
                self.price_updated.emit(sym, price)

            # Always emit bid/ask when both are valid
            if bid_valid and ask_valid:
                self.bid_ask_updated.emit(sym, bid, ask)

        except Exception:
            log.debug("Worker %s: bad price tick: %s", self._label, ticker)

    def _on_pending_tickers(self, tickers) -> None:
        """Called by ib_async when any subscribed tickers have pending updates.

        This is the most reliable streaming hook — fires for live, delayed,
        and frozen data.  Handles both the single active ticker and background
        tickers for the multi-symbol cache.
        """
        for ticker in tickers:
            # Single-symbol active subscription — mark dirty; the 100 ms
            # flush loop coalesces bursts (same treatment as depth): the
            # unthrottled form emitted per L1 tick and every emit fanned
            # into ladder full-grid repaints, trades-summary marks and
            # ticker-strip restyles on the GUI thread.
            if (self._active_ticker is not None
                    and self._active_price_symbol
                    and ticker is self._active_ticker):
                self._active_price_dirty = True

            # Options-income ticket quote batch (O(1) reverse index)
            if self._ticker_to_opt_conid:
                conid = self._ticker_to_opt_conid.get(id(ticker))
                if conid:
                    self._emit_opt_quote(conid, ticker)

            # RC #6: O(1) reverse-index lookup instead of O(M) linear scan.
            # Previously iterated over every entry in _bg_tickers on each tick,
            # which scaled quadratically as background subscriptions grew.
            if self._ticker_to_sym:
                sym = self._ticker_to_sym.get(id(ticker))
                if sym:
                    self._bg_price_dirty[sym] = ticker
                    if sym in self._l1_refused and self._has_l1_price_tick(ticker):
                        self._clear_l1_refusals(sym)

            # Depth-of-market hot path: mark dirty, don't emit here — the
            # 100 ms ``_depth_flush_loop`` coalesces bursts so we don't
            # flood the Qt main thread with one signal per L2 tick.
            if self._ticker_to_depth_sym:
                d_sym = self._ticker_to_depth_sym.get(id(ticker))
                if d_sym:
                    self._depth_dirty.add(d_sym)

    async def _do_market_data(self, params: dict) -> None:
        """Subscribe to live top-of-book data for the contract in *params*."""
        if self._ib is None or not self._ib.isConnected():
            log.warning(
                "Worker %s: market data requested but not connected", self._label)
            return

        # Cancel any existing subscription first
        await self._do_cancel_market_data()

        try:
            from ib_async import Contract
            conid = int(params.get("conid", 0))
            if conid <= 0:
                log.warning(
                    "Worker %s: invalid conid for market data: %s", self._label, conid)
                return

            # Build contract directly from saved details – no qualifyContracts needed.
            c = Contract(conId=conid)
            c.exchange = params.get("exchange") or "SMART"
            c.currency = params.get("currency") or "USD"
            if params.get("sec_type"):
                c.secType = params["sec_type"]
            if params.get("local_symbol"):
                c.localSymbol = params["local_symbol"]
            if params.get("expiry"):
                c.lastTradeDateOrContractMonth = params["expiry"]
            if params.get("multiplier"):
                c.multiplier = params["multiplier"]

            # Request delayed-frozen fallback when real-time is unavailable
            # (e.g. error 10197 "competing live session").  Type 4 = live
            # when entitled → delayed → frozen close values after hours.
            # NOTE: the market-data type is CONNECTION-GLOBAL — every call
            # site in this worker must use the same type, or the last one
            # wins and knocks the others out of frozen mode.
            self._ib.reqMarketDataType(4)

            self._active_price_symbol = params.get("symbol", "")
            ticker = self._ib.reqMktData(c, genericTickList="", snapshot=False)
            self._active_ticker = ticker
            log.info("Worker %s: subscribed to market data for %s (conId=%d)",
                     self._label, self._active_price_symbol, conid)
            tws_log.info("[%s] API-CALL  reqMktData(conId=%d, symbol=%s, exchange=%s, "
                         "snapshot=False)",
                         self._label, conid, self._active_price_symbol, c.exchange)

            # Emit immediately if ticker already has a price
            self._on_price_update(ticker)

        except Exception as exc:
            log.warning(
                "Worker %s: market data subscription failed: %s", self._label, exc)

    async def _do_cancel_market_data(self) -> None:
        """Cancel the current market data subscription if any."""
        if self._active_ticker is not None:
            try:
                if self._ib is not None and self._ib.isConnected():
                    self._ib.cancelMktData(self._active_ticker.contract)
                    log.debug("Worker %s: cancelled market data for %s",
                              self._label, self._active_price_symbol)
                    tws_log.info("[%s] API-CALL  cancelMktData(symbol=%s)",
                                 self._label, self._active_price_symbol)
            except Exception as exc:
                log.debug("Worker %s: cancel market data error: %s",
                          self._label, exc)
            finally:
                self._active_ticker = None
                self._active_price_symbol = ""
                self._active_price_dirty = False

    # ── Background market data (multi-symbol cache) ───────────────────────────

    async def _do_subscribe_bg_symbols(self, params_list: list[dict]) -> None:
        """Subscribe to background market data for multiple symbols.

        Symbols already subscribed (present in ``_bg_tickers``) are skipped.
        Emits ``bg_price_updated`` immediately for any ticker that already
        has data cached by ib_async.
        """
        if self._ib is None or not self._ib.isConnected():
            return

        from ib_async import Contract

        # 4 = delayed-frozen fallback (connection-global — keep every call
        # site on the same type; see _do_market_data).
        self._ib.reqMarketDataType(4)

        for params in params_list:
            sym = params.get("symbol", "")
            if not sym or sym in self._bg_tickers:
                continue  # already subscribed

            conid = int(params.get("conid", 0))
            if conid <= 0:
                continue

            c = Contract(conId=conid)
            c.exchange = params.get("exchange") or "SMART"
            c.currency = params.get("currency") or "USD"
            if params.get("sec_type"):
                c.secType = params["sec_type"]
            if params.get("local_symbol"):
                c.localSymbol = params["local_symbol"]
            if params.get("expiry"):
                c.lastTradeDateOrContractMonth = params["expiry"]
            if params.get("multiplier"):
                c.multiplier = params["multiplier"]

            try:
                ticker = self._ib.reqMktData(
                    c, genericTickList="", snapshot=False)
                self._bg_tickers[sym] = ticker
                self._ticker_to_sym[id(ticker)] = sym  # RC #6: reverse index
                log.info("Worker %s: bg subscribed to %s (conId=%d)",
                         self._label, sym, conid)
                tws_log.info("[%s] API-CALL  reqMktData(conId=%d, symbol=%s, exchange=%s, "
                             "snapshot=False) [background]",
                             self._label, conid, sym, c.exchange)
                # Emit immediately if ticker already has cached data
                self._emit_bg_price(sym, ticker)
            except Exception as exc:
                log.warning("Worker %s: bg subscribe failed for %s: %s",
                            self._label, sym, exc)

        log.info("Worker %s: background subscriptions active for %d symbol(s)",
                 self._label, len(self._bg_tickers))

    async def _do_unsubscribe_bg_symbol(self, symbol: str) -> None:
        """Unsubscribe one background market-data stream."""
        ticker = self._bg_tickers.pop(symbol, None)
        self._bg_last_bid_ask.pop(symbol, None)  # clear stale bid/ask cache
        self._clear_l1_refusals(symbol)
        if ticker is not None:
            self._ticker_to_sym.pop(id(ticker), None)  # RC #6: clean reverse index
            if self._ib is not None and self._ib.isConnected():
                try:
                    self._ib.cancelMktData(ticker.contract)
                    log.debug("Worker %s: bg unsubscribed %s", self._label, symbol)
                except Exception as exc:
                    log.debug("Worker %s: bg cancel error for %s: %s",
                              self._label, symbol, exc)

    async def _do_unsubscribe_all_bg(self) -> None:
        """Unsubscribe all background market-data streams."""
        if self._ib is not None and self._ib.isConnected():
            for sym, ticker in list(self._bg_tickers.items()):
                try:
                    self._ib.cancelMktData(ticker.contract)
                except Exception:
                    pass
        count = len(self._bg_tickers)
        self._bg_tickers.clear()
        self._bg_last_bid_ask.clear()    # clear stale bid/ask cache
        self._ticker_to_sym.clear()  # RC #6: clear reverse index
        self._bg_price_dirty.clear()     # pending emits reference dead tickers
        self._clear_l1_refusals()
        log.debug("Worker %s: all %d bg subscriptions cleared",
                  self._label, count)

    # ── Depth-of-market (L2) subscription helpers ─────────────────────────────

    async def _do_subscribe_depth(self, symbol: str, num_rows: int) -> None:
        """Resolve a contract for *symbol* via ReferenceData and issue
        ``reqMktDepth``.  Errors (TWS code 101 — too many subs, 10090 /
        10167 — no L2 entitlement) are caught and surfaced via
        ``market_data_failed`` so the UI can render the ladder using
        L1-only data.
        """
        if self._ib is None or not self._ib.isConnected():
            log.debug("Worker %s: depth subscribe skipped (no IB)", self._label)
            return
        sym = (symbol or "").strip()
        if not sym:
            return
        if sym in self._depth_tickers:
            return   # already subscribed

        # Resolve the contract from the chip's instrument entry (the
        # resolver is Qt-free and thread-safe).  This mirrors what
        # OrderEntryPanel already does for L1 subscriptions and avoids
        # a round-trip to TWS for contract-details.
        try:
            from src.core import instruments
            cached = instruments.get_contract(sym)
        except Exception:
            cached = None
        if not cached:
            log.warning("Worker %s: depth subscribe — no local contract for %s",
                        self._label, sym)
            self.market_data_failed.emit(sym)
            return

        try:
            from ib_async import Contract
        except ImportError:
            return
        c = Contract(
            conId=int(cached.get("conid") or 0),
            symbol=cached.get("symbol", sym),
            secType=cached.get("sec_type", "") or "",
            exchange=cached.get("exchange", "") or "SMART",
            currency=cached.get("currency", "USD") or "USD",
            localSymbol=cached.get("local_symbol", "") or "",
            lastTradeDateOrContractMonth=cached.get("expiry", "") or "",
            multiplier=cached.get("multiplier", "") or "",
        )

        # SMART-routed contracts (stocks) only have depth as *smart depth*
        # — TWS aggregates the venues the account is entitled to and
        # labels each level with its exchange.  Asking for plain
        # per-exchange depth on SMART is rejected with error 10092
        # ("Deep market data is not supported for this combination of
        # security type/exchange"), so the ladder never saw a stock
        # book.  Direct-routed futures (CME, NYMEX, …) take the plain
        # form; smart depth on them is the error case.
        smart = (c.exchange or "").upper() == "SMART"
        try:
            ticker = self._ib.reqMktDepth(
                c, numRows=int(num_rows or 10), isSmartDepth=smart)
        except Exception as exc:
            log.warning("Worker %s: reqMktDepth(%s) raised: %s",
                        self._label, sym, exc)
            self.market_data_failed.emit(sym)
            return

        self._depth_tickers[sym] = ticker
        self._ticker_to_depth_sym[id(ticker)] = sym
        self._depth_smart[sym] = smart
        log.info("Worker %s: depth subscribed %s (numRows=%d, smart=%s)",
                 self._label, sym, num_rows, smart)
        tws_log.info("[%s] API-CALL  reqMktDepth(conId=%s, symbol=%s, "
                     "exchange=%s, numRows=%d, isSmartDepth=%s)",
                     self._label, c.conId, sym, c.exchange, num_rows, smart)

    async def _do_unsubscribe_depth(self, symbol: str) -> None:
        sym = (symbol or "").strip()
        if not sym:
            return
        ticker = self._depth_tickers.pop(sym, None)
        self._depth_dirty.discard(sym)
        smart = self._depth_smart.pop(sym, False)
        if ticker is None:
            return
        self._ticker_to_depth_sym.pop(id(ticker), None)
        if self._ib is not None and self._ib.isConnected():
            try:
                self._ib.cancelMktDepth(ticker.contract, isSmartDepth=smart)
            except Exception as exc:
                log.debug("Worker %s: cancelMktDepth(%s) error: %s",
                          self._label, sym, exc)
        log.debug("Worker %s: depth unsubscribed %s", self._label, sym)

    async def _do_unsubscribe_all_depth(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            for sym, ticker in list(self._depth_tickers.items()):
                try:
                    self._ib.cancelMktDepth(
                        ticker.contract,
                        isSmartDepth=self._depth_smart.get(sym, False))
                except Exception:
                    pass
        count = len(self._depth_tickers)
        self._depth_tickers.clear()
        self._ticker_to_depth_sym.clear()
        self._depth_smart.clear()
        self._depth_dirty.clear()
        log.debug("Worker %s: all %d depth subscriptions cleared",
                  self._label, count)

    async def _depth_flush_loop(self) -> None:
        """Every 100 ms, emit a ``depth_updated`` signal for each symbol
        whose L2 ticker was marked dirty by ``_on_pending_tickers``.

        The hot path (``_on_pending_tickers``) only adds to ``_depth_dirty``
        — no allocation, no emit.  The flush reads ``ticker.domBids`` /
        ``ticker.domAsks`` (ib_async mutates these lists in place on each
        tick) and copies them into plain ``list[tuple[price, size]]``
        before emitting, so the UI thread never sees partial mutations.

        A separate 100 ms loop is used instead of reusing the 250 ms
        ``_throttler_loop`` because DOM feels laggy above ~150 ms and we
        don't want to couple PnL/summary batching cadence to DOM cadence.
        """
        while self._running:
            await asyncio.sleep(0.1)

            # L1 price coalescing shares this loop's cadence: flush the
            # active symbol and any dirty background symbols at most
            # 10×/s each.  First paint stays instant — subscribe paths
            # emit directly.
            if self._active_price_dirty:
                self._active_price_dirty = False
                if (self._active_ticker is not None
                        and self._active_price_symbol):
                    self._on_price_update(self._active_ticker)
            if self._bg_price_dirty:
                bg_flush = self._bg_price_dirty
                self._bg_price_dirty = {}
                for sym, ticker in bg_flush.items():
                    self._emit_bg_price(sym, ticker)

            if not self._depth_dirty:
                continue
            for sym in list(self._depth_dirty):
                ticker = self._depth_tickers.get(sym)
                if ticker is None:
                    continue
                # Snapshot into plain tuples BEFORE emit so ib_async's
                # in-place mutation of the lists can't race the UI.
                try:
                    bids = [(float(l.price), float(l.size))
                            for l in getattr(ticker, "domBids", [])]
                    asks = [(float(l.price), float(l.size))
                            for l in getattr(ticker, "domAsks", [])]
                except Exception as exc:
                    log.debug("Worker %s: depth snapshot error (%s): %s",
                              self._label, sym, exc)
                    continue
                self.depth_updated.emit(sym, bids, asks)
            self._depth_dirty.clear()

    def _emit_bg_price(self, sym: str, ticker) -> None:
        """Extract last/bid/ask from a background ticker and emit ``bg_price_updated``.

        Bid/ask are carried forward from the last valid tick so that a
        "last-price-only" update (common with IB delayed / type-3 data for
        futures) does not emit bid=0 / ask=0 and blank out the UI display.
        """
        try:
            last = getattr(ticker, "last", float('nan'))
            bid = getattr(ticker, "bid", float('nan'))
            ask = getattr(ticker, "ask", float('nan'))

            bid_valid = not math.isnan(bid) and bid > 0 and bid < 1e12
            ask_valid = not math.isnan(ask) and ask > 0 and ask < 1e12

            # Derive last from midpoint only when both bid and ask are
            # live — the midpoint is a reasonable stand-in for "current
            # price" when the tape is quiet but the book is active.
            if math.isnan(last) or last <= 0:
                if bid_valid and ask_valid:
                    last = (bid + ask) / 2.0

            # Do NOT synthesize last from the prior-session close: a
            # snapshot-only ticker (close populated, bid/ask still NaN
            # while streaming warms up) would otherwise look like a
            # live tick with bid/ask missing, leaving the UI showing a
            # frozen price plus a zero change and no quotes.  Leaving
            # ``last`` at 0 here keeps the price/bid/ask/change row
            # blank until a real tick arrives; ``close_out`` below still
            # carries the close to the UI for day-change computation.
            if math.isnan(last) or last <= 0:
                last = 0.0

            # Preserve the last known valid bid/ask — fall back to the cache
            # when the current tick doesn't carry bid or ask data.
            prev = self._bg_last_bid_ask.get(sym, (0.0, 0.0))
            bid_out = bid if bid_valid else prev[0]
            ask_out = ask if ask_valid else prev[1]
            if bid_valid or ask_valid:
                self._bg_last_bid_ask[sym] = (bid_out, ask_out)

            # Prior-session close for day-change display (0 when unknown).
            close = getattr(ticker, "close", float('nan'))
            close_out = float(close) if (not math.isnan(close)
                                         and close > 0) else 0.0

            self.bg_price_updated.emit(sym, last, bid_out, ask_out, close_out)

            # ── Sizes (for Book Trader L1-fallback + last-trade stripe) ─
            def _sz(val):
                if val is None:
                    return 0.0
                try:
                    v = float(val)
                except (TypeError, ValueError):
                    return 0.0
                if math.isnan(v) or v < 0 or v >= _IB_DBL_MAX:
                    return 0.0
                return v
            self.bg_sizes_updated.emit(
                sym,
                _sz(getattr(ticker, "bidSize", 0.0)),
                _sz(getattr(ticker, "askSize", 0.0)),
                _sz(getattr(ticker, "lastSize", 0.0)),
            )
        except Exception:
            log.debug("Worker %s: bg price emit error for %s",
                      self._label, sym)

    def _prune_stale_data(self) -> None:
        """RC #5 — trim permanent dictionaries to bound memory over long sessions.

        Called by the throttler loop every ~75 s.  Removes:
        * Order-ref caches (_order_times, _order_error_messages,
          _order_symbols, _rejected_order_cache) for orders placed on a
          previous calendar day.
        * Terminal-status _trades entries that are NOT from today.
        * _portfolio_cache entries for zero-position instruments whose
          reqPnLSingle subscription has already been cancelled (they will
          never receive another update).
        * _fills_cache entries older than 10 calendar days — comfortably
          past the UI's widest preset (7 trading days), and the fills
          snapshot is the Trades tab's source of truth after a rewire,
          so nothing a preset can show is dropped.
        * _order_status_cache entries for terminal orders from previous
          days (today's stay: the blotter shows them all session).
        * _conid_contract_meta entries no cache references any more.
        """
        # Refs generated before the YYMMDD switch carry a 4-digit MMDD, so
        # match on both prefixes; a legacy ref simply prunes as stale on
        # the next sweep, which is the correct outcome for a past day.
        now_et = datetime.now(_ET)
        today_prefix = f"nano_{now_et:%y%m%d}_"
        legacy_prefix = f"nano_{now_et:%m%d}_"

        def _is_today_ref(ref: str) -> bool:
            return ref.startswith(today_prefix) or ref.startswith(legacy_prefix)

        # ── Order-ref caches ─────────────────────────────────────────────────
        stale_refs = [r for r in list(self._order_times)
                      if not _is_today_ref(r)]
        for r in stale_refs:
            self._order_times.pop(r, None)
            self._order_error_messages.pop(r, None)
            self._order_symbols.pop(r, None)

        stale_rejected = [oid for oid in list(self._rejected_order_cache)
                          if today_prefix not in oid
                          and legacy_prefix not in oid]
        for oid in stale_rejected:
            self._rejected_order_cache.pop(oid, None)

        # ── _trades ──────────────────────────────────────────────────────────
        # Keep all of today's orders; prune terminal-status orders from
        # previous days (they can no longer be cancelled or modified).
        stale_trades: list[str] = []
        for oid, trade in list(self._trades.items()):
            if today_prefix in oid:
                continue  # today — keep regardless of status
            status = (getattr(trade.orderStatus, "status", "")
                      if getattr(trade, "orderStatus", None) else "")
            if status in ("Filled", "Cancelled", "Canceled",
                          "Inactive", "ApiCancelled", "Completed"):
                stale_trades.append(oid)
        for oid in stale_trades:
            self._trades.pop(oid, None)
            self._has_status_handler.discard(oid)

        # ── _portfolio_cache ─────────────────────────────────────────────────
        # Remove zero-position entries whose reqPnLSingle subscription has
        # been cancelled (not in _pnl_single_subs, not pending cancel).
        # These entries will never receive another pnlSingle update.
        stale_pos: list[tuple[str, int]] = []
        for key, pos in list(self._portfolio_cache.items()):
            if (pos.get("position", 0) == 0
                    and key not in self._pnl_single_subs
                    and key not in self._pnl_zero_pending_cancel):
                stale_pos.append(key)
        for key in stale_pos:
            self._portfolio_cache.pop(key, None)
            self._pnl_single_cache.pop(key, None)

        # ── _fills_cache ─────────────────────────────────────────────────────
        # time_sort is ISO-style, so a lexicographic date compare works.
        fills_cutoff = (now_et - timedelta(days=10)).date().isoformat()
        stale_fills = [
            eid for eid, f in self._fills_cache.items()
            if (f.get("time_sort", "") or "")[:10] < fills_cutoff
            and f.get("time_sort")
        ]
        for eid in stale_fills:
            self._fills_cache.pop(eid, None)

        # ── _order_status_cache ──────────────────────────────────────────────
        terminal = ("Filled", "Cancelled", "Canceled",
                    "Inactive", "ApiCancelled", "Completed",
                    "Rejected", "ValidationError")
        today_iso = now_et.date().isoformat()
        stale_status: list[str] = []
        for oid, d in self._order_status_cache.items():
            if d.get("status", "") not in terminal:
                continue
            if today_prefix in oid or legacy_prefix in oid:
                continue   # today's internal order — keep
            ts = d.get("time_sort", "") or ""
            if ts and ts[:10] < today_iso:
                stale_status.append(oid)
            elif not ts and "_nano_" in oid:
                # Internal ref from a previous day (prefix check above
                # already excluded today's) with no timestamp.
                stale_status.append(oid)
        for oid in stale_status:
            self._order_status_cache.pop(oid, None)

        # ── _conid_contract_meta ─────────────────────────────────────────────
        if self._conid_contract_meta:
            referenced: set[int] = set()
            for f in self._fills_cache.values():
                cid = f.get("con_id", 0)
                if cid:
                    referenced.add(int(cid))
            for trade in self._trades.values():
                cid = getattr(getattr(trade, "contract", None), "conId", 0)
                if cid:
                    referenced.add(int(cid))
            for (_acct, cid) in self._portfolio_cache:
                referenced.add(int(cid))
            for cid in [c for c in self._conid_contract_meta
                        if c not in referenced]:
                self._conid_contract_meta.pop(cid, None)

        if (stale_refs or stale_rejected or stale_trades or stale_pos
                or stale_fills or stale_status):
            log.debug(
                "Worker %s: pruned %d order refs, %d rejected orders, "
                "%d stale trades, %d flat positions, %d old fills, "
                "%d old status entries",
                self._label,
                len(stale_refs), len(stale_rejected),
                len(stale_trades), len(stale_pos),
                len(stale_fills), len(stale_status))

    def _on_account_summary(self, value) -> None:
        """De-duplicate rapid ticks into the buffer dict.

        ``accountSummaryEvent`` emits a single :class:`AccountValue` per call.
        """
        try:
            # CashBalance from the summary's $LEDGER tag is group-consolidated
            # (account="All"), not per-account — drop it here and rely on the
            # per-account accountValueEvent stream instead.
            if value.tag == "CashBalance":
                return
            self._buffer[(value.account, value.tag, value.currency)] = (
                value.value, value.currency)
            tws_log.debug("[%s] EVENT     accountSummaryEvent → account=%s tag=%s "
                          "value=%s currency=%s",
                          self._label, value.account, value.tag,
                          value.value, value.currency)
        except Exception:
            log.debug("Worker %s: bad AccountValue: %s", self._label, value)

    def _on_account_value(self, value) -> None:
        """Funnel per-account ``CashBalance`` ledger rows from the
        reqAccountUpdatesMulti stream into the same buffer the account
        summary uses, so the existing ``summary_batch_updated`` path carries
        them to the UI.  Only CashBalance is forwarded — the multi stream
        also emits dozens of other tags we don't surface — and the "BASE"
        roll-up row is dropped so only real currencies reach the panel."""
        try:
            if value.tag != "CashBalance" or value.currency in ("", "BASE"):
                return
            self._buffer[(value.account, value.tag, value.currency)] = (
                value.value, value.currency)
            tws_log.debug("[%s] EVENT     accountValueEvent → account=%s "
                          "CashBalance %s %s",
                          self._label, value.account,
                          value.value, value.currency)
        except Exception:
            log.debug("Worker %s: bad account value: %s", self._label, value)

    def _on_pnl(self, entry) -> None:
        """Handle pnlEvent – provides dailyPnL, realizedPnL, unrealizedPnL.

        ``pnlEvent`` emits a single :class:`PnL` object per call.
        Fields may be ``float('nan')`` or ``DBL_MAX`` when unavailable.
        """
        try:
            acct = entry.account
            tws_log.debug("[%s] EVENT     pnlEvent → account=%s dailyPnL=%s "
                          "realizedPnL=%s unrealizedPnL=%s",
                          self._label, acct,
                          entry.dailyPnL, entry.realizedPnL, entry.unrealizedPnL)
            daily = _safe_pnl(entry.dailyPnL)
            # TEMP diagnostic: show raw vs sanitised dailyPnL once per account
            # so we can see whether IB is sending the DBL_MAX "unset" sentinel.
            if not hasattr(self, "_pnl_diag"):
                self._pnl_diag = set()
            if acct not in self._pnl_diag:
                self._pnl_diag.add(acct)
                log.info("Worker %s pnlEvent diag: acct=%s rawDaily=%r safeDaily=%r "
                         "rawUnrl=%r rawReal=%r",
                         self._label, acct, entry.dailyPnL, daily,
                         entry.unrealizedPnL, entry.realizedPnL)
            if daily != 0.0:
                self._buffer[(acct, "DailyPnL", "BASE")] = (str(daily), "BASE")
            # Emit account-level P&L signal for the UI position panel
            realized = _safe_pnl(entry.realizedPnL)
            unrealized = _safe_pnl(entry.unrealizedPnL)
            self.account_pnl_updated.emit(acct, daily, realized, unrealized)
        except Exception:
            log.debug("Worker %s: bad PnL entry: %s", self._label, entry)

    def _on_pnl_single(self, entry) -> None:
        """Handle pnlSingleEvent – per-position P&L updates.

        ``pnlSingleEvent`` emits a :class:`PnLSingle` object per call.
        Fields may be ``float('nan')`` or ``DBL_MAX`` (1.7976e+308) when
        unavailable.

        Also extracts ``entry.value`` (market value) and injects it into
        the portfolio cache so positions populated via ``positionEvent``
        (which does **not** carry market data) get live market values.
        """
        try:
            acct = entry.account
            con_id = entry.conId
            tws_log.debug("[%s] EVENT     pnlSingleEvent → account=%s conId=%s "
                          "dailyPnL=%s unrealizedPnL=%s realizedPnL=%s value=%s",
                          self._label, acct, con_id,
                          entry.dailyPnL, entry.unrealizedPnL, entry.realizedPnL,
                          getattr(entry, "value", None))
            daily = _safe_pnl(entry.dailyPnL)
            realized = _safe_pnl(entry.realizedPnL)
            unrealized = _safe_pnl(entry.unrealizedPnL)
            self._pnl_single_cache[(acct, con_id)] = (daily, realized, unrealized)
            # RC #1: store in throttle buffer — the _throttler_loop flushes
            # at most every 250 ms, collapsing rapid ticks into one signal
            # per (account, conId) instead of flooding the Qt event queue.
            self._pnl_single_buffer[(acct, con_id)] = (daily, realized, unrealized)

            # ── Merge market_value / market_price into portfolio cache ────────
            # positionEvent only delivers position qty + avgCost.  PnLSingle
            # adds value (market_value) and position (qty), from which we can
            # derive market_price when position != 0.  Merge these into the
            # cached dict so snapshot callers and the UI position model see
            # live values.
            mkt_value = _safe_pnl(getattr(entry, "value", None))
            pos_qty = float(getattr(entry, "position", 0) or 0)
            mkt_price = 0.0
            if pos_qty != 0 and mkt_value != 0.0:
                # For futures, entry.value = qty × price × multiplier,
                # so mkt_value / pos_qty = price × multiplier (e.g. 2× for
                # MNQ).  Divide by the multiplier to get the true price.
                key = (acct, con_id)
                cached_for_mult = self._portfolio_cache.get(key, {})
                try:
                    mult = float(cached_for_mult.get("multiplier", "") or 1)
                except (ValueError, TypeError):
                    mult = 1.0
                if mult < 1:
                    mult = 1.0
                mkt_price = mkt_value / (pos_qty * mult)

            key = (acct, con_id)
            if key in self._portfolio_cache:
                cached = self._portfolio_cache[key]
                # ── IMPORTANT: never write position qty from pnlSingle. ────────
                # pnlSingle events can arrive out-of-order or with a stale
                # position value AFTER positionEvent has already zeroed the
                # cache.  Writing pos_qty here would overwrite the correct
                # position=0 set by positionEvent, causing the closed position
                # to reappear with the old (pre-sell) qty.
                # Position qty is owned exclusively by _on_position_update.
                if pos_qty == 0:
                    # Market value / price / unrealized P&L for a flat
                    # position are definitively zero — zero them without
                    # the != 0.0 guards so the UI model shows them as 0
                    # even when positionEvent is late.  (The unrealized
                    # guard below would otherwise SKIP IB's legitimate
                    # "unrealized = 0" report and leave the stale
                    # pre-close figure on the zero row forever.)
                    cached["market_value"] = 0.0
                    cached["market_price"] = 0.0
                    cached["unrealized_pnl"] = 0.0
                else:
                    # Position is still open — update live market data.
                    if mkt_value != 0.0:
                        cached["market_value"] = mkt_value
                    if mkt_price != 0.0:
                        cached["market_price"] = mkt_price
                    if unrealized != 0.0:
                        cached["unrealized_pnl"] = unrealized
                if realized != 0.0:
                    cached["realized_pnl"] = realized
                # Re-buffer so the throttler emits an updated portfolio row
                self._portfolio_buffer[key] = cached
        except Exception:
            log.debug("Worker %s: bad PnLSingle entry: %s", self._label, entry)

    def _on_position_update(self, item) -> None:
        """Buffer a PortfolioItem update for throttled emission.

        ``positionEvent`` fires once per position change.
        """
        try:
            contract = item.contract
            acct = getattr(item, "account", "")
            con_id = getattr(contract, "conId", 0)
            if not acct or not con_id:
                log.debug("Worker %s: position update dropped (acct=%r, conId=%r)",
                          self._label, acct, con_id)
                return
            sym = getattr(contract, "localSymbol", "") or getattr(
                contract, "symbol", "")
            pos = float(getattr(item, "position", 0))
            log.debug("Worker %s: position event %s %s pos=%.1f acct=%s",
                      self._label, sym, con_id, pos, acct)
            tws_log.debug("[%s] EVENT     positionEvent → account=%s symbol=%s "
                          "conId=%s pos=%.1f avgCost=%.4f",
                          self._label, acct, sym, con_id, pos,
                          float(getattr(item, "avgCost", 0)))
            self._portfolio_buffer[(acct, con_id)] = {
                "label": self._label,
                "account": acct,
                "con_id": con_id,
                "symbol": getattr(contract, "symbol", ""),
                "local_symbol": getattr(contract, "localSymbol", ""),
                "sec_type": getattr(contract, "secType", ""),
                "exchange": getattr(contract, "exchange", ""),
                "currency": getattr(contract, "currency", "USD"),
                "multiplier": getattr(contract, "multiplier", ""),
                "position": float(getattr(item, "position", 0)),
                # positionEvent → Position(account, contract, position, avgCost)
                # market_price / market_value / unrealized_pnl / realized_pnl
                # are NOT available from positionEvent; they come in via
                # pnlSingleEvent (_on_pnl_single) and are merged into the
                # portfolio cache there.  Preserve any cached values so we
                # don't zero out live data from pnlSingle.
                "avg_cost": float(getattr(item, "avgCost", 0)),
                # A flat position has zero market value and zero
                # unrealized P&L by definition — preserving the cached
                # pre-close values would pin stale figures on the zero
                # row (TWS won't send further events to correct them).
                "market_price":   0.0 if pos == 0 else self._portfolio_cache.get(
                    (acct, con_id), {}).get("market_price", 0.0),
                "market_value":   0.0 if pos == 0 else self._portfolio_cache.get(
                    (acct, con_id), {}).get("market_value", 0.0),
                "unrealized_pnl": 0.0 if pos == 0 else self._portfolio_cache.get(
                    (acct, con_id), {}).get("unrealized_pnl", 0.0),
                "realized_pnl":   self._portfolio_cache.get(
                    (acct, con_id), {}).get("realized_pnl", 0.0),
            }
            # Also persist into the long-lived cache for late subscribers
            self._portfolio_cache[(acct, con_id)
                                  ] = self._portfolio_buffer[(acct, con_id)]


            # Subscribe to per-position PnL for ALL positions (including zero).
            # Keeping the subscription active after a position closes ensures
            # the daily P&L (realized gain/loss from the trade that day) is
            # still delivered and visible in the Positions view.
            pnl_key = (acct, con_id)
            if pnl_key not in self._pnl_single_subs:
                try:
                    self._ib.reqPnLSingle(acct, "", con_id)
                    self._pnl_single_subs.add(pnl_key)
                    tws_log.info("[%s] API-CALL  reqPnLSingle(account=%s, conId=%s)",
                                 self._label, acct, con_id)
                except Exception as exc:
                    log.debug("Worker %s: reqPnLSingle failed for %s/%s: %s",
                              self._label, acct, con_id, exc)

            # RC #3: manage deferred cancellation when position goes flat.
            # Schedule cancellation 60 s from now so realized P&L stays
            # visible in the UI after the close; abort if position re-opens.
            if pos == 0 and pnl_key in self._pnl_single_subs:
                self._pnl_zero_pending_cancel[pnl_key] = time.monotonic() + 60
            elif pos != 0 and pnl_key in self._pnl_zero_pending_cancel:
                # Position re-opened before the cancellation timer fired.
                del self._pnl_zero_pending_cancel[pnl_key]

        except Exception:
            log.debug("Worker %s: bad portfolio item: %s", self._label, item)

    def _depth_sym_for_req_id(self, req_id) -> str | None:
        """Symbol of the depth subscription behind *req_id*, or None when
        the request isn't one of ours (L1 stream, options batch, order
        id, …)."""
        if not self._ticker_to_depth_sym or self._ib is None:
            return None
        try:
            wrapper = self._ib.wrapper
            ticker = wrapper.reqId2Ticker.get(req_id)
            # L1 and depth share one Ticker per contract — only the
            # ticker's depth reqId is a depth request.
            if (ticker is None
                    or wrapper.ticker2ReqId["mktDepth"].get(ticker) != req_id):
                return None
        except Exception:
            return None
        return self._ticker_to_depth_sym.get(id(ticker))

    def _drop_depth_state(self, sym: str) -> None:
        """Forget a depth subscription TWS refused (no cancel — nothing
        was opened)."""
        ticker = self._depth_tickers.pop(sym, None)
        self._depth_smart.pop(sym, None)
        self._depth_dirty.discard(sym)
        if ticker is not None:
            self._ticker_to_depth_sym.pop(id(ticker), None)

    def _bg_sym_for_l1_req_id(self, req_id) -> str | None:
        """Background symbol whose top-of-book request is *req_id*, or
        None (depth request, options batch, order id, …).

        ib_async keys tickers by contract hash, so a symbol's L1 and depth
        streams share ONE Ticker — the reqId is told apart by excluding
        the ticker's live depth reqId."""
        if not self._ticker_to_sym or self._ib is None:
            return None
        try:
            wrapper = self._ib.wrapper
            ticker = wrapper.reqId2Ticker.get(req_id)
            if ticker is None:
                return None
            if wrapper.ticker2ReqId["mktDepth"].get(ticker) == req_id:
                return None
        except Exception:
            return None
        return self._ticker_to_sym.get(id(ticker))

    @staticmethod
    def _has_l1_price_tick(ticker) -> bool:
        """True when this update cycle carried a real bid/ask/last price —
        a depth tick on the shared Ticker doesn't count, and neither do
        the stale fields it still holds from before a refusal."""
        for t in getattr(ticker, "ticks", None) or ():
            if (getattr(t, "tickType", None) in _L1_PRICE_TICK_TYPES
                    and 0 < (getattr(t, "price", 0) or 0) < 1e12):
                return True
        return False

    def _on_l1_refused(self, sym: str, error_code) -> None:
        """TWS refused *sym*'s background quote stream: tell the UI once
        and schedule a re-subscribe."""
        if sym in self._l1_refused:
            tws_log.info("[%s] QUOTE-REFUSED  %s again (error %s)",
                         self._label, sym, error_code)
            return
        self._l1_refused[sym] = (0, time.monotonic() + _L1_RETRY_BASE_S)
        log.warning("Worker %s: no market data for %s (error %s) – "
                    "retrying in %.0f s", self._label, sym, error_code,
                    _L1_RETRY_BASE_S)
        self.quote_status_changed.emit(sym, False)

    def _clear_l1_refusals(self, sym: str | None = None) -> None:
        """Forget a refusal (all of them when *sym* is None) and tell the
        UI quotes are back — or, after a reconnect / unsubscribe, that
        there is no refusal standing any more."""
        syms = list(self._l1_refused) if sym is None else [sym]
        for s in syms:
            if self._l1_refused.pop(s, None) is not None:
                log.info("Worker %s: market data for %s is flowing again",
                         self._label, s)
                self.quote_status_changed.emit(s, True)

    def _retry_refused_quotes(self) -> None:
        """Re-request every refused background quote stream that is due.

        TWS's permission set can change under a running session (it
        re-adds a venue at the next session change), and a fresh request
        is routed against the current set — so a stream refused at 04:00
        may be served on a later attempt without restarting TWS."""
        if self._ib is None or not self._ib.isConnected():
            return
        now = time.monotonic()
        for sym, (tries, due) in list(self._l1_refused.items()):
            if now < due:
                continue
            ticker = self._bg_tickers.get(sym)
            if ticker is None:
                self._l1_refused.pop(sym, None)
                continue
            contract = ticker.contract
            try:
                self._ib.cancelMktData(contract)
            except Exception:
                pass    # TWS already ended the refused request
            try:
                new_ticker = self._ib.reqMktData(
                    contract, genericTickList="", snapshot=False)
            except Exception as exc:
                log.debug("Worker %s: quote retry for %s failed: %s",
                          self._label, sym, exc)
                new_ticker = ticker
            if new_ticker is not ticker:
                self._ticker_to_sym.pop(id(ticker), None)
                self._bg_tickers[sym] = new_ticker
                self._ticker_to_sym[id(new_ticker)] = sym
            tries += 1
            delay = min(_L1_RETRY_BASE_S * (2 ** tries), _L1_RETRY_MAX_S)
            self._l1_refused[sym] = (tries, now + delay)
            tws_log.info("[%s] API-CALL  reqMktData(symbol=%s) [quote retry "
                         "#%d, next in %.0f s]", self._label, sym, tries, delay)

    def _on_ib_error(self, reqId, errorCode, errorString, contract) -> None:
        """Handle IB error events – detect market-data failures like 10197
        and order rejections like 201."""
        if errorCode in (2104, 2106, 2158):
            # Routine farm-status notices arrive constantly; skip the
            # Contract repr (a full dataclass format) for them.
            tws_log.warning("[%s] EVENT     errorEvent → reqId=%s "
                            "errorCode=%s errorString=%s",
                            self._label, reqId, errorCode, errorString)
        else:
            tws_log.warning("[%s] EVENT     errorEvent → reqId=%s "
                            "errorCode=%s errorString=%s contract=%s",
                            self._label, reqId, errorCode, errorString,
                            contract)

        # Error 354 — "Requested market data is not subscribed."  The
        # options ticket first tries frozen data (type 2), which needs a
        # live entitlement; accounts with delayed-only data get 354 back.
        # Flip to delayed-frozen (type 4) once and resubscribe the batch.
        if (errorCode == 354
                and not self._opt_delayed_fallback
                and getattr(contract, "conId", 0) in self._opt_tickers):
            self._opt_delayed_fallback = True
            log.warning("Worker %s: no live entitlement for options ticket "
                        "(error 354) – resubscribing with delayed-frozen data",
                        self._label)
            asyncio.ensure_future(self._do_option_quotes(
                self._opt_quote_token, self._opt_quote_contracts))

        # Depth (L2) request refused — 10092 = depth not offered for this
        # secType/exchange combination, 354 = no depth entitlement (e.g.
        # CME depth on a paper account).  Tell the ladder so it renders
        # from L1, and forget the dead ticker so the next
        # ``subscribe_depth`` for the symbol isn't skipped as "already
        # subscribed".  Resolved through ib_async's reqId→ticker map
        # rather than the contract's conId: the L1 stream for the same
        # contract can raise 354 too, and that must not kill the depth.
        if errorCode in (10092, 354):
            d_sym = self._depth_sym_for_req_id(reqId)
            if d_sym:
                log.warning("Worker %s: depth unavailable for %s "
                            "(error %s) – ladder falls back to L1",
                            self._label, d_sym, errorCode)
                self._drop_depth_state(d_sym)
                self.market_data_failed.emit(d_sym)

        # Background quote stream refused (no entitlement for the venue
        # TWS routed it to) — surface it and keep re-requesting.
        if errorCode in _L1_REFUSED_CODES:
            l1_sym = self._bg_sym_for_l1_req_id(reqId)
            if l1_sym:
                self._on_l1_refused(l1_sym, errorCode)

        if errorCode == 10197 and self._active_ticker is not None:
            # "No market data during competing live session"
            # The paper account can't get data – signal the UI to try another worker.
            sym = self._active_price_symbol
            log.warning("Worker %s: market data blocked for '%s' (error 10197) – "
                        "signalling fallback", self._label, sym)
            if sym:
                self.market_data_failed.emit(sym)

        # ── Order-related errors ───────────────────────────────────────────
        #
        # Error 201 — Order rejected by IB/TWS (e.g. insufficient funds).
        #   → Persist the reason and surface it as a tooltip on the order row.
        #
        # Error 202 — Cancel request rejected by TWS.
        #   This fires when we call cancelOrder() on an order that TWS has
        #   already filled or cancelled.  This is a natural race condition:
        #   our in-memory trade.orderStatus.status is a local cache updated
        #   asynchronously via orderStatusEvent; TWS may have filled the order
        #   milliseconds before our cancel arrived.  The order itself is fine —
        #   do NOT write an error tooltip.  The orderStatusEvent ("Filled") will
        #   update the row correctly on its own.
        #
        # Error 399 — Order message error (e.g. invalid TIF for the exchange).
        #   → Treat the same as 201.

        if errorCode == 201 or errorCode == 399:
            # Order was rejected by IB — find the trade and surface the reason.
            matched_trade = None
            order_ref = ""
            for oid, t in self._trades.items():
                if getattr(t.order, "orderId", None) == reqId:
                    matched_trade = t
                    order_ref = getattr(t.order, "orderRef", "") or ""
                    break

            if order_ref.startswith("nano_"):
                clean_msg = (errorString
                             .replace("<br />", "\n")
                             .replace("<br/>", "\n")
                             .replace("<br>", "\n")
                             .strip())
                self._order_error_messages[order_ref] = clean_msg

                order_log.warning("[%s] ORDER-REJECT  orderRef=%s errorCode=%s: %s",
                                  self._label, order_ref, errorCode, clean_msg)
                log.warning("Worker %s: order %s rejected (code %s): %s",
                            self._label, order_ref, errorCode, clean_msg)

                if matched_trade:
                    d = self._build_trade_dict(matched_trade)
                    if d:
                        self._rejected_order_cache[d["order_id"]] = d
                        self.order_status_updated.emit(d)

        elif errorCode == 202:
            # Cancel request rejected — log only, no tooltip.
            # The order's own status event (Filled / Cancelled) will update
            # the UI row; writing an error here would be misleading.
            order_log.warning("[%s] ORDER-CANCEL-REJECTED  reqId=%s: %s",
                              self._label, reqId, errorString)
            log.warning("Worker %s: cancel rejected for reqId=%s (likely race "
                        "condition — order already filled/cancelled): %s",
                        self._label, reqId, errorString)

    # ── Global order listener ─────────────────────────────────────────────────

    @staticmethod
    def _trade_order_key(order, order_status) -> str:
        """Canonical order key for a Trade — the exact derivation
        ``_build_trade_dict`` uses (see its docstring for the rules).
        Split out so ``_on_global_order_status`` can decide to skip a
        handled order without building the full dict."""
        order_ref = getattr(order, "orderRef", "") or ""
        acct = getattr(order, "account", "") or ""
        if order_ref.startswith("nano_"):
            return f"{acct}_{order_ref}"
        perm_id = getattr(order, "permId", 0) or getattr(
            order_status, "permId", 0)
        if perm_id:
            return f"{acct}_p{perm_id}"
        return f"{acct}_{order.orderId}"

    def _build_trade_dict(self, trade, fill_map: dict | None = None) -> dict | None:
        """Build an order-status dict from an ib_async Trade object.

        Returns ``None`` if the trade has no usable status.
        Shared by ``_on_global_order_status`` and ``_snapshot_existing_orders``.

        Key strategy
        ------------
        * **Internal (NanoPulse) orders** — ``orderRef`` starts with ``nano_``.
          The canonical key is ``{account}_{orderRef}``.  Account numbers are
          immutable (assigned by IBKR), so the key survives connection-label
          renames without any migration.
        * **External orders** (TWS / other clients) — keyed by
          ``{account}_p{permId}`` (globally unique, survives restarts).
          Falls back to ``{account}_{orderId}``.

        Parameters
        ----------
        fill_map:
            Optional ``{permId: (avgPrice, cumQty)}`` lookup built from
            ``self._ib.fills()`` / ``reqExecutionsAsync``.  Used to enrich
            completed orders whose ``OrderStatus`` lacks fill data.
        """
        try:
            status = trade.orderStatus.status if trade.orderStatus else ""
            if not status:
                return None

            order = trade.order
            contract = trade.contract

            # ── Unique key (account-based, survives label renames) ────────
            order_ref = getattr(order, "orderRef", "") or ""
            perm_id = getattr(order, "permId", 0) or getattr(
                trade.orderStatus, "permId", 0)
            acct = getattr(order, "account", "") or ""

            # Single source of truth for the key — shared with the
            # global handler's early skip.
            order_id = self._trade_order_key(order, trade.orderStatus)

            # ── Fill price ────────────────────────────────────────────────
            # 1. orderStatus.avgFillPrice (populated for live orders)
            fill_price = "–"
            avg = getattr(trade.orderStatus, "avgFillPrice", 0.0)
            if avg and avg > 0:
                fill_price = f"{avg:.2f}"

            # 2. trade.fills (populated for orders filled during session)
            if fill_price == "–" and getattr(trade, "fills", None):
                total_qty_f = 0.0
                weighted_sum = 0.0
                for f in trade.fills:
                    ex = f.execution
                    if ex.shares and ex.price:
                        total_qty_f += ex.shares
                        weighted_sum += ex.shares * ex.price
                if total_qty_f > 0:
                    fill_price = f"{weighted_sum / total_qty_f:.2f}"

            # 3. fill_map from reqExecutionsAsync (for completed orders
            #    whose OrderStatus was created with defaults)
            if fill_price == "–" and fill_map and perm_id:
                entry = fill_map.get(perm_id)
                if entry:
                    avg_p = entry[0]
                    if avg_p and avg_p > 0:
                        fill_price = f"{avg_p:.2f}"

            # ── Quantity ──────────────────────────────────────────────────
            qty = getattr(order, "totalQuantity", 0)
            # completedOrder may leave totalQuantity at 0; fall back to
            # orderStatus.filled or the fill_map cumulative qty.
            if not qty or qty == 0:
                qty = getattr(trade.orderStatus, "filled", 0)
            if (not qty or qty == 0) and fill_map and perm_id:
                entry = fill_map.get(perm_id)
                if entry:
                    cum = entry[1]
                    if cum and cum > 0:
                        qty = cum
            quantity = str(int(qty)) if qty else "0"

            symbol = (getattr(contract, "localSymbol", "")
                      or getattr(contract, "symbol", ""))

            # Fallback 1: symbol cached at placement time.  Option orders
            # are placed on a conId-only contract, so this is the only
            # source for their display symbol on reject/snapshot emits.
            if not symbol:
                symbol = self._order_symbols.get(order_ref, "")

            # Fallback 2: if symbol is still empty (contract built with conId
            # only and ib_async didn't update it), resolve from the chip /
            # hot-button instrument entries.
            if not symbol:
                con_id = getattr(contract, "conId", 0)
                if con_id:
                    try:
                        from src.core import instruments
                        sc = instruments.find_by_conid(con_id)
                        if sc:
                            symbol = (sc.get("local_symbol", "")
                                      or sc.get("symbol", ""))
                    except Exception:
                        pass

            lmt = getattr(order, "lmtPrice", 0)
            stp = getattr(order, "auxPrice", 0)

            # ── Timestamp ──────────────────────────────────────────────
            # 1. trade.log – best source for live/in-session orders.
            et_dt = None
            log_entries = getattr(trade, "log", None)
            if log_entries:
                last_entry = log_entries[-1]
                et_dt = _to_eastern(getattr(last_entry, "time", None))

            # 2. order.completedTime – TWS provides this for completed
            #    orders (format "yyyymmdd-HH:MM:SS" or similar).
            if et_dt is None:
                ct = getattr(order, "completedTime", "") or ""
                if ct:
                    et_dt = _parse_ib_time(ct)

            # 3. trade.fills – execution timestamp.
            if et_dt is None and getattr(trade, "fills", None):
                try:
                    last_fill = trade.fills[-1]
                    ft = getattr(last_fill.execution, "time", None)
                    if ft:
                        et_dt = _to_eastern(ft)
                except Exception:
                    pass

            # 4. fill_map time (keyed by permId).
            if et_dt is None and fill_map and perm_id:
                fm_entry = fill_map.get(perm_id)
                if fm_entry and len(fm_entry) >= 3:
                    et_dt = fm_entry[2]  # Eastern datetime stored by snapshot

            # 5. orderRef timestamp — NanoPulse embeds MMDD_HHMMSS in every
            #    orderRef, so we always know the submission time even for
            #    cancelled orders that TWS stops returning after a reconnect.
            #    This is more reliable than _order_times because it requires
            #    no prior observation of the order in the current session.
            if et_dt is None and order_ref.startswith("nano_"):
                et_dt = _parse_orderref_time(order_ref)

            # 6. Persistent timestamp cache — first valid time we ever observed
            #    for this order.  Cancelled orders have no completedTime and no
            #    fills, so after a reconnect all four sources above are empty and
            #    they show 00:00:00.  This cache fixes that.
            if et_dt is None and order_ref:
                cached_iso = self._order_times.get(order_ref)
                if cached_iso:
                    try:
                        et_dt = datetime.fromisoformat(cached_iso)
                    except Exception:
                        pass

            # Fallback: start of today so unknown-time orders sort to bottom.
            if et_dt is None:
                et_dt = datetime.now(_ET).replace(
                    hour=0, minute=0, second=0, microsecond=0)
            timestamp = et_dt.strftime("%H:%M:%S")
            time_sort = et_dt.isoformat()

            # Persist the first valid (non-midnight) timestamp so future
            # refreshes/reconnects can recover it via step 5 above.
            if order_ref and order_ref.startswith("nano_"):
                if et_dt.hour != 0 or et_dt.minute != 0 or et_dt.second != 0:
                    self._order_times.setdefault(order_ref, et_dt.isoformat())

            return {
                "order_id": order_id,
                "order_ref": getattr(order, "orderRef", "") or "",
                "symbol": symbol,
                # Contract identity for jump-to-Book-Trader (see the
                # pre-check emit).  secType may be "" for orders placed
                # on a lean conId-only contract.
                "con_id": getattr(contract, "conId", 0) or 0,
                "sec_type": getattr(contract, "secType", "") or "",
                "account": getattr(order, "account", ""),
                "action": getattr(order, "action", ""),
                "order_type": getattr(order, "orderType", "MKT"),
                "quantity": quantity,
                "limit_price": str(lmt) if lmt else "–",
                "stop_price": str(stp) if stp else "–",
                "tif": getattr(order, "tif", "DAY"),
                "fill_price": fill_price,
                "status": status,
                "time": timestamp,
                "time_sort": time_sort,
                # Persisted rejection reason keyed by orderRef — survives
                # reconnect because _order_error_messages is never cleared.
                "error_message": self._order_error_messages.get(order_ref, ""),
            }
        except Exception:
            log.debug("Worker %s: _build_trade_dict error",
                      self._label, exc_info=True)
            return None

    def _flatten_fill(self, fill) -> dict:
        """Flatten an ib_async ``Fill`` into a dict matching the Trades
        view schema.  Handles missing attributes gracefully.

        ``commission`` / ``realized_pnl`` are ``None`` when the
        ``commissionReport`` has not populated yet (e.g. a live
        ``execDetailsEvent`` arriving before TWS's commission callback).
        They become real floats once ``reqExecutionsAsync`` refreshes
        the snapshot on the next (re)connection.
        """
        ex = getattr(fill, "execution", None)
        contract = getattr(fill, "contract", None)
        report = getattr(fill, "commissionReport", None)

        raw_time = getattr(ex, "time", None) if ex is not None else None
        et_time = _to_eastern(raw_time) if raw_time else None

        exec_id = getattr(ex, "execId", "") if ex is not None else ""

        # ib_async attaches a default-initialised ``CommissionReport`` to
        # every ``Fill`` (all fields 0.0, execId='').  Treat it as "not
        # populated yet" — the real report arrives via commissionReportEvent,
        # which we don't subscribe to, so values only appear once a
        # subsequent reqExecutionsAsync snapshot refreshes the fill.
        commission: float | None = None
        realized_pnl: float | None = None
        report_populated = (
            report is not None and bool(getattr(report, "execId", ""))
        )
        if report_populated:
            c = getattr(report, "commission", None)
            if c is not None:
                cf = float(c)
                if not math.isnan(cf) and abs(cf) < _IB_DBL_MAX:
                    commission = cf
            r = getattr(report, "realizedPNL", None)
            if r is not None:
                rf = float(r)
                if not math.isnan(rf) and abs(rf) < _IB_DBL_MAX:
                    realized_pnl = rf

        d = {
            "exec_id":       exec_id,
            "time":          et_time.strftime("%b%d %H:%M:%S") if et_time else "",
            "time_sort":     et_time.isoformat() if et_time else "",
            "con_id":        int(getattr(contract, "conId", 0) or 0) if contract else 0,
            "symbol":        getattr(contract, "symbol", "") if contract else "",
            "local_symbol":  getattr(contract, "localSymbol", "") if contract else "",
            "sec_type":      getattr(contract, "secType", "") if contract else "",
            # Contract multiplier (e.g. "20" for NQ, "2" for MNQ, "" or
            # "1" for stocks).  Stored as float so downstream consumers
            # can scale per-point notionals into dollar P&L for futures.
            "multiplier":    _parse_multiplier(
                getattr(contract, "multiplier", "") if contract else ""),
            "side":          getattr(ex, "side", "") if ex is not None else "",
            "shares":        float(getattr(ex, "shares", 0) or 0) if ex is not None else 0.0,
            "price":         float(getattr(ex, "price", 0) or 0) if ex is not None else 0.0,
            "exchange":      getattr(ex, "exchange", "") if ex is not None else "",
            "order_ref":     getattr(ex, "orderRef", "") if ex is not None else "",
            "account":       getattr(ex, "acctNumber", "") if ex is not None else "",
            "expiry":        getattr(contract, "lastTradeDateOrContractMonth", "") if contract else "",
            "strike":        float(getattr(contract, "strike", 0) or 0) if contract else 0.0,
            "right":         getattr(contract, "right", "") if contract else "",
            "commission":    commission,
            "realized_pnl":  realized_pnl,
            "order_id":      int(getattr(ex, "orderId", 0) or 0) if ex is not None else 0,
            "client_id":     int(getattr(ex, "clientId", 0) or 0) if ex is not None else 0,
            "liquidation":   int(getattr(ex, "liquidation", 0) or 0) if ex is not None else 0,
            "last_liquidity": int(getattr(ex, "lastLiquidity", 0) or 0) if ex is not None else 0,
            "label":         self._label,
        }

        # Option orders are placed on a lean conId-only contract (TWS
        # error 478 workaround in _do_place_order), and ib_async hands
        # live fills that same contract — blank symbol/secType and no
        # multiplier.  Backfill the blanks from the metadata captured at
        # placement time so the Trades view gets a labeled row and the
        # OPT multiplier default (×100) can kick in.
        if d["con_id"] and not d["symbol"] and not d["local_symbol"]:
            meta = self._conid_contract_meta.get(d["con_id"])
            if meta:
                d["symbol"] = meta["symbol"]
                d["local_symbol"] = meta["local_symbol"]
                d["sec_type"] = d["sec_type"] or meta["sec_type"]
                if d["multiplier"] == 1.0:
                    d["multiplier"] = _parse_multiplier(meta["multiplier"])
                if not d["expiry"]:
                    d["expiry"] = meta["expiry"]
                if not d["strike"]:
                    d["strike"] = float(meta["strike"] or 0.0)
                if not d["right"]:
                    d["right"] = meta["right"]
        return d

    def _on_exec_details(self, trade, fill) -> None:
        """Live execDetailsEvent → emit + cache a flattened fill dict."""
        try:
            d = self._flatten_fill(fill)
        except Exception:
            log.debug("Worker %s: _on_exec_details flatten error",
                      self._label, exc_info=True)
            return
        exec_id = d.get("exec_id", "")
        if exec_id:
            self._fills_cache[exec_id] = d
        tws_log.info("[%s] EVENT     execDetailsEvent → execId=%s %s %s %s @ %s",
                     self._label, exec_id, d.get("symbol", ""),
                     d.get("side", ""), d.get("shares", 0), d.get("price", 0))
        self.fill_updated.emit(d)

    def _on_commission_report(self, trade, fill, report) -> None:
        """commissionReportEvent → re-flatten & re-emit.

        ib_async attaches ``report`` onto ``fill.commissionReport`` before
        firing this event, so ``_flatten_fill`` naturally picks up the
        populated commission / realizedPNL values.  The downstream models
        dedupe by execId, so re-emitting updates the existing row in the
        flat view and correctly re-aggregates the summary view.
        """
        try:
            d = self._flatten_fill(fill)
        except Exception:
            log.debug("Worker %s: _on_commission_report flatten error",
                      self._label, exc_info=True)
            return
        exec_id = d.get("exec_id", "")
        if exec_id:
            self._fills_cache[exec_id] = d
        tws_log.debug("[%s] EVENT     commissionReportEvent → execId=%s "
                      "commission=%s realizedPNL=%s",
                      self._label, exec_id,
                      d.get("commission"), d.get("realized_pnl"))
        self.fill_updated.emit(d)

    async def _snapshot_fills(self) -> None:
        """Fetch the replayable fills and emit each through ``fill_updated``.

        Uses ``reqExecutionsAsync`` to trigger/refresh the fetch, then
        reads from ``ib.fills()`` — the internal ib_async cache — which
        is where ib_async merges ``commissionReport`` data in.  The list
        returned by ``reqExecutionsAsync`` itself holds *fresh* Fill
        objects whose ``CommissionReport`` is never populated (commission
        arrives on a separate callback after execDetailsEnd); ``ib.fills()``
        holds the authoritative merged copies.

        MOST of the merged copies are still report-less at this moment,
        though: TWS streams the snapshot's commissionReports *after*
        execDetailsEnd (measured: 25 of 182 had arrived by the read), and
        for a fill replayed from a previous session ib_async merges the
        late report WITHOUT firing ``commissionReportEvent`` — the wrapper
        gates that emit on ``permId2Trade``, which only knows this
        session's own live orders.  Nothing re-emits those rows, so the
        Comm / R.P&L cells stayed blank until the next reconnect.
        ``_sweep_late_commissions`` (kicked off below) closes that gap.

        Called on every (re)connection, after ``_snapshot_existing_orders``.
        Cached in ``self._fills_cache`` so ``request_fills_snapshot`` can
        replay them when the UI wires in late without another API call.
        """
        if self._ib is None or not self._ib.isConnected():
            return

        try:
            from ib_async import ExecutionFilter
            tws_log.info("[%s] API-CALL  reqExecutionsAsync() [Trades snapshot]",
                         self._label)
            await self._ib.reqExecutionsAsync(ExecutionFilter())
        except Exception:
            log.exception("Worker %s: _snapshot_fills reqExecutions failed",
                          self._label)
            tws_log.error(
                "[%s] API-ERR   reqExecutionsAsync [Trades] failed", self._label)
            return

        try:
            fills = list(self._ib.fills())
        except Exception:
            log.exception("Worker %s: ib.fills() read failed", self._label)
            fills = []
        tws_log.info("[%s] API-RESP  ib.fills() → %d fill(s) [Trades]",
                     self._label, len(fills))

        def _key(f):
            ex = getattr(f, "execution", None)
            return getattr(ex, "time", None) or datetime.min.replace(tzinfo=timezone.utc)

        emitted = 0
        for f in sorted(fills, key=_key):
            try:
                d = self._flatten_fill(f)
            except Exception:
                log.debug("Worker %s: _snapshot_fills flatten error",
                          self._label, exc_info=True)
                continue
            exec_id = d.get("exec_id", "")
            if exec_id:
                self._fills_cache[exec_id] = d
            self.fill_updated.emit(d)
            emitted += 1
        tws_log.info("[%s] SNAPSHOT  fills emitted %d (cache size=%d)",
                     self._label, emitted, len(self._fills_cache))

        # Back-fill the commissions that arrive after execDetailsEnd (see
        # the docstring).  A background task so the connect path isn't
        # held up; restarted (not stacked) on every re-snapshot.
        if self._commission_sweep_task is not None:
            self._commission_sweep_task.cancel()
        self._commission_sweep_task = asyncio.ensure_future(
            self._sweep_late_commissions())

    async def _sweep_late_commissions(self) -> None:
        """Re-emit snapshot fills once their commissionReport has merged.

        Polls ``ib.fills()`` for a short while after a snapshot and
        re-emits every cached fill whose dict says ``commission is None``
        but whose merged copy now carries a populated report.  The models
        dedupe by execId, so each re-emit updates the existing row.  Exits
        as soon as nothing is missing; the deadline covers fills whose
        report legitimately never comes (TWS omits a few).
        """
        deadline = time.monotonic() + 30.0
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(2.0)
                if (not self._running or self._ib is None
                        or not self._ib.isConnected()):
                    return
                missing = {eid for eid, d in self._fills_cache.items()
                           if d.get("commission") is None}
                if not missing:
                    return
                healed = 0
                try:
                    fills = list(self._ib.fills())
                except Exception:
                    continue
                for f in fills:
                    ex = getattr(f, "execution", None)
                    eid = getattr(ex, "execId", "") if ex is not None else ""
                    if eid not in missing:
                        continue
                    report = getattr(f, "commissionReport", None)
                    if report is None or not getattr(report, "execId", ""):
                        continue    # still not merged
                    try:
                        d = self._flatten_fill(f)
                    except Exception:
                        continue
                    self._fills_cache[eid] = d
                    self.fill_updated.emit(d)
                    healed += 1
                if healed:
                    tws_log.info(
                        "[%s] SNAPSHOT  commission back-fill re-emitted %d "
                        "fill(s), %d still without a report",
                        self._label, healed, len(missing) - healed)
        except asyncio.CancelledError:
            pass    # superseded by a newer snapshot, or shutting down

    def _on_global_order_status(self, trade) -> None:
        """Global ``orderStatusEvent`` callback – fires for **every** order
        status change on this IB connection, including orders that were placed
        before a disconnect/reconnect cycle and orders placed manually in TWS.

        Only skip orders that have a dedicated per-trade ``_on_status``
        closure (placed by ``_do_place_order`` in this session).  Orders
        restored from snapshot do NOT have closures, so we must emit for
        them here — otherwise cancel/fill updates after reconnect are lost.
        """
        # Skip BEFORE building the full dict: this global callback fires
        # for every status of every order, and for session-placed orders
        # the per-trade closure already emits AND writes the same
        # tws_log/order_log lines — paying ~200 lines of attribute
        # walking (plus a duplicate disk write) per event just to throw
        # the result away was measurable all day.
        try:
            if (self._trade_order_key(trade.order, trade.orderStatus)
                    in self._has_status_handler):
                return
        except Exception:
            pass   # fall through to the full build, which guards itself

        try:
            self._maybe_settle_pending_from_trade(trade)
        except Exception:
            order_log.exception("[%s] BRACKET-DEFER  pending-exit check "
                                "failed", self._label)

        d = self._build_trade_dict(trade)
        if not d:
            return

        # Log every unhandled global order status event to TWS log
        tws_log.info("[%s] EVENT     global orderStatusEvent → orderId=%s "
                     "status=%s symbol=%s action=%s filled=%s avgFill=%s "
                     "orderRef=%s permId=%s",
                     self._label,
                     getattr(trade.order, "orderId", "?"),
                     d.get("status", "?"),
                     d.get("symbol", "?"),
                     d.get("action", "?"),
                     getattr(trade.orderStatus, "filled", 0),
                     getattr(trade.orderStatus, "avgFillPrice", 0),
                     d.get("order_ref", ""),
                     getattr(trade.order, "permId", 0))

        order_id = d["order_id"]

        # Log to order log for external or snapshot-restored orders
        order_log.info("[%s] ORDER-STATUS  %s | status=%s symbol=%s "
                       "account=%s action=%s filled=%s avgFill=%s orderRef=%s "
                       "(via global handler)",
                       self._label, order_id, d.get("status", "?"),
                       d.get("symbol", "?"), d.get("account", ""),
                       d.get("action", "?"),
                       getattr(trade.orderStatus, "filled", 0),
                       getattr(trade.orderStatus, "avgFillPrice", 0),
                       d.get("order_ref", ""))

        # Register in _trades so cancel routing works for orders that
        # arrive after the initial snapshot (e.g. TWS delivers them
        # late after a restart while ib_async is already connected).
        if order_id not in self._trades:
            self._trades[order_id] = trade
            log.debug("Worker %s: global handler registered %s in _trades",
                      self._label, order_id)

        self.order_status_updated.emit(d)

    async def _snapshot_existing_orders(self) -> None:
        """Emit current open trades and today's completed orders so the
        Order Status table is populated immediately after app restart
        or reconnection.

        Uses fresh TWS requests (not local caches) so that a manual
        refresh always picks up orders placed in TWS or by other clients.
        """
        if self._ib is None or not self._ib.isConnected():
            return

        emitted = 0
        seen_ids: set[str] = set()

        # Build a fill lookup from fresh execution reports.
        # Keyed by permId → (avgPrice, cumQty, et_datetime).
        # This enriches completed orders whose OrderStatus lacks fill data
        # and provides a timestamp when trade.log is empty.
        fill_map: dict[int, tuple] = {}
        try:
            from ib_async import ExecutionFilter
            tws_log.info("[%s] API-CALL  reqExecutionsAsync()", self._label)
            fresh_fills = await self._ib.reqExecutionsAsync(ExecutionFilter())
            tws_log.info("[%s] API-RESP  reqExecutionsAsync → %d execution(s)",
                         self._label, len(fresh_fills))
            for f in fresh_fills:
                ex = f.execution
                pid = getattr(ex, "permId", 0)
                if pid:
                    ex_time = getattr(ex, "time", None)
                    et_time = _to_eastern(ex_time) if ex_time else None
                    fill_map[pid] = (ex.avgPrice, ex.cumQty, et_time)
            if fill_map:
                log.info("Worker %s: built fill_map with %d execution(s)",
                         self._label, len(fill_map))
        except Exception:
            log.debug("Worker %s: fill_map build error",
                      self._label, exc_info=True)
            tws_log.error(
                "[%s] API-ERR   reqExecutionsAsync failed", self._label)

        try:
            # 1. Open / working orders – fresh request to TWS (all clients).
            tws_log.info("[%s] API-CALL  reqAllOpenOrdersAsync()", self._label)
            open_trades = await self._ib.reqAllOpenOrdersAsync()
            log.info("Worker %s: snapshot found %d open trade(s)",
                     self._label, len(open_trades))
            tws_log.info("[%s] API-RESP  reqAllOpenOrdersAsync → %d open trade(s)",
                         self._label, len(open_trades))
            for trade in open_trades:
                d = self._build_trade_dict(trade, fill_map)
                if d and d["order_id"] not in seen_ids:
                    seen_ids.add(d["order_id"])
                    self.order_status_updated.emit(d)
                    emitted += 1
                    # Register in _trades so cancel works
                    self._trades[d["order_id"]] = trade
                    order_log.info("[%s] ORDER-SNAPSHOT  %s | status=%s symbol=%s "
                                   "action=%s qty=%s fill=%s (open)",
                                   self._label, d["order_id"], d.get("status"),
                                   d.get("symbol"), d.get("action"),
                                   d.get("quantity"), d.get("fill_price"))
        except Exception:
            log.exception("Worker %s: snapshot open trades error", self._label)
            tws_log.error(
                "[%s] API-ERR   reqAllOpenOrdersAsync failed", self._label)

        try:
            # 2. Completed (filled / cancelled) orders from today's session.
            #    reqCompletedOrdersAsync(apiOnly=False) includes ALL orders
            #    (manual TWS + all API clients).
            tws_log.info(
                "[%s] API-CALL  reqCompletedOrdersAsync(apiOnly=False)", self._label)
            completed = await self._ib.reqCompletedOrdersAsync(apiOnly=False)
            log.info("Worker %s: snapshot found %d completed order(s)",
                     self._label, len(completed))
            tws_log.info("[%s] API-RESP  reqCompletedOrdersAsync → %d completed order(s)",
                         self._label, len(completed))
            for trade in completed:
                d = self._build_trade_dict(trade, fill_map)
                if d and d["order_id"] not in seen_ids:
                    seen_ids.add(d["order_id"])
                    self.order_status_updated.emit(d)
                    emitted += 1
                    order_log.info("[%s] ORDER-SNAPSHOT  %s | status=%s symbol=%s "
                                   "action=%s qty=%s fill=%s (completed)",
                                   self._label, d["order_id"], d.get("status"),
                                   d.get("symbol"), d.get("action"),
                                   d.get("quantity"), d.get("fill_price"))
        except Exception:
            log.exception(
                "Worker %s: snapshot completed orders error", self._label)
            tws_log.error(
                "[%s] API-ERR   reqCompletedOrdersAsync failed", self._label)

        # 3. Re-emit cached rejected orders that TWS no longer returns.
        # Rejected (Inactive) orders vanish from both reqAllOpenOrders and
        # reqCompletedOrders within seconds.  Our cache keeps them visible
        # across every refresh and reconnect cycle.
        for oid, cached_d in self._rejected_order_cache.items():
            if oid not in seen_ids:
                # Refresh the error_message in case it was somehow updated
                order_ref = cached_d.get("order_ref", "")
                if order_ref:
                    cached_d["error_message"] = self._order_error_messages.get(
                        order_ref, cached_d.get("error_message", "")
                    )
                self.order_status_updated.emit(cached_d)
                seen_ids.add(oid)
                emitted += 1
                order_log.info("[%s] ORDER-SNAPSHOT  %s | status=%s (from rejected cache)",
                               self._label, oid, cached_d.get("status"))

        log.info("Worker %s: order snapshot emitted %d order(s)",
                 self._label, emitted)
        tws_log.info("[%s] API-RESP  _snapshot_existing_orders → total %d order(s) emitted",
                     self._label, emitted)
