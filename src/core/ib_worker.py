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

import asyncio
import logging
import math
import random
import string
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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
    """Generate a custom orderRef: ``nano_{MMDD}_{HHMMSS}_{4 random lowercase letters}``."""
    now = datetime.now(_ET)
    tag = "".join(random.choices(string.ascii_lowercase, k=4))
    return f"nano_{now:%m%d}_{now:%H%M%S}_{tag}"


def _parse_orderref_time(order_ref: str) -> datetime | None:
    """Extract the submission timestamp embedded in a NanoPulse orderRef.

    Format: ``nano_{MMDD}_{HHMMSS}_{4-letter tag}``
    Example: ``nano_0401_103045_abcd`` → 10:30:45 ET on April 1st.

    Uses the current year (edge case: orders placed on Dec 31 viewed on
    Jan 1 will show the wrong year, acceptable for an intraday blotter).
    Returns ``None`` if the format does not match.
    """
    try:
        parts = order_ref.split("_")
        # Expected: ["nano", "MMDD", "HHMMSS", "xxxx"]
        if len(parts) != 4 or parts[0] != "nano":
            return None
        mmdd, hhmmss = parts[1], parts[2]
        if len(mmdd) != 4 or len(hhmmss) != 6:
            return None
        month  = int(mmdd[:2])
        day    = int(mmdd[2:])
        hour   = int(hhmmss[:2])
        minute = int(hhmmss[2:4])
        second = int(hhmmss[4:])
        year   = datetime.now(_ET).year
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

try:
    from ib_async import IB, AccountValue
except ImportError:  # allow UI dev without TWS dependency installed
    IB = None  # type: ignore[assignment,misc]
    AccountValue = None  # type: ignore[assignment,misc]

from src.utils.logger import tws_log, order_log

log = logging.getLogger(__name__)


class IBWorkerThread(QThread):
    """One thread ↔ one TWS/Gateway connection."""

    # Signals funnelled to the main-thread model
    summary_batch_updated = Signal(list)          # list[tuple[str, str, str, str, str]]
    connection_status_changed = Signal(str, bool)  # (label, is_connected)
    browse_results = Signal(list)                  # list[dict] from reqContractDetails
    browse_failed = Signal(str)                    # error message
    price_updated = Signal(str, float)             # (symbol, last_price)
    bid_ask_updated = Signal(str, float, float)    # (symbol, bid, ask)
    market_data_failed = Signal(str)               # (symbol) – e.g. error 10197
    order_status_updated = Signal(dict)            # order status dict → main thread
    portfolio_updated = Signal(list)               # list[dict] – portfolio position batch
    bg_price_updated = Signal(str, float, float, float)  # (symbol, last, bid, ask) – background cache
    flatten_close_needed = Signal(dict)            # {symbol, account, action, qty, sec_type} → UI places the close order

    def __init__(
        self,
        connection_label: str,
        host: str = "127.1.1.1",
        port: int = 1000,
        client_id: int = 100,
        parent=None,
    ):
        super().__init__(parent)
        self._label = connection_label
        self._host = host
        self._port = port
        self._client_id = client_id
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ib: IB | None = None
        self._main_task: asyncio.Task | None = None

        # De-dupe buffer keyed by (account, tag) → (value, currency)
        self._buffer: dict[tuple[str, str], tuple[str, str]] = {}

        # Portfolio buffer keyed by (account, conId) → position dict
        self._portfolio_buffer: dict[tuple[str, int], dict] = {}

        # Persistent portfolio cache – keeps ALL last-known position dicts
        # so late-connecting UI consumers can get an instant snapshot.
        self._portfolio_cache: dict[tuple[str, int], dict] = {}

        # Market data subscription state
        self._active_ticker = None
        self._active_price_symbol: str = ""

        # Background market data cache: multiple simultaneous subscriptions
        self._bg_tickers: dict[str, object] = {}  # symbol → ib_async Ticker

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

        # First valid timestamp observed per orderRef (nano_...), ISO format.
        # Cancelled orders have no completedTime and no fills, so their
        # timestamp is lost after a reconnect.  This cache lets _build_trade_dict
        # and _emit_order_status restore the real time on every subsequent
        # refresh instead of falling back to 00:00:00.
        # NOT cleared on reconnect.
        self._order_times: dict[str, str] = {}

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
            asyncio.run_coroutine_threadsafe(self._snapshot_existing_orders(), self._loop)
        else:
            log.debug("Worker %s: request_order_snapshot but loop not ready", self._label)

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

    def request_portfolio_refresh(self) -> None:
        """Thread-safe: re-subscribe to account updates so IB re-pushes all positions.

        This forces IB to fire ``updatePortfolioEvent`` for every open
        position.  Useful after an order fill when the live event was
        missed or delayed.
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._do_portfolio_refresh(), self._loop)
        else:
            log.debug("Worker %s: request_portfolio_refresh but loop not ready", self._label)

    async def _do_portfolio_refresh(self) -> None:
        """Request account updates for each managed account to refresh positions."""
        if self._ib is None or not self._ib.isConnected():
            return
        for acct in self._ib.managedAccounts():
            self._ib.reqAccountUpdates(acct)
            tws_log.info("[%s] API-CALL  reqAccountUpdates(account=%s) [refresh]",
                         self._label, acct)
        log.debug("Worker %s: portfolio refresh (requested for %d account(s))",
                  self._label, len(self._ib.managedAccounts()))

    def request_browse(self, params: dict) -> None:
        """Thread-safe: browse all contracts matching params via reqContractDetails."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._do_browse(params), self._loop)
        else:
            log.warning("Worker %s: request_browse but loop not ready", self._label)
            self.browse_failed.emit("Worker event loop not ready.")

    def request_market_data(self, params: dict) -> None:
        """Thread-safe: subscribe to live market data for a contract.

        *params* must contain ``conid`` and optionally ``symbol``,
        ``exchange``, ``currency``.
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._do_market_data(params), self._loop)
        else:
            log.warning("Worker %s: request_market_data but loop not ready", self._label)

    def cancel_market_data(self) -> None:
        """Thread-safe: cancel the current market data subscription."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._do_cancel_market_data(), self._loop)
        else:
            self._active_ticker = None

    def place_order(self, params: dict) -> None:
        """Thread-safe: place an order on the worker's IB connection.

        *params* keys:
            conid, symbol, sec_type, exchange, currency, local_symbol,
            expiry, multiplier,
            account, action (BUY/SELL), qty, order_type (MKT/LMT),
            limit_price (for LMT), tif (DAY/GTC/…)
        """
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._do_place_order(params), self._loop)
        else:
            log.warning("Worker %s: place_order but loop not ready", self._label)

    def cancel_order(self, order_id: str) -> None:
        """Thread-safe: cancel an open order by *order_id*."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._do_cancel_order(order_id), self._loop)
        else:
            log.warning("Worker %s: cancel_order but loop not ready", self._label)

    def cancel_all_for_symbol(self, symbol: str, accounts: set[str]) -> None:
        """Thread-safe: cancel all open orders for *symbol* whose account is in *accounts*."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_cancel_all_for_symbol(symbol, accounts), self._loop)
        else:
            log.warning("Worker %s: cancel_all_for_symbol but loop not ready", self._label)

    def flatten(self, symbol: str, account: str) -> None:
        """Thread-safe: cancel all open orders for *symbol*/*account*, then
        close the position with an offsetting MKT order."""
        if self._loop is not None and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._do_flatten(symbol, account), self._loop)
        else:
            log.warning("Worker %s: flatten but loop not ready", self._label)

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
            log.warning("Worker %s: subscribe_background_symbols but loop not ready", self._label)

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

    # ── Thread entry-point ────────────────────────────────────────────────────

    def run(self) -> None:  # noqa: D401 – Qt override
        if IB is None:
            log.warning("ib_async not available – worker %s exiting.", self._label)
            return

        self._running = True
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        self._ib = IB()
        self._ib.accountSummaryEvent += self._on_account_summary
        self._ib.pnlEvent += self._on_pnl
        self._ib.errorEvent += self._on_ib_error
        self._ib.pendingTickersEvent += self._on_pending_tickers
        # Global order-status listener – catches ALL order updates
        # (including orders placed before a reconnect and TWS manual orders)
        self._ib.orderStatusEvent += self._on_global_order_status
        # Portfolio updates – fires for each PortfolioItem change
        self._ib.updatePortfolioEvent += self._on_portfolio_update

        tws_log.info("[%s] IB instance created – event subscriptions: "
                     "accountSummaryEvent, pnlEvent, errorEvent, "
                     "pendingTickersEvent, orderStatusEvent, updatePortfolioEvent",
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
                        log.debug("Worker %s disconnected from IB", self._label)
                        tws_log.info("[%s] API-CALL  disconnect() → OK", self._label)
                    except Exception as e:
                        log.warning("Worker %s: error during IB disconnect: %s", self._label, e)
                        tws_log.error("[%s] API-ERR   disconnect() failed: %s", self._label, e)

            self._main_task = None
            self._loop = None
            loop.close()
            log.info("Worker %s: thread shutdown complete", self._label)
            tws_log.info("[%s] SHUTDOWN  thread shutdown complete", self._label)
            self.connection_status_changed.emit(self._label, False)

    # ── Async core ────────────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        self._main_task = asyncio.current_task()
        await asyncio.gather(
            self._reconnect_loop(),
            self._throttler_loop(),
            return_exceptions=True,
        )

    async def _reconnect_loop(self) -> None:
        """Check connectivity every 1 s; reconnect every 5 s when down."""
        reconnect_countdown = 0.0
        while self._running:
            if self._ib.isConnected():
                reconnect_countdown = 0.0
                await asyncio.sleep(1)
                continue

            if reconnect_countdown <= 0:
                try:
                    log.info("Worker %s connecting to %s:%d (cid=%d) …",
                             self._label, self._host, self._port, self._client_id)
                    tws_log.info("[%s] API-CALL  connectAsync(host=%s, port=%d, clientId=%d)",
                                self._label, self._host, self._port, self._client_id)
                    await self._ib.connectAsync(
                        self._host, self._port, clientId=self._client_id,
                    )
                except Exception as exc:
                    log.warning("Worker %s connect failed: %s", self._label, exc)
                    tws_log.error("[%s] API-ERR   connectAsync FAILED: %s",
                                 self._label, exc)
                    self.connection_status_changed.emit(self._label, False)
                    reconnect_countdown = 5.0
                    await asyncio.sleep(1)
                    reconnect_countdown -= 1.0
                    continue

                # Connection established — update status immediately
                self.connection_status_changed.emit(self._label, True)
                log.info("Worker %s connected – seeding initial snapshot", self._label)
                tws_log.info("[%s] API-RESP  connectAsync → CONNECTED", self._label)

                # Old Trade objects and their _on_status closures are invalid
                # after a disconnect — ib_async clears its internal collection.
                # Reset tracking so the global handler doesn't skip status
                # updates for orders restored from the upcoming snapshot.
                self._trades.clear()
                self._has_status_handler.clear()
                self._bg_tickers.clear()

                # Post-connect setup: errors here should NOT reset the
                # connection status — the IB link is alive; only the
                # setup step failed and will be retried on next cycle.
                try:
                    # Forward manual TWS orders + surviving orders to this client
                    self._ib.reqAutoOpenOrders(True)
                    tws_log.info("[%s] API-CALL  reqAutoOpenOrders(True)", self._label)

                    # Seed the buffer from the snapshot that connectAsync
                    # already fetched during synchronisation.
                    tws_log.info("[%s] API-CALL  accountSummaryAsync()", self._label)
                    for v in await self._ib.accountSummaryAsync():
                        self._buffer[(v.account, v.tag)] = (v.value, v.currency)
                    log.info("Worker %s buffer seeded with %d tag(s)", self._label, len(self._buffer))
                    tws_log.info("[%s] API-RESP  accountSummaryAsync → %d tag(s)",
                                self._label, len(self._buffer))

                    # Subscribe to P&L stream for DailyPnL
                    for acct in self._ib.managedAccounts():
                        self._ib.reqPnL(acct)
                        log.info("Worker %s subscribed to pnlEvent for %s", self._label, acct)
                        tws_log.info("[%s] API-CALL  reqPnL(account=%s)", self._label, acct)

                    # Subscribe to portfolio updates (positions, market values)
                    for acct in self._ib.managedAccounts():
                        self._ib.reqAccountUpdates(acct)
                        log.info("Worker %s subscribed to portfolio for %s", self._label, acct)
                        tws_log.info("[%s] API-CALL  reqAccountUpdates(account=%s)", self._label, acct)

                    # Snapshot existing open + completed orders
                    tws_log.info("[%s] API-CALL  _snapshot_existing_orders()", self._label)
                    await self._snapshot_existing_orders()

                except Exception as exc:
                    log.warning("Worker %s post-connect setup error (connection still alive): %s",
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
                for (account, tag), (value, currency) in self._buffer.items():
                    batch.append((self._label, account, tag, value, currency))
                self._buffer.clear()
                if batch:
                    self.summary_batch_updated.emit(batch)

            # Emit portfolio batch
            if self._portfolio_buffer:
                p_batch = list(self._portfolio_buffer.values())
                self._portfolio_buffer.clear()
                self.portfolio_updated.emit(p_batch)


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
            c.exchange = params.get("exchange") or ("SMART" if sec_type == "STK" else "")
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
            log.warning("Worker %s browse failed: %s", self._label, exc)
            tws_log.error("[%s] API-ERR   browse failed: %s", self._label, exc)
            self.browse_failed.emit(str(exc))

    # ── Order placement ───────────────────────────────────────────────────────

    async def _do_place_order(self, params: dict) -> None:
        """Build Contract + Order, place via ib_async, bind trade.statusEvent."""
        if self._ib is None or not self._ib.isConnected():
            log.warning("Worker %s: place_order but not connected", self._label)
            return

        try:
            from ib_async import Contract, MarketOrder, LimitOrder, TagValue

            # ── Build contract ─────────────────────────────────────────────
            conid = int(params.get("conid", 0))
            if conid <= 0:
                log.warning("Worker %s: invalid conid for order: %s", self._label, conid)
                return

            c = Contract(conId=conid)
            c.exchange = params.get("exchange") or "SMART"
            # Populate symbol fields so the Trade object kept by ib_async
            # retains them across reqAllOpenOrdersAsync() snapshots (ib_async
            # does NOT overwrite the contract on existing Trade objects).
            c.symbol = params.get("symbol", "")
            c.secType = params.get("sec_type", "")
            c.currency = params.get("currency", "USD")
            c.localSymbol = params.get("local_symbol", "")
            if params.get("expiry"):
                c.lastTradeDateOrContractMonth = params["expiry"]
            if params.get("multiplier"):
                c.multiplier = params["multiplier"]

            # Stocks: route to IBEOS (IBKR Overnight ATS) during overnight
            # session (20:00–03:59 ET), otherwise SMART.
            sec_type = params.get("sec_type", "")
            if sec_type == "STK":
                now_et = datetime.now(_ET)
                hour = now_et.hour
                if hour >= 20 or hour < 4:
                    c.exchange = "IBEOS"
                    log.info("Worker %s: STK overnight → exchange=IBEOS (ET %02d:%02d)",
                             self._label, hour, now_et.minute)
                else:
                    c.exchange = "SMART"

            # ── Build order ───────────────────────────────────────────────
            action = params.get("action", "BUY")
            qty = int(params.get("qty", 1))
            order_type = params.get("order_type", "MKT")
            account = params.get("account", "")
            tif = params.get("tif", "DAY")

            if order_type == "ADAPTIVE":
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
            elif order_type == "PEG_PRIM":
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
            elif order_type == "LMT":
                limit_price = float(params.get("limit_price", 0))
                order = LimitOrder(action, qty, limit_price)
            else:
                order = MarketOrder(action, qty)

            order.account = account
            order.tif = tif
            order.orderRef = _generate_order_ref()

            # Stocks: configure extended-hours flags based on exchange.
            if sec_type == "STK":
                if c.exchange == "IBEOS":
                    # IBEOS is an overnight venue — no RTH flags needed.
                    # advancedErrorOverride bypasses TWS precautionary
                    # Error 10329 that blocks direct-routed IBEOS orders.
                    order.advancedErrorOverride = "10329"
                else:
                    order.outsideRth = True
                    order.allowPreOpen = False

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

            trade = self._ib.placeOrder(c, order)

            # Account + orderRef = immutable canonical key.
            # Account numbers never change; connection labels can be renamed.
            order_id = f"{account}_{order.orderRef}"
            self._trades[order_id] = trade
            self._has_status_handler.add(order_id)

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

            order_log.info("[%s] ORDER-STATUS  %s | status=%s symbol=%s account=%s",
                           self._label, order_id, status,
                           params.get("symbol", "?"), account)

            # ── Bind trade.statusEvent for live updates ───────────────────
            # The key never changes (orderRef-based), so the closure is
            # trivially simple — no migration, no supersedes.
            frozen_params = dict(params)
            frozen_oid = order_id

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

            trade.statusEvent += _on_status

        except Exception as exc:
            log.exception("Worker %s: place_order failed: %s", self._label, exc)
            tws_log.error("[%s] API-ERR   placeOrder failed: %s", self._label, exc)
            order_log.error("[%s] ORDER-ERROR  placeOrder failed: %s | params=%s",
                            self._label, exc, params)

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
            log.info("Worker %s: cancel requested for order %s", self._label, order_id)
            tws_log.info("[%s] API-CALL  cancelOrder(orderId=%s) for key=%s",
                         self._label, trade.order.orderId, order_id)
            order_log.info("[%s] ORDER-CANCEL  requested for %s | orderId=%s "
                           "symbol=%s orderRef=%s",
                           self._label, order_id, trade.order.orderId,
                           getattr(trade.contract, "symbol", "?"),
                           getattr(trade.order, "orderRef", ""))
        except Exception as exc:
            log.exception("Worker %s: cancel_order failed: %s", self._label, exc)
            tws_log.error("[%s] API-ERR   cancelOrder failed for %s: %s",
                          self._label, order_id, exc)
            order_log.error("[%s] ORDER-CANCEL  ERROR for %s: %s",
                            self._label, order_id, exc)

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
            contract = getattr(trade, "contract", None)
            if contract is None:
                continue
            trade_sym = (getattr(contract, "localSymbol", "") or
                         getattr(contract, "symbol", "")).upper()
            if trade_sym != symbol_upper:
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
            log.warning("Worker %s: flatten could not read positions", self._label)
            return

        for pos in positions:
            if getattr(pos, "account", "") != account:
                continue
            contract = pos.contract
            pos_sym = (getattr(contract, "localSymbol", "") or
                       getattr(contract, "symbol", "")).upper()
            if pos_sym != symbol_upper:
                continue

            net_pos = float(getattr(pos, "position", 0))
            if net_pos == 0:
                continue

            action = "SELL" if net_pos > 0 else "BUY"
            qty = abs(int(net_pos))

            order_log.info("[%s] ORDER-FLATTEN  %s %s %d | account=%s",
                           self._label, action, symbol, qty, account)

            # Emit to UI thread — the UI will route through the normal
            # order-placement path (limit cap, RTH, prefs, etc.).
            self.flatten_close_needed.emit({
                "symbol":  symbol,
                "account": account,
                "action":  action,
                "qty":     qty,
            })

    def _on_trade_status(self, trade, order_id: str, params: dict) -> None:
        """Callback fired by trade.statusEvent – runs in the worker's event loop.

        Emits ``order_status_updated`` Signal to safely pass data to the GUI thread.
        """
        status = trade.orderStatus.status
        log.info("Worker %s: order %s status → %s", self._label, order_id, status)
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

        d = {
            "order_id": order_id,
            "order_ref": order_ref,
            "symbol": params.get("symbol", ""),
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

    # ── ib_async callbacks ────────────────────────────────────────────────────

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
            # Single-symbol active subscription (existing behavior)
            if (self._active_ticker is not None
                    and self._active_price_symbol
                    and ticker is self._active_ticker):
                self._on_price_update(ticker)

            # Background multi-symbol subscriptions
            if self._bg_tickers:
                for sym, bg_ticker in self._bg_tickers.items():
                    if ticker is bg_ticker:
                        self._emit_bg_price(sym, ticker)
                        break

    async def _do_market_data(self, params: dict) -> None:
        """Subscribe to live top-of-book data for the contract in *params*."""
        if self._ib is None or not self._ib.isConnected():
            log.warning("Worker %s: market data requested but not connected", self._label)
            return

        # Cancel any existing subscription first
        await self._do_cancel_market_data()

        try:
            from ib_async import Contract
            conid = int(params.get("conid", 0))
            if conid <= 0:
                log.warning("Worker %s: invalid conid for market data: %s", self._label, conid)
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

            # Request delayed streaming data as fallback when real-time is
            # unavailable (e.g. error 10197 "competing live session").
            # Type 3 = delayed: streams 15-min delayed ticks continuously.
            self._ib.reqMarketDataType(3)

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
            log.warning("Worker %s: market data subscription failed: %s", self._label, exc)

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
                log.debug("Worker %s: cancel market data error: %s", self._label, exc)
            finally:
                self._active_ticker = None
                self._active_price_symbol = ""

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

        self._ib.reqMarketDataType(3)  # delayed as fallback

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
                ticker = self._ib.reqMktData(c, genericTickList="", snapshot=False)
                self._bg_tickers[sym] = ticker
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
        if ticker is not None and self._ib is not None and self._ib.isConnected():
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
        log.debug("Worker %s: all %d bg subscriptions cleared", self._label, count)

    def _emit_bg_price(self, sym: str, ticker) -> None:
        """Extract last/bid/ask from a background ticker and emit ``bg_price_updated``."""
        try:
            last = getattr(ticker, "last", float('nan'))
            bid = getattr(ticker, "bid", float('nan'))
            ask = getattr(ticker, "ask", float('nan'))

            bid_valid = not math.isnan(bid) and bid > 0 and bid < 1e12
            ask_valid = not math.isnan(ask) and ask > 0 and ask < 1e12

            # Derive last from midpoint if unavailable
            if math.isnan(last) or last <= 0:
                if bid_valid and ask_valid:
                    last = (bid + ask) / 2.0

            # Fallback to close
            if math.isnan(last) or last <= 0:
                close = getattr(ticker, "close", float('nan'))
                if not math.isnan(close) and close > 0:
                    last = close

            if math.isnan(last):
                last = 0.0
            bid_out = bid if bid_valid else 0.0
            ask_out = ask if ask_valid else 0.0

            self.bg_price_updated.emit(sym, last, bid_out, ask_out)
        except Exception:
            log.debug("Worker %s: bg price emit error for %s", self._label, sym)

    def _on_account_summary(self, value) -> None:
        """De-duplicate rapid ticks into the buffer dict.

        ``accountSummaryEvent`` emits a single :class:`AccountValue` per call.
        """
        try:
            self._buffer[(value.account, value.tag)] = (value.value, value.currency)
            tws_log.debug("[%s] EVENT     accountSummaryEvent → account=%s tag=%s "
                          "value=%s currency=%s",
                          self._label, value.account, value.tag,
                          value.value, value.currency)
        except Exception:
            log.debug("Worker %s: bad AccountValue: %s", self._label, value)

    def _on_pnl(self, entry) -> None:
        """Handle pnlEvent – provides dailyPnL, realizedPnL, unrealizedPnL.

        ``pnlEvent`` emits a single :class:`PnL` object per call.
        Fields may be ``float('nan')`` when unavailable.
        """
        try:
            acct = entry.account
            tws_log.debug("[%s] EVENT     pnlEvent → account=%s dailyPnL=%s "
                          "realizedPnL=%s unrealizedPnL=%s",
                          self._label, acct,
                          entry.dailyPnL, entry.realizedPnL, entry.unrealizedPnL)
            if entry.dailyPnL is not None and not math.isnan(entry.dailyPnL):
                self._buffer[(acct, "DailyPnL")] = (str(entry.dailyPnL), "BASE")
        except Exception:
            log.debug("Worker %s: bad PnL entry: %s", self._label, entry)

    def _on_portfolio_update(self, item) -> None:
        """Buffer a PortfolioItem update for throttled emission.

        ``updatePortfolioEvent`` fires once per position change.
        """
        try:
            contract = item.contract
            acct = getattr(item, "account", "")
            con_id = getattr(contract, "conId", 0)
            if not acct or not con_id:
                log.debug("Worker %s: portfolio update dropped (acct=%r, conId=%r)",
                          self._label, acct, con_id)
                return
            sym = getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")
            pos = float(getattr(item, "position", 0))
            log.debug("Worker %s: portfolio event %s %s pos=%.1f acct=%s",
                      self._label, sym, con_id, pos, acct)
            tws_log.debug("[%s] EVENT     updatePortfolioEvent → account=%s symbol=%s "
                          "conId=%s pos=%.1f avgCost=%.2f mktPrice=%.2f mktValue=%.2f "
                          "unrealizedPnL=%.2f realizedPnL=%.2f",
                          self._label, acct, sym, con_id, pos,
                          float(getattr(item, "averageCost", 0)),
                          float(getattr(item, "marketPrice", 0)),
                          float(getattr(item, "marketValue", 0)),
                          float(getattr(item, "unrealizedPNL", 0)),
                          float(getattr(item, "realizedPnL", 0)))
            self._portfolio_buffer[(acct, con_id)] = {
                "label": self._label,
                "account": acct,
                "con_id": con_id,
                "symbol": getattr(contract, "symbol", ""),
                "local_symbol": getattr(contract, "localSymbol", ""),
                "sec_type": getattr(contract, "secType", ""),
                "exchange": getattr(contract, "exchange", ""),
                "currency": getattr(contract, "currency", "USD"),
                "position": float(getattr(item, "position", 0)),
                "avg_cost": float(getattr(item, "averageCost", 0)),
                "market_price": float(getattr(item, "marketPrice", 0)),
                "market_value": float(getattr(item, "marketValue", 0)),
                "unrealized_pnl": float(getattr(item, "unrealizedPNL", 0)),
                "realized_pnl": float(getattr(item, "realizedPnL", 0)),
            }
            # Also persist into the long-lived cache for late subscribers
            self._portfolio_cache[(acct, con_id)] = self._portfolio_buffer[(acct, con_id)]
        except Exception:
            log.debug("Worker %s: bad portfolio item: %s", self._label, item)

    def _on_ib_error(self, reqId, errorCode, errorString, contract) -> None:
        """Handle IB error events – detect market-data failures like 10197
        and order rejections like 201."""
        tws_log.warning("[%s] EVENT     errorEvent → reqId=%s errorCode=%s "
                        "errorString=%s contract=%s",
                        self._label, reqId, errorCode, errorString, contract)

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
            perm_id = getattr(order, "permId", 0) or getattr(trade.orderStatus, "permId", 0)
            acct = getattr(order, "account", "") or ""

            if order_ref.startswith("nano_"):
                # Internal NanoPulse order — account + orderRef is immutable.
                order_id = f"{acct}_{order_ref}"
            elif perm_id:
                order_id = f"{acct}_p{perm_id}"
            else:
                order_id = f"{acct}_{order.orderId}"

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

            # Fallback: if symbol is still empty (contract built with conId
            # only and ib_async didn't update it), resolve from saved config.
            if not symbol:
                con_id = getattr(contract, "conId", 0)
                if con_id:
                    try:
                        from src.core import config_manager
                        for sc in config_manager.load_contracts():
                            if sc.get("conid") == con_id:
                                symbol = (sc.get("local_symbol", "")
                                          or sc.get("symbol", ""))
                                break
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
                et_dt = datetime.now(_ET).replace(hour=0, minute=0, second=0, microsecond=0)
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
            log.debug("Worker %s: _build_trade_dict error", self._label, exc_info=True)
            return None

    def _on_global_order_status(self, trade) -> None:
        """Global ``orderStatusEvent`` callback – fires for **every** order
        status change on this IB connection, including orders that were placed
        before a disconnect/reconnect cycle and orders placed manually in TWS.

        Only skip orders that have a dedicated per-trade ``_on_status``
        closure (placed by ``_do_place_order`` in this session).  Orders
        restored from snapshot do NOT have closures, so we must emit for
        them here — otherwise cancel/fill updates after reconnect are lost.
        """
        d = self._build_trade_dict(trade)
        if not d:
            return

        # Log every global order status event to TWS log
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

        # Only skip if a per-trade _on_status closure is handling this order.
        # Snapshot-registered trades (in _trades but NOT _has_status_handler)
        # must be emitted here so cancel/fill status reaches the UI.
        order_id = d["order_id"]
        if order_id in self._has_status_handler:
            return

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
                log.info("Worker %s: built fill_map with %d execution(s)", self._label, len(fill_map))
        except Exception:
            log.debug("Worker %s: fill_map build error", self._label, exc_info=True)
            tws_log.error("[%s] API-ERR   reqExecutionsAsync failed", self._label)

        try:
            # 1. Open / working orders – fresh request to TWS (all clients).
            tws_log.info("[%s] API-CALL  reqAllOpenOrdersAsync()", self._label)
            open_trades = await self._ib.reqAllOpenOrdersAsync()
            log.info("Worker %s: snapshot found %d open trade(s)", self._label, len(open_trades))
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
            tws_log.error("[%s] API-ERR   reqAllOpenOrdersAsync failed", self._label)

        try:
            # 2. Completed (filled / cancelled) orders from today's session.
            #    reqCompletedOrdersAsync(apiOnly=False) includes ALL orders
            #    (manual TWS + all API clients).
            tws_log.info("[%s] API-CALL  reqCompletedOrdersAsync(apiOnly=False)", self._label)
            completed = await self._ib.reqCompletedOrdersAsync(apiOnly=False)
            log.info("Worker %s: snapshot found %d completed order(s)", self._label, len(completed))
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
            log.exception("Worker %s: snapshot completed orders error", self._label)
            tws_log.error("[%s] API-ERR   reqCompletedOrdersAsync failed", self._label)

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

        log.info("Worker %s: order snapshot emitted %d order(s)", self._label, emitted)
        tws_log.info("[%s] API-RESP  _snapshot_existing_orders → total %d order(s) emitted",
                     self._label, emitted)
