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
from .accounts_model import AccountsTableModel

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
_K_MAIN = "flash_main_splitter"
_K_RIGHT_V = "flash_right_v_splitter"
_K_TOP_RIGHT = "flash_top_right_splitter"


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
    layout.addWidget(lbl, alignment=Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    layout.addStretch(1)
    return panel


class FlashTradeView(QWidget):
    """Skeleton layout for the FlashTrade page — section titles only."""

    def __init__(self, active_threads: dict[str, IBWorkerThread] | None = None,
                 accounts_model: AccountsTableModel | None = None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._active_threads: dict[str, IBWorkerThread] = active_threads if active_threads is not None else {}
        self._accounts_model = accounts_model
        self._connected_order_workers: dict[str, IBWorkerThread] = {}  # label → worker instance wired
        self._connected_portfolio_workers: dict[str, IBWorkerThread] = {}  # label → worker for portfolio
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
        self._book_trading = _titled_panel("Book Trading")

        # ── Right 3/5 — split top / bottom ────────────────────────────────────
        self._right_splitter = QSplitter(Qt.Orientation.Vertical)
        self._right_splitter.setHandleWidth(2)
        self._right_splitter.setStyleSheet(_SPLITTER_SS)

        # Top half of right side — Order Status | Account List (resizable)
        self._top_right_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._top_right_splitter.setHandleWidth(2)
        self._top_right_splitter.setStyleSheet(_SPLITTER_SS)
        self._order_status = OrderStatusPanel(self)

        if self._accounts_model is not None:
            self._account_list = AccountListPanel(self._accounts_model, self)
        else:
            self._account_list = _titled_panel("Account List")

        self._top_right_splitter.addWidget(self._order_status)
        self._top_right_splitter.addWidget(self._account_list)
        self._top_right_splitter.setStretchFactor(0, 1)
        self._top_right_splitter.setStretchFactor(1, 1)

        # Bottom half of right side — Account Position
        self._account_position = AccountPositionPanel(
            accounts_model=self._accounts_model,
            parent=self,
        )

        self._right_splitter.addWidget(self._top_right_splitter)
        self._right_splitter.addWidget(self._account_position)
        self._right_splitter.setStretchFactor(0, 1)
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

        # ── Wire order status cancel button → cancel logic ───────────────────
        self._order_status.cancel_requested.connect(self._on_cancel_requested)
        self._order_status.refresh_requested.connect(self._on_refresh_requested)

        # ── Wire account position refresh button → portfolio refresh ─────────
        self._account_position.refresh_requested.connect(self._on_position_refresh_requested)

    # ── Order placement ───────────────────────────────────────────────────────

    def _wire_all_workers(self) -> None:
        """Ensure every active worker's order_status_updated signal is wired."""
        for label, worker in self._active_threads.items():
            if worker.isRunning():
                self._ensure_worker_connected(worker)

    def _on_order_requested(self, payload: dict) -> None:
        """Handle Buy/Sell button click from OrderEntryPanel.

        1. Collect checked accounts from AccountListPanel.
        2. Look up saved contract details for the symbol.
        3. For each account, find the correct IBWorkerThread and place the order.
        """
        # ── 1. Validate: get checked accounts ─────────────────────────────
        if not isinstance(self._account_list, AccountListPanel):
            log.warning("FlashTrade: AccountListPanel not available – aborting order")
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
            log.warning("FlashTrade: no saved contract for '%s' – aborting", symbol)
            return

        conid = contract.get("conid")
        if not conid:
            log.warning("FlashTrade: saved contract for '%s' has no conid – aborting", symbol)
            return

        action = payload.get("action", "BUY")
        qty = payload.get("qty", 1)
        order_type = payload.get("type", "MKT")
        last_price = payload.get("last_price", 0.0)
        sec_type = contract.get("sec_type", "")

        # ── Compute limit price when user has offsets configured ──────────
        limit_price = None
        if last_price > 0:
            from src.utils.helpers import calculate_capped_price, tick_size_for_contract
            from src.core.trading_prefs import trading_prefs

            tick = tick_size_for_contract(
                sec_type,
                symbol=contract.get("symbol", ""),
                local_symbol=contract.get("local_symbol", ""),
            )
            prefs = trading_prefs.get(sec_type)

            if order_type in ("BID", "ASK", "MID"):
                # Buy @Bid / Sell @Ask / @Mid → use buy_bid_offset / sell_ask_offset
                limit_price = calculate_capped_price(
                    symbol_type=sec_type,
                    side=action,
                    base_price=last_price,
                    tick_size=tick,
                    offset_key="side",
                )
                log.info("FlashTrade: %s %s %s base=%.4f tick=%.4f → limit=%.4f",
                         action, order_type, symbol, last_price, tick, limit_price)
                order_type = "LMT"

            elif order_type == "MKT":
                # MKT → check if Limit Cap Offset is set; if so, cap the MKT
                cap_value = float(prefs.get("limit_cap", 0))
                if cap_value != 0:
                    limit_price = calculate_capped_price(
                        symbol_type=sec_type,
                        side=action,
                        base_price=last_price,
                        tick_size=tick,
                        offset_key="limit_cap",
                    )
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
                log.warning("FlashTrade: no connected worker for account %s – skipping", account)
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
        """Connect order_status_updated and portfolio_updated signals if not already connected.

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
                prev.order_status_updated.disconnect(self._on_order_status_updated)
            except RuntimeError:
                pass
            try:
                prev.portfolio_updated.disconnect(self._on_portfolio_updated)
            except RuntimeError:
                pass
            log.debug("FlashTrade: unwired stale signals for worker %s", label)

        worker.order_status_updated.connect(self._on_order_status_updated)
        worker.portfolio_updated.connect(self._on_portfolio_updated)
        self._connected_order_workers[label] = worker
        self._connected_portfolio_workers[label] = worker
        log.debug("FlashTrade: wired order_status_updated + portfolio_updated for worker %s", label)

        # Re-snapshot: the worker's initial snapshot may have fired before
        # this signal was connected.  Ask it to emit all known orders again.
        if worker.is_ib_connected:
            worker.request_order_snapshot()
            worker.request_portfolio_snapshot()

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
        })

        # When an order fills, IB *should* fire updatePortfolioEvent but
        # on paper accounts it can be delayed or missed.  Schedule a
        # portfolio refresh after a short delay to guarantee the panel
        # reflects the new position.
        if status in ("Filled", "Completed"):
            QTimer.singleShot(2000, self._refresh_portfolio)

    def _on_portfolio_updated(self, batch: list[dict]) -> None:
        """Receive portfolio position batch from worker thread → update panel."""
        log.debug("FlashTrade: portfolio batch received (%d positions)", len(batch))
        self._account_position.update_positions(batch)

        # Feed account balance info from accounts_model if available
        if self._accounts_model is not None:
            # Collect unique accounts first to avoid repeated lookups
            seen_accounts: set[str] = set()
            from .accounts_model import COL_NET_LIQ, COL_ACCOUNT
            for pos in batch:
                account = pos.get("account", "")
                if not account or account in seen_accounts:
                    continue
                seen_accounts.add(account)
                # Find the account row in the model to get NetLiquidation
                for row_idx in range(self._accounts_model.rowCount()):
                    row_data = self._accounts_model._table_data[row_idx]
                    if row_data[COL_ACCOUNT] == account:
                        try:
                            bal = float(row_data[COL_NET_LIQ]) if row_data[COL_NET_LIQ] else 0
                        except (ValueError, TypeError):
                            bal = 0
                        self._account_position.set_account_data(account, bal, 0)
                        break

    def _refresh_portfolio(self) -> None:
        """Ask every connected worker to re-push portfolio positions."""
        for label, worker in self._active_threads.items():
            if worker.isRunning() and worker.is_ib_connected:
                worker.request_portfolio_refresh()
        log.debug("FlashTrade: portfolio refresh requested for all workers")

    def _on_cancel_requested(self, order_id: str) -> None:
        """Cancel button clicked in OrderStatusPanel → route to correct worker.

        order_id format: ``{connection_label}_{orderId}``
        """
        parts = order_id.split("_", 1)
        if len(parts) < 2:
            log.warning("FlashTrade: malformed order_id for cancel: %s", order_id)
            return

        label = parts[0]
        worker = self._active_threads.get(label)
        if worker and worker.isRunning() and worker.is_ib_connected:
            worker.cancel_order(order_id)
            log.info("FlashTrade: cancel forwarded to worker %s for order %s", label, order_id)
        else:
            log.warning("FlashTrade: worker %s not available for cancel of %s", label, order_id)

    def _on_refresh_requested(self) -> None:
        """Refresh button clicked → ask every connected worker to re-snapshot."""
        count = 0
        for label, worker in self._active_threads.items():
            if worker.isRunning() and worker.is_ib_connected:
                worker.request_order_snapshot()
                worker.request_portfolio_refresh()
                count += 1
        log.info("FlashTrade: refresh triggered for %d worker(s)", count)

    def _on_position_refresh_requested(self) -> None:
        """Refresh button clicked in AccountPositionPanel → ask worker to refresh portfolio."""
        for label, worker in self._active_threads.items():
            if worker.isRunning() and worker.is_ib_connected:
                worker.request_portfolio_refresh()
                log.info("FlashTrade: portfolio refresh requested for worker %s", label)

    # ── Layout persistence ────────────────────────────────────────────────────

    def _on_splitter_moved(self, pos: int, index: int) -> None:
        self._save_timer.start()                   # restart debounce

    def _persist_layout(self) -> None:
        layout = config_manager.load_layout()
        layout[_K_MAIN] = self._main_splitter.sizes()
        layout[_K_RIGHT_V] = self._right_splitter.sizes()
        layout[_K_TOP_RIGHT] = self._top_right_splitter.sizes()
        config_manager.save_layout(layout)
        log.debug("FlashTrade layout saved")

    def _restore_layout(self) -> None:
        layout = config_manager.load_layout()
        if _K_MAIN in layout:
            self._main_splitter.setSizes(layout[_K_MAIN])
        if _K_RIGHT_V in layout:
            self._right_splitter.setSizes(layout[_K_RIGHT_V])
        if _K_TOP_RIGHT in layout:
            self._top_right_splitter.setSizes(layout[_K_TOP_RIGHT])
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
