from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter

from src.core import config_manager
from src.core.ib_worker import IBWorkerThread
from .order_entry_panel import OrderEntryPanel
from .order_status_panel import OrderStatusPanel
from .account_list_panel import AccountListPanel
from .account_position_panel import AccountPositionPanel
from .accounts_model import AccountsTableModel, detect_account_type
from .trades_panel import TradesPanel
from .positions_trades_tabs import PositionsTradesTabs

log = logging.getLogger(__name__)

# ── Colours ───────────────────────────────────────────────────────────────────
BG_MAIN = "#161a25"
BG_PANEL = "#1e2230"
TEXT = "#C9D1D9"
TEXT_DIM = "#8B949E"
BORDER = "#30363D"

_C_MAIN = QColor(BG_MAIN)

_TITLE_FONT = QFont("Helvetica Neue, Arial", 11)
_TITLE_FONT.setWeight(QFont.Weight.Bold)

_SPLITTER_SS = f"QSplitter::handle {{ background: {BORDER}; }}"

# Config keys for each splitter
_K_MAIN      = "flash_main_splitter"
_K_RIGHT_H   = "flash_right_h_splitter"   # horizontal: left-content | accounts
_K_LEFT_V    = "flash_left_v_splitter"    # vertical:   orders | positions-trades


def _titled_panel(title: str) -> QWidget:
    """Return an empty dark panel with a top-left section title."""
    panel = QWidget()
    panel.setStyleSheet(
        f"background: {BG_PANEL};"
        f"border: 1px solid {BORDER};"
        f"border-radius: 4px;"
    )
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(10, 8, 10, 8)
    layout.setSpacing(0)

    lbl = QLabel(title)
    lbl.setFont(_TITLE_FONT)
    lbl.setStyleSheet(f"color: {TEXT}; border: none;")
    layout.addWidget(lbl, alignment=Qt.AlignmentFlag.AlignTop |
                     Qt.AlignmentFlag.AlignLeft)
    layout.addStretch(1)
    return panel


class FlashTradeView(QWidget):
    """Skeleton layout for the FlashTrade page — section titles only."""

    def __init__(self, active_threads: dict[str, IBWorkerThread] | None = None,
                 accounts_model: AccountsTableModel | None = None, parent=None,
                 *, open_preferences=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._active_threads: dict[str,
                                   IBWorkerThread] = active_threads if active_threads is not None else {}
        self._accounts_model = accounts_model
        self._open_preferences_callback = open_preferences
        # label → worker instance wired
        self._connected_order_workers: dict[str, IBWorkerThread] = {}
        # label → worker for portfolio
        self._connected_portfolio_workers: dict[str, IBWorkerThread] = {}
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(300)          # debounce 300 ms
        self._save_timer.timeout.connect(self._persist_layout)
        self._setup_ui()
        self._restore_layout()

        # Eagerly wire any workers that may have auto-connected before
        # the Flash tab is shown (snapshot arrives ~1-2 s after connect).
        QTimer.singleShot(1500, self._wire_all_workers)

    def _setup_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(2)

        # ── Left 1/5 — Order Entry ────────────────────────────────────────────
        self._order_entry = OrderEntryPanel(
            active_threads=self._active_threads,
            parent=self,
        )

        # ── Middle 1/5 — Book Trading ─────────────────────────────────────────
        from .book_trader_view import BookTraderView
        self._book_trading = BookTraderView(
            active_threads=self._active_threads,
            parent=self,
        )

        # ── Right 3/5 — Account List full-height on right; left side split top/bottom
        self._right_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._right_splitter.setHandleWidth(2)
        self._right_splitter.setStyleSheet(_SPLITTER_SS)

        # Left column: Order Status (top) | Positions+Trades (bottom)
        self._top_right_splitter = QSplitter(Qt.Orientation.Vertical)
        self._top_right_splitter.setHandleWidth(2)
        self._top_right_splitter.setStyleSheet(_SPLITTER_SS)
        self._order_status = OrderStatusPanel(self)

        self._account_position = AccountPositionPanel(
            accounts_model=self._accounts_model,
            parent=self,
            open_preferences=self._open_preferences_callback,
        )
        self._trades_panel = TradesPanel(
            self, open_preferences=self._open_preferences_callback,
        )
        self._positions_trades = PositionsTradesTabs(
            positions_widget=self._account_position,
            trades_widget=self._trades_panel,
            parent=self,
        )

        self._top_right_splitter.addWidget(self._order_status)
        self._top_right_splitter.addWidget(self._positions_trades)
        self._top_right_splitter.setStretchFactor(0, 1)
        self._top_right_splitter.setStretchFactor(1, 1)

        # Right column: Account List spans full height
        if self._accounts_model is not None:
            self._account_list = AccountListPanel(self._accounts_model, self)
        else:
            self._account_list = _titled_panel("Account List")

        self._right_splitter.addWidget(self._top_right_splitter)
        self._right_splitter.addWidget(self._account_list)
        self._right_splitter.setStretchFactor(0, 3)
        self._right_splitter.setStretchFactor(1, 1)

        # ── Assemble into main horizontal splitter ────────────────────────────
        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setHandleWidth(2)
        self._main_splitter.setStyleSheet(_SPLITTER_SS)

        self._main_splitter.addWidget(self._order_entry)
        self._main_splitter.addWidget(self._book_trading)
        self._main_splitter.addWidget(self._right_splitter)

        # 1 : 1 : 3  ratio
        self._main_splitter.setStretchFactor(0, 1)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setStretchFactor(2, 3)

        root.addWidget(self._main_splitter)

        # ── Wire splitter moved → debounced save ─────────────────────────────
        self._main_splitter.splitterMoved.connect(self._on_splitter_moved)
        self._right_splitter.splitterMoved.connect(self._on_splitter_moved)
        self._top_right_splitter.splitterMoved.connect(self._on_splitter_moved)

        # ── Wire order entry → order placement ───────────────────────────────
        self._order_entry.order_requested.connect(self._on_order_requested)
        self._order_entry.cancel_all_requested.connect(
            self._on_cancel_all_for_symbol)
        self._order_entry.flatten_requested.connect(self._on_flatten_requested)
        self._order_entry.reverse_requested.connect(self._on_reverse_requested)
        self._order_entry.close_pct_requested.connect(
            self._on_close_pct_requested)
        self._order_entry.close_tier_requested.connect(
            self._on_close_tier_requested)

        # ── Wire book trader (mirrors order-entry signal surface) ────────────
        # BookTraderView submits through the same handlers so TP/SL/Trail
        # children, multi-account fan-out, and close-tier routing all
        # behave identically whether the click came from OrderEntry or
        # a ladder row.
        self._book_trading.order_requested.connect(self._on_order_requested)
        self._book_trading.cancel_all_requested.connect(
            self._on_cancel_all_for_symbol)
        self._book_trading.cancel_order_requested.connect(
            self._on_cancel_requested)
        self._book_trading.flatten_requested.connect(self._on_flatten_requested)
        self._book_trading.reverse_requested.connect(self._on_reverse_requested)
        self._book_trading.close_tier_requested.connect(
            self._on_close_tier_requested)

        # ── Wire order status cancel button → cancel logic ───────────────────
        self._order_status.cancel_requested.connect(self._on_cancel_requested)
        self._order_status.refresh_requested.connect(
            self._on_refresh_requested)

        # ── Wire account position refresh button → portfolio refresh ─────────
        self._account_position.refresh_requested.connect(
            self._on_position_refresh_requested)

        # ── Wire account list → positions filter + order-target banner ───────
        if isinstance(self._account_list, AccountListPanel):
            self._account_list.selection_changed.connect(
                self._account_position.set_selected_accounts)
            self._account_list.selection_changed.connect(
                self._refresh_order_target)
            self._account_list.active_group_changed.connect(
                self._refresh_order_target)
            # Push the initial persisted selection so the view is correct
            # before the user clicks anything.
            self._account_position.set_selected_accounts(
                self._account_list.get_selected_accounts())
            self._refresh_order_target()

        # ── Wire position panel action buttons ───────────────────────────────
        # Header-row Flatten: flatten every open position for that account
        self._account_position.flatten_requested.connect(
            self._on_position_account_flatten)
        # Position-row buttons: Flatten (100%) · 25% · 50% · 75%
        self._account_position.position_close_requested.connect(
            self._on_position_close_requested)

        # ── Wire account-list action buttons ─────────────────────────────────
        # 25% · 50% · 75% · Flatten · Reverse · Cancel
        if isinstance(self._account_list, AccountListPanel):
            self._account_list.action_requested.connect(
                self._on_account_list_action)

    @property
    def trades_panel(self) -> TradesPanel:
        """Host-side accessor so MainWindow can use it as a ColumnsProvider."""
        return self._trades_panel

    @property
    def account_position(self) -> AccountPositionPanel:
        """Host-side accessor so MainWindow can include it in the combined ColumnsProvider."""
        return self._account_position

    # ── Order placement ───────────────────────────────────────────────────────

    def _wire_all_workers(self) -> None:
        """Ensure every active worker's order_status_updated signal is wired."""
        for label, worker in self._active_threads.items():
            if worker.isRunning():
                self._ensure_worker_connected(worker)
        # Ensure background price subscriptions are active
        self._order_entry.refresh_price_subscription()
        # Book Trader needs the same cold-start retry — it's built before
        # workers connect and would otherwise never wire its signals or
        # subscribe depth.
        if hasattr(self._book_trading, "refresh_subscriptions"):
            self._book_trading.refresh_subscriptions()

    def _on_order_requested(self, payload: dict) -> None:
        """Handle Buy/Sell button click from OrderEntryPanel.

        1. Collect checked accounts from AccountListPanel.
        2. Look up saved contract details for the symbol.
        3. For each account, find the correct IBWorkerThread and place the order.
        """
        # ── 1. Validate: get checked accounts ─────────────────────────────
        # When called from flatten, the payload carries ``_flatten_account``
        # so we target only that specific account (not all checked ones).
        flatten_account = payload.get("_flatten_account")
        if flatten_account:
            accounts = [flatten_account]
        else:
            if not isinstance(self._account_list, AccountListPanel):
                log.warning(
                    "FlashTrade: AccountListPanel not available – aborting order")
                return

            accounts = self._account_list.get_selected_accounts()
            if not accounts:
                log.warning("FlashTrade: no accounts checked – aborting order")
                return

        # ── 2. Look up contract from saved config ─────────────────────────
        symbol = payload.get("symbol", "")
        if not symbol:
            log.warning("FlashTrade: no symbol in order payload – aborting")
            return

        contracts = config_manager.load_contracts()
        contract = None
        q = symbol.upper()
        for c in contracts:
            if (c.get("symbol", "").upper() == q
                    or c.get("local_symbol", "").upper() == q):
                contract = c
                break

        if not contract:
            log.warning(
                "FlashTrade: no saved contract for '%s' – aborting", symbol)
            return

        conid = contract.get("conid")
        if not conid:
            log.warning(
                "FlashTrade: saved contract for '%s' has no conid – aborting", symbol)
            return

        action = payload.get("action", "BUY")
        qty = payload.get("qty", 1)
        order_type = payload.get("type", "MKT")
        last_price = payload.get("last_price", 0.0)
        sec_type = contract.get("sec_type", "")

        # ── Resolve preferences and tick size up-front ────────────────────
        from src.utils.helpers import (
            calculate_capped_price, calculate_offset, tick_size_for_contract,
        )
        from src.core.trading_prefs import trading_prefs

        tick = tick_size_for_contract(
            sec_type,
            symbol=contract.get("symbol", ""),
            local_symbol=contract.get("local_symbol", ""),
        )
        prefs = trading_prefs.get(sec_type)

        # Helper: compute the limit-cap price (returns 0 when no base price)
        def _cap_price() -> float:
            if last_price <= 0:
                return 0.0
            return calculate_capped_price(
                symbol_type=sec_type, side=action,
                base_price=last_price, tick_size=tick,
                offset_key="limit_cap",
            )

        # ── Route by order type ───────────────────────────────────────────
        limit_price = None
        adaptive_priority = None
        peg_offset = None

        if order_type == "MID":
            # Buy/Sell @Mid → IB Adaptive Algo + limit cap
            limit_price = _cap_price()
            adaptive_priority = prefs.get("mid_price_behavior", "Normal")
            log.info("FlashTrade: %s @Mid %s base=%.4f tick=%.4f → "
                     "ADAPTIVE limit=%.4f priority=%s",
                     action, symbol, last_price, tick,
                     limit_price, adaptive_priority)
            order_type = "ADAPTIVE"

        elif order_type in ("BID", "ASK"):
            if action == "BUY":
                behavior = prefs.get("buy_bid_behavior")
            else:
                behavior = prefs.get("sell_ask_behavior")

            if behavior is not None:
                # Futures → Adaptive Algo (CME has no native peg)
                limit_price = _cap_price()
                adaptive_priority = behavior
                log.info("FlashTrade: FUT %s @%s %s base=%.4f tick=%.4f → "
                         "ADAPTIVE limit=%.4f priority=%s",
                         action, order_type, symbol, last_price, tick,
                         limit_price, adaptive_priority)
                order_type = "ADAPTIVE"
            else:
                # Stocks → Pegged to Primary (REL)
                # auxPrice = dollar offset from the NBBO
                if action == "BUY":
                    raw_offset = float(prefs.get("buy_bid_offset", 0))
                    offset_unit = str(prefs.get("buy_bid_unit", "ticks"))
                else:
                    raw_offset = float(prefs.get("sell_ask_offset", 0))
                    offset_unit = str(prefs.get("sell_ask_unit", "ticks"))
                peg_offset = abs(calculate_offset(
                    raw_offset, offset_unit,
                    last_price if last_price > 0 else 0, tick,
                ))
                # lmtPrice = limit cap (ceiling BUY / floor SELL), 0 = no cap
                limit_price = _cap_price()
                log.info("FlashTrade: STK %s @%s %s base=%.4f tick=%.4f → "
                         "PEG_PRIM limit=%.4f peg_offset=%.4f",
                         action, order_type, symbol, last_price, tick,
                         limit_price, peg_offset)
                order_type = "PEG_PRIM"

        elif order_type in ("BID_LMT", "ASK_LMT"):
            # Buy @Bid Limit / Sell @Ask Limit — plain LimitOrder at the
            # exact bid or ask price provided by the order entry panel.
            limit_price = float(payload.get("limit_price", 0))
            if limit_price <= 0:
                log.warning("FlashTrade: %s %s but limit_price=%.4f – aborting",
                            action, order_type, limit_price)
                return
            log.info("FlashTrade: %s %s %s → LMT @ %.4f",
                     action, order_type, symbol, limit_price)
            order_type = "LMT"

        elif order_type == "MKT" and last_price > 0:
            # MKT → cap with a limit if Limit Cap Offset is set
            cap_value = float(prefs.get("limit_cap", 0))
            if cap_value != 0:
                limit_price = _cap_price()
                log.info("FlashTrade: MKT capped → LMT  %s %s base=%.4f "
                         "cap=%s %s tick=%.4f → limit=%.4f",
                         action, symbol, last_price,
                         cap_value, prefs.get("limit_cap_unit", "ticks"),
                         tick, limit_price)
                order_type = "LMT"

        log.info("FlashTrade: %s %s %d %s → %d account(s): %s",
                 action, order_type, qty, symbol, len(accounts), accounts)

        # ── 3. Route to correct worker for each account ───────────────────
        for account in accounts:
            worker = self._find_worker_for_account(account)
            if not worker:
                log.warning(
                    "FlashTrade: no connected worker for account %s – skipping", account)
                continue

            # Ensure order_status_updated signal is wired for this worker
            self._ensure_worker_connected(worker)

            params = {
                "conid": int(conid),
                "symbol": symbol,
                "sec_type": contract.get("sec_type", ""),
                "exchange": contract.get("exchange", "SMART"),
                "currency": contract.get("currency", "USD"),
                "local_symbol": contract.get("local_symbol", ""),
                "expiry": contract.get("expiry", ""),
                "multiplier": contract.get("multiplier", ""),
                "account": account,
                "action": action,
                "qty": qty,
                "order_type": order_type,
                "tif": "DAY"
            }
            if limit_price is not None:
                params["limit_price"] = limit_price
            if adaptive_priority is not None:
                params["adaptive_priority"] = adaptive_priority
            if peg_offset is not None:
                params["peg_offset"] = peg_offset
            # Forward bracket-order tiers for attached TP / SL / Trail children.
            # `bracket_ref_price` is the expected-fill anchor point for child
            # prices — critical because `limit_price` on Adaptive / PEG /
            # cap-wrapped-MKT orders is a *ceiling* (BUY) or *floor* (SELL),
            # NOT the intended fill.  Children priced off the cap land far
            # from the real fill.  Only BID_LMT / ASK_LMT orders truly fill
            # at their limit, so that's the one case where we anchor to it.
            params["tick_size"] = tick
            params["last_price"] = last_price
            original_type = payload.get("type", "MKT")
            if original_type in ("BID_LMT", "ASK_LMT") and limit_price:
                params["bracket_ref_price"] = limit_price
            else:
                params["bracket_ref_price"] = last_price
            # Convert the user's Limit Cap preference into a points offset.
            # Reused for stop-loss children so they submit as STP LMT (trigger
            # vs. limit offset by this amount) instead of plain STP — gives
            # the resulting limit order a small execution window while still
            # guaranteeing the stop fires at the user's intended SL price.
            cap_raw = float(prefs.get("limit_cap", 0) or 0)
            if cap_raw:
                params["limit_cap_offset"] = abs(calculate_offset(
                    cap_raw, str(prefs.get("limit_cap_unit", "ticks")),
                    last_price if last_price > 0 else 0, tick,
                ))
            else:
                params["limit_cap_offset"] = 0.0
            params["profit_take"] = payload.get("profit_take")
            params["stop_loss"] = payload.get("stop_loss")
            params["trailing"] = payload.get("trailing")
            worker.place_order(params)

    def _find_worker_for_account(self, account: str) -> IBWorkerThread | None:
        """Look up the correct worker thread for a given account ID."""
        if self._accounts_model is None:
            return None

        label = self._accounts_model.connection_for_account(account)
        if label is None:
            return None

        worker = self._active_threads.get(label)
        if worker and worker.isRunning() and worker.is_ib_connected:
            return worker
        return None

    def _ensure_worker_connected(self, worker: IBWorkerThread) -> None:
        """Connect order_status_updated, portfolio_updated, and
        flatten_close_needed signals if not already connected.

        If the worker for this label has been replaced (disconnect/reconnect
        cycle), disconnect from the old instance and wire the new one.
        When wiring a new worker, request a re-snapshot so orders that were
        emitted before this signal was connected are not lost.
        """
        label = worker._label
        prev = self._connected_order_workers.get(label)
        if prev is worker:
            return  # already wired to this exact instance

        # Disconnect stale worker if any
        if prev is not None:
            try:
                prev.order_status_updated.disconnect(
                    self._on_order_status_updated)
            except RuntimeError:
                pass
            try:
                prev.portfolio_updated.disconnect(self._on_portfolio_updated)
            except RuntimeError:
                pass
            try:
                prev.flatten_close_needed.disconnect(
                    self._on_flatten_close_needed)
            except RuntimeError:
                pass
            try:
                prev.reverse_close_needed.disconnect(
                    self._on_reverse_close_needed)
            except RuntimeError:
                pass
            try:
                prev.pnl_single_updated.disconnect(self._on_pnl_single_updated)
            except RuntimeError:
                pass
            try:
                prev.account_pnl_updated.disconnect(self._on_account_pnl_updated)
            except RuntimeError:
                pass
            try:
                prev.fill_updated.disconnect(self._on_fill_updated)
            except RuntimeError:
                pass
            log.debug("FlashTrade: unwired stale signals for worker %s", label)

        worker.order_status_updated.connect(self._on_order_status_updated)
        worker.portfolio_updated.connect(self._on_portfolio_updated)
        worker.flatten_close_needed.connect(self._on_flatten_close_needed)
        worker.reverse_close_needed.connect(self._on_reverse_close_needed)
        worker.pnl_single_updated.connect(self._on_pnl_single_updated)
        worker.account_pnl_updated.connect(self._on_account_pnl_updated)
        worker.fill_updated.connect(self._on_fill_updated)
        self._connected_order_workers[label] = worker
        self._connected_portfolio_workers[label] = worker
        log.debug(
            "FlashTrade: wired order_status_updated + portfolio_updated + "
            "pnl_single_updated + account_pnl_updated + fill_updated "
            "for worker %s", label)

        # Re-snapshot: the worker's initial snapshot may have fired before
        # this signal was connected.  Ask it to emit all known orders and
        # fills again so the Trades view is populated.
        if worker.is_ib_connected:
            worker.request_order_snapshot()
            worker.request_portfolio_snapshot()
            worker.request_pnl_snapshot()
            worker.request_fills_snapshot()

    def _on_order_status_updated(self, data: dict) -> None:
        """Receive order status from worker thread → update OrderStatusPanel."""
        order_id = data.get("order_id", "")
        account = data.get("account", "")
        status = data.get("status", "")

        # Remove stale row when key migrates from orderId → permId
        supersedes = data.get("supersedes")
        if supersedes:
            self._order_status.remove_order(supersedes)

        # Display alias instead of raw account ID
        display_account = account
        if self._accounts_model and account:
            alias = self._accounts_model._aliases.get(account, "")
            if alias:
                display_account = alias

        self._order_status.update_order(order_id, {
            "order_ref": data.get("order_ref", ""),
            "symbol": data.get("symbol", "–"),
            "account": display_account,
            "action": data.get("action", "–"),
            "order_type": data.get("order_type", "–"),
            "quantity": data.get("quantity", "–"),
            "limit_price": data.get("limit_price", "–"),
            "stop_price": data.get("stop_price", "–"),
            "tif": data.get("tif", "–"),
            "fill_price": data.get("fill_price", "–"),
            "status": status,
            "time": data.get("time", "–"),
            "time_sort": data.get("time_sort", ""),
            "error_message": data.get("error_message", ""),
        })


    def _on_portfolio_updated(self, batch: list[dict]) -> None:
        """Receive portfolio position batch from worker thread → update panel."""
        log.debug("FlashTrade: portfolio batch received (%d positions)", len(batch))
        self._account_position.update_positions(batch)

        # Feed account balance info from accounts_model if available.
        # RC #2: use O(1) reverse-index lookup (_acct_to_label + _row_map)
        # instead of the previous O(n×m) linear scan over every account row
        # for every position in the batch.
        if self._accounts_model is not None:
            seen_accounts: set[str] = set()
            from .accounts_model import COL_NET_LIQ, COL_EXCESS_LIQ
            for pos in batch:
                account = pos.get("account", "")
                if not account or account in seen_accounts:
                    continue
                seen_accounts.add(account)
                # O(1): resolve label → row index directly
                label = self._accounts_model.connection_for_account(account)
                if label is None:
                    continue
                row_idx = self._accounts_model._row_map.get((label, account))
                if row_idx is None:
                    continue
                row_data = self._accounts_model._table_data[row_idx]
                try:
                    bal = float(row_data[COL_NET_LIQ]) if row_data[COL_NET_LIQ] else 0
                except (ValueError, TypeError):
                    bal = 0
                try:
                    excess = float(row_data[COL_EXCESS_LIQ]) if row_data[COL_EXCESS_LIQ] else 0
                except (ValueError, TypeError):
                    excess = 0
                bp = self._accounts_model.get_buying_power(account)
                self._account_position.set_account_data(
                    account, balance=bal,
                    net_liq=bal, excess_liq=excess,
                    buying_power=bp)

    def _on_pnl_single_updated(self, account: str, con_id: int,
                               daily_pnl: float, realized_pnl: float,
                               unrealized_pnl: float) -> None:
        """Per-position P&L from pnlSingleEvent → update position panel."""
        self._account_position.update_pnl_single(
            account, con_id, daily_pnl, realized_pnl, unrealized_pnl)

    def _on_account_pnl_updated(self, account: str, daily_pnl: float,
                                realized_pnl: float,
                                unrealized_pnl: float) -> None:
        """Account-level P&L from pnlEvent → update account header row."""
        self._account_position.set_account_data(
            account, daily_pnl=daily_pnl,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl)

    def _on_fill_updated(self, fill: dict) -> None:
        """Execution arrived (startup snapshot or live execDetailsEvent) →
        insert/refresh a row in the Trades view.  Dedupe by execId is
        handled in the model."""
        display_account = fill.get("account", "")
        if self._accounts_model and display_account:
            alias = self._accounts_model._aliases.get(display_account, "")
            if alias:
                fill = dict(fill)
                fill["account"] = alias
        self._trades_panel.add_fill(fill)

    def _refresh_portfolio(self) -> None:
        """Ask every connected worker to re-push portfolio positions."""
        for label, worker in self._active_threads.items():
            if worker.isRunning() and worker.is_ib_connected:
                worker.request_portfolio_refresh()
        log.debug("FlashTrade: portfolio refresh requested for all workers")

    def _on_cancel_requested(self, order_id: str) -> None:
        """Cancel button clicked in OrderStatusPanel → route to correct worker.

        order_id format: ``{account}_{orderRef}``
        The cancel is only forwarded when the order's account has its checkbox
        checked in the AccountListPanel.  If the AccountListPanel is not
        available the cancel is blocked entirely (fail-safe).
        """
        parts = order_id.split("_", 1)
        if len(parts) < 2:
            log.warning(
                "FlashTrade: malformed order_id for cancel: %s", order_id)
            return

        account = parts[0]

        # Always require the account checkbox to be checked.
        # get_selected_accounts() returns only accounts where is_checked=True,
        # so an empty set means nothing is checked — cancel is blocked.
        if not isinstance(self._account_list, AccountListPanel):
            log.warning("FlashTrade: cancel blocked for order %s — "
                        "AccountListPanel not available", order_id)
            return

        checked = set(self._account_list.get_selected_accounts())
        if account not in checked:
            log.warning(
                "FlashTrade: cancel blocked for order %s — account %s "
                "is not checked (checked: %s)",
                order_id, account, sorted(checked) or "none",
            )
            return

        label = self._accounts_model.connection_for_account(account)
        if not label:
            log.warning("FlashTrade: no connection found for account %s (order %s)",
                        account, order_id)
            return

        worker = self._active_threads.get(label)
        if worker and worker.isRunning() and worker.is_ib_connected:
            worker.cancel_order(order_id)
            log.info(
                "FlashTrade: cancel forwarded to worker %s for order %s", label, order_id)
        else:
            log.warning(
                "FlashTrade: worker %s not available for cancel of %s", label, order_id)

    def _on_cancel_all_for_symbol(self, symbol: str) -> None:
        """Cancel button clicked in OrderEntryPanel → cancel all open orders
        for *symbol* on the checked accounts across every connected worker.

        Only accounts with their checkbox checked in the AccountListPanel are
        affected.  If no accounts are checked nothing is cancelled.
        """
        if not isinstance(self._account_list, AccountListPanel):
            log.warning(
                "FlashTrade: cancel-all blocked — AccountListPanel not available")
            return

        checked: set[str] = set(self._account_list.get_selected_accounts())
        if not checked:
            log.warning(
                "FlashTrade: cancel-all blocked for '%s' — no accounts checked", symbol)
            return

        count = 0
        for label, worker in self._active_threads.items():
            if worker.isRunning() and worker.is_ib_connected:
                self._ensure_worker_connected(worker)
                worker.cancel_all_for_symbol(symbol, checked)
                count += 1
        log.info("FlashTrade: cancel_all_for_symbol '%s' → %d worker(s) "
                 "accounts=%s", symbol, count, sorted(checked))

    def _on_flatten_requested(self, symbol: str) -> None:
        """Flatten button clicked → cancel open orders then close position
        for *symbol* on every checked account across all connected workers.

        Each checked account is flattened individually so the worker can
        read the per-account position and send the correct offsetting order.
        """
        if not isinstance(self._account_list, AccountListPanel):
            log.warning(
                "FlashTrade: flatten blocked — AccountListPanel not available")
            return

        checked: set[str] = set(self._account_list.get_selected_accounts())
        if not checked:
            log.warning(
                "FlashTrade: flatten blocked for '%s' — no accounts checked", symbol)
            return

        count = 0
        for label, worker in self._active_threads.items():
            if worker.isRunning() and worker.is_ib_connected:
                self._ensure_worker_connected(worker)
                for acct in checked:
                    worker.flatten(symbol, acct)
                count += 1
        log.info("FlashTrade: flatten '%s' → %d worker(s) accounts=%s",
                 symbol, count, sorted(checked))

    def _on_flatten_close_needed(self, data: dict) -> None:
        """Worker read a non-zero position during flatten → place the closing
        order through the normal order-placement path so that limit-price cap,
        RTH routing, and trading preferences are all applied (same as
        Buy/Sell @MKT button).
        """
        symbol = data.get("symbol", "")
        action = data.get("action", "BUY")
        qty = data.get("qty", 0)
        account = data.get("account", "")

        if not symbol or qty <= 0 or not account:
            log.warning(
                "FlashTrade: flatten_close_needed invalid data: %s", data)
            return

        # Read current last price from the order entry cache so the limit
        # cap calculation has a base price (same source as the MKT button).
        last_price = 0.0
        cached = self._order_entry._price_cache.get(symbol.upper())
        if cached:
            last_price = cached[0]  # (last, bid, ask)

        # Build the same payload that _emit_order("BUY"/"SELL", "MKT")
        # would produce, then feed it through _on_order_requested which
        # applies limit cap, RTH, prefs, etc.
        payload = {
            "action": action,
            "type": "MKT",
            "symbol": symbol,
            "qty": qty,
            "last_price": last_price,
            # Force to a single account — override normal "all checked" logic
            "_flatten_account": account,
        }
        log.info("FlashTrade: flatten close → routing %s %d %s @MKT "
                 "account=%s last=%.4f through normal order path",
                 action, qty, symbol, account, last_price)
        self._on_order_requested(payload)

    def _on_reverse_requested(self, symbol: str) -> None:
        """Reverse button clicked → cancel open orders then flip position
        for *symbol* on every checked account across all connected workers.
        """
        if not isinstance(self._account_list, AccountListPanel):
            log.warning(
                "FlashTrade: reverse blocked — AccountListPanel not available")
            return

        checked: set[str] = set(self._account_list.get_selected_accounts())
        if not checked:
            log.warning(
                "FlashTrade: reverse blocked for '%s' — no accounts checked", symbol)
            return

        count = 0
        for label, worker in self._active_threads.items():
            if worker.isRunning() and worker.is_ib_connected:
                self._ensure_worker_connected(worker)
                for acct in checked:
                    worker.reverse(symbol, acct)
                count += 1
        log.info("FlashTrade: reverse '%s' → %d worker(s) accounts=%s",
                 symbol, count, sorted(checked))

    def _on_reverse_close_needed(self, data: dict) -> None:
        """Worker read a non-zero position during reverse → place the
        reversing order through the normal order-placement path so that
        limit-price cap, RTH routing, and trading preferences are all
        applied (same as Buy/Sell @MKT button).
        """
        symbol = data.get("symbol", "")
        action = data.get("action", "BUY")
        qty = data.get("qty", 0)
        account = data.get("account", "")

        if not symbol or qty <= 0 or not account:
            log.warning(
                "FlashTrade: reverse_close_needed invalid data: %s", data)
            return

        # Read current last price from the order entry cache so the limit
        # cap calculation has a base price (same source as the MKT button).
        last_price = 0.0
        cached = self._order_entry._price_cache.get(symbol.upper())
        if cached:
            last_price = cached[0]  # (last, bid, ask)

        payload = {
            "action": action,
            "type": "MKT",
            "symbol": symbol,
            "qty": qty,
            "last_price": last_price,
            # Force to a single account — override normal "all checked" logic
            "_flatten_account": account,
        }
        log.info("FlashTrade: reverse close → routing %s %d %s @MKT "
                 "account=%s last=%.4f through normal order path",
                 action, qty, symbol, account, last_price)
        self._on_order_requested(payload)

    def _on_close_pct_requested(self, symbol: str, pct: int) -> None:
        """Partial-close button (25%/50%/75%) clicked → close *pct* percent
        of the position for *symbol* on every checked account.

        No open orders are cancelled — only a partial offsetting MKT order
        is sent.  The worker reads the position, computes the quantity, and
        emits ``flatten_close_needed`` which routes through the normal
        order-placement path (limit cap, RTH, prefs, etc.).
        """
        if not isinstance(self._account_list, AccountListPanel):
            log.warning(
                "FlashTrade: close_%d%% blocked — AccountListPanel not available", pct)
            return

        checked: set[str] = set(self._account_list.get_selected_accounts())
        if not checked:
            log.warning(
                "FlashTrade: close_%d%% blocked for '%s' — no accounts checked", pct, symbol)
            return

        count = 0
        for label, worker in self._active_threads.items():
            if worker.isRunning() and worker.is_ib_connected:
                self._ensure_worker_connected(worker)
                for acct in checked:
                    worker.close_pct(symbol, acct, pct)
                count += 1
        log.info("FlashTrade: close_%d%% '%s' → %d worker(s) accounts=%s",
                 pct, symbol, count, sorted(checked))

    def _on_close_tier_requested(self, data: dict) -> None:
        """Take Profit / Stop Loss header button clicked → submit an exit
        order sized to the current position on every checked account.

        * TP → LMT at last_price ± offset_pts (profit direction).
        * SL → STP or STP LMT (if limit_cap set) at last_price ∓ offset_pts.
        * qty = min(|position|, payload.qty).  Zero-position accounts are
          silently skipped so we never submit an empty / phantom order.
        """
        symbol = data.get("symbol", "")
        side = data.get("side", "")
        if not symbol or side not in ("tp", "sl"):
            log.warning("FlashTrade: close_tier invalid payload: %s", data)
            return
        if not isinstance(self._account_list, AccountListPanel):
            log.warning(
                "FlashTrade: close_tier blocked — AccountListPanel not available")
            return

        checked: set[str] = set(self._account_list.get_selected_accounts())
        if not checked:
            log.warning(
                "FlashTrade: close_tier %s blocked for '%s' — "
                "no accounts checked", side, symbol)
            return

        # Use the UI's cached last_price (same source the order-entry panel
        # shows); fall back to the payload's snapshot if the cache is cold.
        last_price = 0.0
        cached = self._order_entry._price_cache.get(symbol.upper())
        if cached:
            last_price = float(cached[0])
        if last_price <= 0:
            last_price = float(data.get("last_price") or 0.0)
        if last_price <= 0:
            log.warning(
                "FlashTrade: close_tier %s '%s' blocked — no live price yet",
                side, symbol)
            return

        # Limit cap offset (points) reused to build STP LMT children for SL
        # where the limit sits cap_offset further into the loss zone from
        # the stop trigger.  TP uses a plain LMT so this is ignored.
        from src.utils.helpers import calculate_offset, tick_size_for_contract
        from src.core.trading_prefs import trading_prefs
        from src.core.reference_data import get_instance as _rd

        contracts = _rd().contracts_by_symbol.get(symbol.upper(), [])
        sec_type = contracts[0].get("sec_type", "") if contracts else ""
        tick = tick_size_for_contract(
            sec_type,
            symbol=contracts[0].get("symbol", "") if contracts else "",
            local_symbol=contracts[0].get("local_symbol", "") if contracts else "",
        ) if contracts else 0.25
        prefs = trading_prefs.get(sec_type)
        cap_raw = float(prefs.get("limit_cap", 0) or 0) if prefs else 0.0
        cap_offset = abs(calculate_offset(
            cap_raw, str(prefs.get("limit_cap_unit", "ticks")),
            last_price, tick,
        )) if cap_raw else 0.0

        offset_pts = float(data.get("offset_pts") or 0.0)
        req_qty = int(data.get("qty") or 0)

        count = 0
        for label, worker in self._active_threads.items():
            if worker.isRunning() and worker.is_ib_connected:
                self._ensure_worker_connected(worker)
                for acct in checked:
                    worker.close_tier(
                        symbol=symbol,
                        account=acct,
                        side=side,
                        offset_pts=offset_pts,
                        requested_qty=req_qty,
                        current_price=last_price,
                        cap_offset=cap_offset,
                    )
                count += 1
        log.info(
            "FlashTrade: close_tier %s '%s' offset=%.4f qty=%d "
            "ref=%.4f cap=%.4f → %d worker(s) accounts=%s",
            side, symbol, offset_pts, req_qty, last_price, cap_offset,
            count, sorted(checked))

    def _on_refresh_requested(self) -> None:
        """Refresh button clicked → ask every connected worker to re-snapshot."""
        count = 0
        for label, worker in self._active_threads.items():
            if worker.isRunning() and worker.is_ib_connected:
                worker.request_order_snapshot()
                worker.request_portfolio_refresh()
                count += 1
        log.info("FlashTrade: refresh triggered for %d worker(s)", count)

    def _refresh_order_target(self, *_args) -> None:
        """Push the current target (group + account set + live/paper
        badge) into the OrderEntryPanel banner.  Wired to both
        ``selection_changed`` and ``active_group_changed``; accepts
        ``*_args`` because the two signals emit with different
        payloads but this method re-reads state from the panel."""
        if not isinstance(self._account_list, AccountListPanel):
            return
        accounts = set(self._account_list.get_selected_accounts())
        group = self._account_list.active_group()

        # Classify live/paper so the banner's background communicates
        # risk at a glance.  Unknown-prefix accounts count as neither;
        # if every non-unknown is Live → LIVE, all Paper → PAPER, a
        # mix of both → MIXED.
        has_live = has_paper = False
        for acct in accounts:
            t = detect_account_type(acct)
            if t == "Live":
                has_live = True
            elif t == "Paper":
                has_paper = True
        if not accounts:
            badge = "NONE"
        elif has_live and has_paper:
            badge = "MIXED"
        elif has_live:
            badge = "LIVE"
        elif has_paper:
            badge = "PAPER"
        else:
            # All unknown prefixes — treat as LIVE for safety (better
            # to over-warn than under-warn).
            badge = "LIVE"

        count = len(accounts)
        if count == 1:
            acct = next(iter(accounts))
            alias = self._accounts_model._aliases.get(acct, "") \
                if self._accounts_model is not None else ""
            label = alias if alias else acct
        else:
            label = f"{count} accounts"

        self._order_entry.set_order_target(group, label, badge, count)

    def _on_position_refresh_requested(self) -> None:
        """Refresh button clicked in AccountPositionPanel.

        1. Re-emit the cached positions immediately (instant UI update).
        2. Ask IB to re-push account updates so any new changes arrive.
        """
        for label, worker in self._active_threads.items():
            if worker.isRunning() and worker.is_ib_connected:
                worker.request_portfolio_snapshot()
                worker.request_portfolio_refresh()
                log.info(
                    "FlashTrade: portfolio snapshot + refresh for worker %s", label)

    def _on_position_account_flatten(self, account: str) -> None:
        """Header-row Flatten clicked → cancel ALL open orders for *account*,
        then close every open position.

        Step 1 — ``cancel_all_for_account``: sweeps every open order for the
        account regardless of symbol, including resting limit orders that have
        no matching position yet (which per-symbol flatten would miss).

        Step 2 — ``worker.flatten(symbol, account)`` per position: reads the
        live position size and emits ``flatten_close_needed`` so the offsetting
        order routes through the normal limit-cap / RTH / prefs path.
        Note: ``_do_flatten`` also runs a per-symbol cancel internally, but
        since Step 1 already cancelled everything, the redundant cancel is a
        no-op and does no harm.
        """
        worker = self._find_worker_for_account(account)
        if not worker:
            log.warning(
                "FlashTrade: account flatten — no connected worker for %s", account)
            return

        # Collect every non-zero position for this account
        pos_keys = [
            (acct, con_id)
            for acct, con_id in self._account_position.get_position_keys()
            if acct == account
        ]
        if not pos_keys:
            log.warning(
                "FlashTrade: account flatten %s — no open positions found", account)
            return

        self._ensure_worker_connected(worker)

        # ── Step 1: cancel ALL open orders for this account (no symbol filter)
        worker.cancel_all_for_account(account)
        log.info("FlashTrade: cancel_all_for_account sent for %s", account)

        # ── Step 2: close each non-zero position via the normal flatten path
        for _, con_id in pos_keys:
            symbol = self._account_position.get_symbol_for_position(account, con_id)
            if symbol:
                worker.flatten(symbol, account)
                log.info(
                    "FlashTrade: position flatten %s for account %s", symbol, account)

        log.info(
            "FlashTrade: account flatten complete — %d position(s) in %s",
            len(pos_keys), account)

    def _on_position_close_requested(self, account: str,
                                     con_id: int, pct: int) -> None:
        """Position-row button clicked.

        ``pct == 100``  → Flatten: cancel open orders then close full position.
        ``pct < 100``   → Partial close: close *pct*% of the position without
                          cancelling any open orders.

        Both paths route through the normal limit-cap / RTH / prefs pipeline
        via ``flatten_close_needed``.
        """
        symbol = self._account_position.get_symbol_for_position(account, con_id)
        if not symbol:
            log.warning(
                "FlashTrade: position_close — no symbol for account=%s conId=%d",
                account, con_id)
            return

        worker = self._find_worker_for_account(account)
        if not worker:
            log.warning(
                "FlashTrade: position_close — no connected worker for account %s",
                account)
            return

        self._ensure_worker_connected(worker)
        if pct >= 100:
            worker.flatten(symbol, account)
            log.info(
                "FlashTrade: position flatten %s for account %s (conId=%d)",
                symbol, account, con_id)
        else:
            worker.close_pct(symbol, account, pct)
            log.info(
                "FlashTrade: position close %d%% %s for account %s (conId=%d)",
                pct, symbol, account, con_id)

    # ── Account-list action buttons (25% / 50% / 75% / Flatten / Reverse / Cancel) ──

    def _on_account_list_action(self, action: str, accounts: list[str]) -> None:
        """Dispatch the 6 account-panel buttons across all *accounts*.

        Action strings (from AccountListPanel._emit_action):
            "close_25"  → close 25 % of every position for each account
            "close_50"  → close 50 %
            "close_75"  → close 75 %
            "flatten"   → cancel all orders + close 100 % of every position
            "reverse"   → cancel all orders + flip every position
            "cancel_all"→ cancel all open orders (no position change)

        Symbol resolution uses the live position model so we always act on
        exactly what is currently held, not a stale chip-bar symbol.
        """
        if not accounts:
            log.warning("FlashTrade: account_list action '%s' — no accounts selected",
                        action)
            return

        pct_map = {"close_25": 25, "close_50": 50, "close_75": 75}

        for account in accounts:
            worker = self._find_worker_for_account(account)
            if not worker:
                log.warning(
                    "FlashTrade: account_list '%s' — no connected worker for %s",
                    action, account)
                continue

            self._ensure_worker_connected(worker)

            # ── Cancel all open orders (all actions that touch positions do
            #    this first; cancel_all does ONLY this) ─────────────────────
            if action in ("flatten", "reverse", "cancel_all"):
                worker.cancel_all_for_account(account)
                log.info("FlashTrade: cancel_all_for_account '%s' (%s)",
                         account, action)

            if action == "cancel_all":
                continue   # done — no position change needed

            # ── Resolve every open position for this account ───────────────
            pos_keys = [
                (acct, con_id)
                for acct, con_id in self._account_position.get_position_keys()
                if acct == account
            ]
            if not pos_keys:
                log.warning(
                    "FlashTrade: account_list '%s' for %s — no open positions",
                    action, account)
                continue

            for _, con_id in pos_keys:
                symbol = self._account_position.get_symbol_for_position(
                    account, con_id)
                if not symbol:
                    log.warning(
                        "FlashTrade: account_list '%s' — no symbol for "
                        "account=%s conId=%d", action, account, con_id)
                    continue

                if action == "flatten":
                    # _do_flatten cancels per-symbol again (no-op since we
                    # already ran cancel_all_for_account) then closes position.
                    worker.flatten(symbol, account)
                    log.info("FlashTrade: account_list flatten %s / %s",
                             symbol, account)

                elif action == "reverse":
                    # _do_reverse cancels per-symbol (no-op) then sends 2×
                    # offsetting order to flip through zero.
                    worker.reverse(symbol, account)
                    log.info("FlashTrade: account_list reverse %s / %s",
                             symbol, account)

                elif action in pct_map:
                    pct = pct_map[action]
                    worker.close_pct(symbol, account, pct)
                    log.info("FlashTrade: account_list close_%d%% %s / %s",
                             pct, symbol, account)

        log.info("FlashTrade: account_list action '%s' dispatched for %d account(s): %s",
                 action, len(accounts), accounts)

    # ── Layout persistence ────────────────────────────────────────────────────

    def _on_splitter_moved(self, pos: int, index: int) -> None:
        self._save_timer.start()                   # restart debounce

    def _persist_layout(self) -> None:
        layout = config_manager.load_layout()
        layout[_K_MAIN]    = self._main_splitter.sizes()
        layout[_K_RIGHT_H] = self._right_splitter.sizes()
        layout[_K_LEFT_V]  = self._top_right_splitter.sizes()
        config_manager.save_layout(layout)
        log.debug("FlashTrade layout saved")

    def _restore_layout(self) -> None:
        layout = config_manager.load_layout()
        # Default sizes matching the target layout (pixels, ~1500px wide x ~720px tall)
        self._main_splitter.setSizes(layout.get(_K_MAIN, [265, 285, 950]))
        self._right_splitter.setSizes(layout.get(_K_RIGHT_H, [760, 190]))
        self._top_right_splitter.setSizes(layout.get(_K_LEFT_V, [380, 340]))
        log.debug("FlashTrade layout restored")

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), _C_MAIN)
        p.end()

    def showEvent(self, event):
        """Re-subscribe to live price and wire any new/reconnected workers
        when the tab becomes visible."""
        super().showEvent(event)
        self._wire_all_workers()
        self._order_entry.refresh_price_subscription()
