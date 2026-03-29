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

log = logging.getLogger(__name__)


class IBWorkerThread(QThread):
    """One thread ↔ one TWS/Gateway connection."""

    # Signals funnelled to the main-thread model
    summary_batch_updated = Signal(list)          # list[tuple[str, str, str, str, str]]
    connection_status_changed = Signal(str, bool)  # (label, is_connected)
    browse_results = Signal(list)                  # list[dict] from reqContractDetails
    browse_failed = Signal(str)                    # error message
    price_updated = Signal(str, float)             # (symbol, last_price)
    market_data_failed = Signal(str)               # (symbol) – e.g. error 10197
    order_status_updated = Signal(dict)            # order status dict → main thread
    portfolio_updated = Signal(list)               # list[dict] – portfolio position batch

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

        # Order tracking: order_id → Trade
        self._trades: dict[str, object] = {}

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
            # Note: Modern ib_async doesn't support subscribe/unsubscribe toggles.
            # This will trigger fresh position data from the server.
            self._ib.reqAccountUpdates(acct)
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
                    except Exception as e:
                        log.warning("Worker %s: error during IB disconnect: %s", self._label, e)

            self._main_task = None
            self._loop = None
            loop.close()
            log.info("Worker %s: thread shutdown complete", self._label)
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
                    await self._ib.connectAsync(
                        self._host, self._port, clientId=self._client_id,
                    )
                    self.connection_status_changed.emit(self._label, True)
                    log.info("Worker %s connected – seeding initial snapshot", self._label)

                    # Forward manual TWS orders + surviving orders to this client
                    self._ib.reqAutoOpenOrders(True)

                    # Seed the buffer from the snapshot that connectAsync
                    # already fetched during synchronisation.
                    # Must use the async version to avoid re-entering the loop.
                    for v in await self._ib.accountSummaryAsync():
                        self._buffer[(v.account, v.tag)] = (v.value, v.currency)
                    log.info("Worker %s buffer seeded with %d tag(s)", self._label, len(self._buffer))

                    # Subscribe to P&L stream for DailyPnL
                    for acct in self._ib.managedAccounts():
                        self._ib.reqPnL(acct)
                        log.info("Worker %s subscribed to pnlEvent for %s", self._label, acct)

                    # Subscribe to portfolio updates (positions, market values)
                    for acct in self._ib.managedAccounts():
                        self._ib.reqAccountUpdates(acct)
                        log.info("Worker %s subscribed to portfolio for %s", self._label, acct)

                    # Snapshot existing open + completed orders so the
                    # Order Status table is populated immediately after
                    # app restart or reconnection.
                    await self._snapshot_existing_orders()

                except Exception as exc:
                    log.warning("Worker %s connect failed: %s", self._label, exc)
                    self.connection_status_changed.emit(self._label, False)
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
                    self.browse_results.emit(results)
                else:
                    self.browse_failed.emit(
                        f"No match found for STK {c.symbol} on {c.exchange}")
            else:
                # ── FUT / OPT: full browse via reqContractDetails ──────────
                log.info("Worker %s browsing: %s %s",
                         self._label, c.secType, c.symbol)
                details = await asyncio.wait_for(
                    self._ib.reqContractDetailsAsync(c), timeout=_TIMEOUT,
                )

                if not details:
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
                self.browse_results.emit(results)

        except asyncio.TimeoutError:
            log.warning("Worker %s browse timed out after %ds for %s %s",
                        self._label, _TIMEOUT,
                        params.get("sec_type"), params.get("symbol"))
            self.browse_failed.emit(
                f"Search timed out after {_TIMEOUT}s — TWS may have lost "
                f"connectivity. Please try again.")
        except Exception as exc:
            log.warning("Worker %s browse failed: %s", self._label, exc)
            self.browse_failed.emit(str(exc))

    # ── Order placement ───────────────────────────────────────────────────────

    async def _do_place_order(self, params: dict) -> None:
        """Build Contract + Order, place via ib_async, bind trade.statusEvent."""
        if self._ib is None or not self._ib.isConnected():
            log.warning("Worker %s: place_order but not connected", self._label)
            return

        try:
            from ib_async import Contract, MarketOrder, LimitOrder, TagValue

            # ── Build contract from conId only ────────────────────────────
            conid = int(params.get("conid", 0))
            if conid <= 0:
                log.warning("Worker %s: invalid conid for order: %s", self._label, conid)
                return

            c = Contract(conId=conid)
            c.exchange = params.get("exchange") or "SMART"

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

            # ── Place ─────────────────────────────────────────────────────
            trade = self._ib.placeOrder(c, order)

            # Use _build_trade_dict for consistent order_id generation.
            # At this point permId may still be 0, so it falls back to orderId.
            d = self._build_trade_dict(trade)
            if d:
                order_id = d["order_id"]
            else:
                order_id = f"{self._label}_{trade.order.orderId}"

            self._trades[order_id] = trade

            log.info("Worker %s: placed %s %s %d %s for account %s → %s",
                     self._label, action, order_type, qty,
                     params.get("symbol", "?"), account, order_id)

            # ── Emit initial status to UI ─────────────────────────────────
            status = trade.orderStatus.status if trade.orderStatus.status else "Submitted"
            self._emit_order_status(trade, order_id, params, status)

            # ── Bind trade.statusEvent for live updates ───────────────────
            # The global orderStatusEvent also catches these, but this
            # binding carries the original params (with our symbol name).
            frozen_params = dict(params)
            # Mutable container so the closure can update the tracked key
            # when migrating from orderId → permId.
            key_ref = [order_id]

            def _on_status(t, _kr=key_ref, _p=frozen_params):
                # Once TWS assigns a permId, re-register under the canonical key
                new_d = self._build_trade_dict(t)
                if new_d:
                    canonical = new_d["order_id"]
                    old_key = None
                    if canonical != _kr[0]:
                        # Migrate from orderId-based key to permId-based key
                        old_key = _kr[0]
                        self._trades.pop(old_key, None)
                        self._trades[canonical] = t
                        _kr[0] = canonical
                    self._emit_order_status(t, canonical, _p,
                                            t.orderStatus.status,
                                            supersedes=old_key)
                else:
                    self._emit_order_status(t, _kr[0], _p, t.orderStatus.status)

            trade.statusEvent += _on_status

        except Exception as exc:
            log.exception("Worker %s: place_order failed: %s", self._label, exc)

    async def _do_cancel_order(self, order_id: str) -> None:
        """Cancel an open order by its *order_id*."""
        trade = self._trades.get(order_id)
        if trade is None:
            log.warning("Worker %s: cancel_order – unknown order_id %s", self._label, order_id)
            return
        try:
            self._ib.cancelOrder(trade.order)
            log.info("Worker %s: cancel requested for order %s", self._label, order_id)
        except Exception as exc:
            log.exception("Worker %s: cancel_order failed: %s", self._label, exc)

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

        d = {
            "order_id": order_id,
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

            # 2. Fallback: bid/ask midpoint
            if math.isnan(price):
                bid = getattr(ticker, "bid", float('nan'))
                ask = getattr(ticker, "ask", float('nan'))
                if (not math.isnan(bid) and not math.isnan(ask)
                        and bid > 0 and ask > 0 and bid < 1e12 and ask < 1e12):
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
        except Exception:
            log.debug("Worker %s: bad price tick: %s", self._label, ticker)

    def _on_pending_tickers(self, tickers) -> None:
        """Called by ib_async when any subscribed tickers have pending updates.

        This is the most reliable streaming hook — fires for live, delayed,
        and frozen data.
        """
        if self._active_ticker is None or not self._active_price_symbol:
            return
        for ticker in tickers:
            if ticker is self._active_ticker:
                self._on_price_update(ticker)
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
            except Exception as exc:
                log.debug("Worker %s: cancel market data error: %s", self._label, exc)
            finally:
                self._active_ticker = None
                self._active_price_symbol = ""

    def _on_account_summary(self, value) -> None:
        """De-duplicate rapid ticks into the buffer dict.

        ``accountSummaryEvent`` emits a single :class:`AccountValue` per call.
        """
        try:
            self._buffer[(value.account, value.tag)] = (value.value, value.currency)
        except Exception:
            log.debug("Worker %s: bad AccountValue: %s", self._label, value)

    def _on_pnl(self, entry) -> None:
        """Handle pnlEvent – provides dailyPnL, realizedPnL, unrealizedPnL.

        ``pnlEvent`` emits a single :class:`PnL` object per call.
        Fields may be ``float('nan')`` when unavailable.
        """
        try:
            acct = entry.account
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
                "realized_pnl": float(getattr(item, "realizedPNL", 0)),
            }
            # Also persist into the long-lived cache for late subscribers
            self._portfolio_cache[(acct, con_id)] = self._portfolio_buffer[(acct, con_id)]
        except Exception:
            log.debug("Worker %s: bad portfolio item: %s", self._label, item)

    def _on_ib_error(self, reqId, errorCode, errorString, contract) -> None:
        """Handle IB error events – detect market-data failures like 10197."""
        if errorCode == 10197 and self._active_ticker is not None:
            # "No market data during competing live session"
            # The paper account can't get data – signal the UI to try another worker.
            sym = self._active_price_symbol
            log.warning("Worker %s: market data blocked for '%s' (error 10197) – "
                        "signalling fallback", self._label, sym)
            if sym:
                self.market_data_failed.emit(sym)

    # ── Global order listener ─────────────────────────────────────────────────

    def _build_trade_dict(self, trade, fill_map: dict | None = None) -> dict | None:
        """Build an order-status dict from an ib_async Trade object.

        Returns ``None`` if the trade has no usable status.
        Shared by ``_on_global_order_status`` and ``_snapshot_existing_orders``.

        Uses ``permId`` as the unique key because ``orderId`` is often 0 for
        completed orders returned by ``reqCompletedOrdersAsync``.  ``permId``
        is globally unique and persists across sessions.

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

            # ── Unique key ────────────────────────────────────────────────
            # Prefer permId (globally unique, survives restarts).
            # Fall back to orderId for orders that haven't received a
            # permId yet (e.g. PendingSubmit).
            perm_id = getattr(order, "permId", 0) or getattr(trade.orderStatus, "permId", 0)
            if perm_id:
                order_id = f"{self._label}_p{perm_id}"
            else:
                order_id = f"{self._label}_{order.orderId}"

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

            # Fallback: start of today so unknown-time orders sort to bottom.
            if et_dt is None:
                et_dt = datetime.now(_ET).replace(hour=0, minute=0, second=0, microsecond=0)
            timestamp = et_dt.strftime("%H:%M:%S")
            time_sort = et_dt.isoformat()

            return {
                "order_id": order_id,
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
            }
        except Exception:
            log.debug("Worker %s: _build_trade_dict error", self._label, exc_info=True)
            return None

    def _on_global_order_status(self, trade) -> None:
        """Global ``orderStatusEvent`` callback – fires for **every** order
        status change on this IB connection, including orders that were placed
        before a disconnect/reconnect cycle and orders placed manually in TWS.

        For orders placed by *this* client (tracked in ``_trades``), the
        per-trade ``_on_status`` closure already emits with the correct
        symbol from the original params — so we skip them here to avoid
        overwriting with an empty symbol from an unqualified Contract.
        """
        d = self._build_trade_dict(trade)
        if not d:
            return

        # If this trade is already tracked by a per-trade handler, skip.
        # The per-trade closure has the correct original params (symbol etc).
        order_id = d["order_id"]
        if order_id in self._trades:
            return

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
            fresh_fills = await self._ib.reqExecutionsAsync(ExecutionFilter())
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

        try:
            # 1. Open / working orders – fresh request to TWS (all clients).
            open_trades = await self._ib.reqAllOpenOrdersAsync()
            log.info("Worker %s: snapshot found %d open trade(s)", self._label, len(open_trades))
            for trade in open_trades:
                d = self._build_trade_dict(trade, fill_map)
                if d and d["order_id"] not in seen_ids:
                    seen_ids.add(d["order_id"])
                    self.order_status_updated.emit(d)
                    emitted += 1
                    # Register in _trades so cancel works
                    self._trades[d["order_id"]] = trade
        except Exception:
            log.exception("Worker %s: snapshot open trades error", self._label)

        try:
            # 2. Completed (filled / cancelled) orders from today's session.
            #    reqCompletedOrdersAsync(apiOnly=False) includes ALL orders
            #    (manual TWS + all API clients).
            completed = await self._ib.reqCompletedOrdersAsync(apiOnly=False)
            log.info("Worker %s: snapshot found %d completed order(s)", self._label, len(completed))
            for trade in completed:
                d = self._build_trade_dict(trade, fill_map)
                if d and d["order_id"] not in seen_ids:
                    seen_ids.add(d["order_id"])
                    self.order_status_updated.emit(d)
                    emitted += 1
        except Exception:
            log.exception("Worker %s: snapshot completed orders error", self._label)

        log.info("Worker %s: order snapshot emitted %d order(s)", self._label, emitted)
