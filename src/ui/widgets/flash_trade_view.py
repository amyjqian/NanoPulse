from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter

from src.core import config_manager
from src.core.ib_worker import IBWorkerThread
from .order_entry_panel import OrderEntryPanel
from .order_status_panel import OrderStatusPanel
from .account_list_panel import AccountListPanel
from .account_position_panel import AccountPositionPanel
from .accounts_model import AccountsTableModel
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
_K_ACCT_VIS  = "accounts_panel_visible"   # bool — false = panel hidden


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

    # Emitted whenever ``set_accounts_panel_visible`` flips the panel.
    # Payload is the new visibility (True = panel shown).  MainWindow
    # listens so it can flip the "Show Account" pill in the top bar
    # opposite the panel state.
    accounts_panel_visibility_changed = Signal(bool)

    # Emitted when a fan-out (order / flatten / cancel) skipped one or
    # more accounts because their worker is offline.  Payload is the
    # human-readable summary text + duration in ms for the host to
    # surface in the status bar.
    transient_status_message = Signal(str, int)

    def __init__(self, active_threads: dict[str, IBWorkerThread] | None = None,
                 accounts_model: AccountsTableModel | None = None, parent=None,
                 *, open_preferences=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._active_threads: dict[str,
                                   IBWorkerThread] = active_threads if active_threads is not None else {}
        self._accounts_model = accounts_model
        self._open_preferences_callback = open_preferences
        # Role gating: admins may not place trades.  Set False for admin
        # sessions via ``set_trading_allowed`` — enforced in
        # ``_on_order_requested`` as defence-in-depth beyond hidden UI.
        self._trading_allowed = True
        # label → worker instance wired
        self._connected_order_workers: dict[str, IBWorkerThread] = {}
        # label → worker for portfolio
        self._connected_portfolio_workers: dict[str, IBWorkerThread] = {}
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(300)          # debounce 300 ms
        self._save_timer.timeout.connect(self._persist_layout)
        # Snapshot of _right_splitter sizes captured the moment we
        # hide the AccountListPanel; restored on the next show so the
        # user gets back the same column width.  None until first hide.
        self._right_splitter_saved_sizes: list[int] | None = None
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
        # Hot-button group-targeted variants — bypass AccountListPanel
        # selection and fan out to the configured group's accounts.
        self._order_entry.hot_button_flatten.connect(
            self._on_hot_button_flatten)
        self._order_entry.hot_button_cancel.connect(
            self._on_hot_button_cancel)

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

        # ── Futures expiration replaced via the search popup ─────────────────
        # Whichever panel hosted the pick, both panels' market data must
        # re-point at the new contract (chips are root-symbol-keyed).
        self._order_entry.contract_replaced.connect(
            self._on_contract_replaced)
        self._book_trading.contract_replaced.connect(
            self._on_contract_replaced)

        # ── Wire order status cancel button → cancel logic ───────────────────
        self._order_status.cancel_requested.connect(self._on_cancel_requested)
        self._order_status.refresh_requested.connect(
            self._on_refresh_requested)

        # ── Wire account position refresh button → portfolio refresh ─────────
        self._account_position.refresh_requested.connect(
            self._on_position_refresh_requested)

        # ── Wire account list → positions filter ──────────────────────────────
        if isinstance(self._account_list, AccountListPanel):
            self._account_list.selection_changed.connect(
                self._on_positions_selection_changed)
            # Cash-sweep right-click menu: provide the sweep-symbol quote and
            # handle the chosen action.
            self._account_list.set_sweep_quote_provider(self._sweep_quote)
            self._account_list.sweep_requested.connect(self._on_sweep_requested)
            # Push the initial persisted selection so the view is correct
            # before the user clicks anything.
            self._on_positions_selection_changed(
                self._account_list.get_selected_accounts())

        # ── Wire position panel action buttons ───────────────────────────────
        # Header-row Flatten: flatten every open position for that account
        self._account_position.flatten_requested.connect(
            self._on_position_account_flatten)
        # Position-row buttons: Flatten (100%) · 25% · 50% · 75%
        self._account_position.position_close_requested.connect(
            self._on_position_close_requested)
        # Context menus: Options income (account rows + STK rows) / sweeps
        self._account_position.account_action_requested.connect(
            self._on_account_action_requested)
        self._account_position.position_action_requested.connect(
            self._on_position_action_requested)

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

    def set_trading_allowed(self, allowed: bool) -> None:
        """Enable/disable order placement for this session (admins → False)."""
        self._trading_allowed = bool(allowed)

    def _on_order_requested(self, payload: dict) -> None:
        """Handle Buy/Sell button click from OrderEntryPanel.

        1. Collect checked accounts from AccountListPanel.
        2. Look up saved contract details for the symbol.
        3. For each account, find the correct IBWorkerThread and place the order.
        """
        # ── 0. Role gate: admins cannot place trades ──────────────────────
        if not self._trading_allowed:
            log.warning("FlashTrade: order blocked — current user cannot trade")
            self.transient_status_message.emit(
                "Trading is disabled for admin accounts.", 4000)
            return
        # ── 1. Validate: get checked accounts ─────────────────────────────
        # Three account-resolution paths in order of priority:
        #   1. ``_flatten_account`` — single-account override used by the
        #      flatten close-needed callback (one position at a time).
        #   2. ``_target_accounts`` — explicit list override used by
        #      group-targeted hot buttons so they fan out to the
        #      configured group regardless of AccountListPanel state.
        #   3. AccountListPanel.get_selected_accounts() — default for
        #      every other Buy/Sell click.
        flatten_account = payload.get("_flatten_account")
        target_accounts = payload.get("_target_accounts")
        if flatten_account:
            accounts = [flatten_account]
        elif target_accounts:
            accounts = list(target_accounts)
            if not accounts:
                log.warning(
                    "FlashTrade: _target_accounts empty – aborting order")
                return
        else:
            if not isinstance(self._account_list, AccountListPanel):
                log.warning(
                    "FlashTrade: AccountListPanel not available – aborting order")
                return

            accounts = self._account_list.get_selected_accounts()
            if not accounts:
                log.warning("FlashTrade: no accounts checked – aborting order")
                return

        # Skip offline workers up front so the UI can tell the user what
        # was dropped instead of letting orders silently vanish inside
        # the per-account fan-out below.
        accounts, _disconnected = self._partition_accounts_by_connection(accounts)
        self._notify_skipped_accounts("Order", _disconnected)
        if not accounts:
            log.warning(
                "FlashTrade: no connected accounts among the selection – "
                "aborting order")
            return

        # ── 2. Look up contract from saved config ─────────────────────────
        symbol = payload.get("symbol", "")
        if not symbol:
            log.warning("FlashTrade: no symbol in order payload – aborting")
            return

        # Flatten / reverse / close-pct flows pre-resolve the contract from
        # the live position itself so the order routes correctly even when
        # the user's saved-contracts row has been rolled to a later
        # expiration (e.g. position on NQM6, saved row already on NQU6).
        contract = payload.get("_resolved_contract")
        if not contract:
            # Buy/Sell entry from the order panel — resolve from saved
            # contracts.  pick_active_contract handles multi-expiry roots
            # (NQM6 + NQU6 both saved as "NQ") by picking the soonest
            # non-expired contract; an exact local_symbol match still
            # pins to that specific contract.
            from src.core.reference_data import get_instance as _rd
            contract = _rd().pick_active_contract(symbol)

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
                # Stocks → Pegged to Primary (REL).  The button labels
                # are "Buy @Bid +1t" and "Sell @Ask -1t" — both fix the
                # peg offset at exactly one tick so the order rests one
                # tick inside the NBBO regardless of the user's
                # buy_bid_offset / sell_ask_offset preferences.
                # auxPrice = dollar offset from the NBBO (always >= 0).
                peg_offset = tick
                # lmtPrice = limit cap (ceiling BUY / floor SELL), 0 = no cap
                limit_price = _cap_price()
                log.info("FlashTrade: STK %s @%s %s base=%.4f tick=%.4f → "
                         "PEG_PRIM limit=%.4f peg_offset=%.4f (+1 tick)",
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

        elif order_type == "LMT":
            # Explicit passthrough limit — price fully specified by the
            # caller (Options Income ticket).  No prefs, no cap, no anchor.
            limit_price = float(payload.get("limit_price", 0))
            if limit_price <= 0:
                log.warning("FlashTrade: %s LMT but limit_price=%.4f – "
                            "aborting", action, limit_price)
                return
            log.info("FlashTrade: %s LMT %s @ %.4f (passthrough)",
                     action, symbol, limit_price)

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
                # Use the resolved contract's root symbol — payload's
                # ``symbol`` may be a local symbol (e.g. flatten on NQM6
                # passes "NQM6"), and IB expects the bare root in
                # contract.symbol with localSymbol carrying the expiry
                # code separately.
                "symbol": contract.get("symbol", "") or symbol,
                "sec_type": contract.get("sec_type", ""),
                # `or "SMART"` rather than .get(..., "SMART") because the
                # key may be present-but-empty (positions returned from
                # ib.positions() carry exchange="" until enriched).
                "exchange": contract.get("exchange") or "SMART",
                "currency": contract.get("currency") or "USD",
                "local_symbol": contract.get("local_symbol", ""),
                "expiry": contract.get("expiry", ""),
                "multiplier": contract.get("multiplier", ""),
                "account": account,
                "action": action,
                "qty": qty,
                "order_type": order_type,
                "tif": "DAY"
            }
            if payload.get("order_ref"):
                # Options ticket pre-generates the ref so its session
                # strip can follow this order's status stream/cancel it.
                params["order_ref"] = payload["order_ref"]
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
            if payload.get("_what_if_check"):
                # Options-income orders run a whatIf margin pre-check on
                # the worker before transmitting (§6 of the spec).
                worker.place_option_order(params)
            else:
                worker.place_order(params)

    def _on_account_action_requested(self, data: dict) -> None:
        """Positions-panel account context menu → route the action."""
        account = str(data.get("account") or "")
        if account and data.get("action") == "options_income":
            self._open_options_ticket(account)

    def _on_position_action_requested(self, data: dict) -> None:
        """STK position-row context menu → Options income pre-filled."""
        if data.get("action") != "options_income":
            return
        account = str(data.get("account") or "")
        if account:
            self._open_options_ticket(account,
                                      symbol=data.get("symbol") or None)

    def _open_options_ticket(self, account: str,
                             symbol: str | None = None) -> None:
        """Open the Options Income ticket pre-filled with *account* (and
        optionally *symbol*).  Orders route back through
        ``_on_order_requested`` so role-gating and blotter wiring apply."""
        if not self._trading_allowed:
            log.warning(
                "FlashTrade: options ticket blocked — user cannot trade")
            self.transient_status_message.emit(
                "Trading is disabled for admin accounts.", 4000)
            return
        from src.ui.widgets.options_income import OptionsIncomeTicket

        def _buying_power(acct: str) -> float:
            if self._accounts_model is None:
                return 0.0
            return float(self._accounts_model.get_buying_power(acct) or 0.0)

        worker = self._find_worker_for_account(account)
        if worker is not None:
            self._ensure_worker_connected(worker)

        alias = ""
        if self._accounts_model is not None:
            alias = self._accounts_model._aliases.get(account, "")
        ticket = OptionsIncomeTicket(
            account=account,
            get_worker=self._find_worker_for_account,
            get_buying_power=_buying_power,
            get_position=self._account_position.position_for_symbol,
            symbol=symbol,
            display_account=alias or account,
            get_account_positions=lambda:
                self._account_position.stk_positions_for_account(account),
            parent=self,
        )
        ticket.order_ready.connect(self._on_order_requested)
        ticket.exec()

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

    def _partition_accounts_by_connection(
        self, accounts: list[str],
    ) -> tuple[list[str], list[str]]:
        """Split *accounts* into ``(connected, disconnected)`` using the
        same predicate as ``_find_worker_for_account`` so partitioning
        never disagrees with the actual fan-out.  Accounts whose worker
        is missing entirely are also treated as disconnected so the
        status message names them too."""
        connected: list[str] = []
        disconnected: list[str] = []
        for account in accounts:
            if self._find_worker_for_account(account) is not None:
                connected.append(account)
            else:
                disconnected.append(account)
        return connected, disconnected

    def _pretty_account(self, account: str) -> str:
        """Return ``alias (account)`` when the user has set an alias,
        else just the account ID.  Reuses ``AccountsTableModel._aliases``
        the same way ``_on_order_status_updated`` does today."""
        if self._accounts_model is not None:
            alias = self._accounts_model._aliases.get(account, "")
            if alias:
                return f"{alias} ({account})"
        return account

    def _notify_skipped_accounts(
        self, action: str, disconnected: list[str],
    ) -> None:
        """Emit a status-bar message + WARNING log line summarising a
        fan-out that skipped offline accounts.  ``action`` is a short
        verb like ``"Order"`` / ``"Flatten"`` / ``"Cancel"`` so the
        text reads naturally."""
        if not disconnected:
            return
        pretty = ", ".join(self._pretty_account(a) for a in disconnected)
        msg = (f"{action} skipped on {len(disconnected)} disconnected "
               f"account(s): {pretty}")
        log.warning("FlashTrade: %s", msg)
        self.transient_status_message.emit(msg, 6000)

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
            for pos in batch:
                account = pos.get("account", "")
                if not account or account in seen_accounts:
                    continue
                seen_accounts.add(account)
                self._push_account_balances(account)

    def _account_balances(self, account: str) -> tuple[float, float, float] | None:
        """Look up (net_liq, excess_liq, buying_power) for *account* from the
        shared accounts model, or ``None`` if the account isn't known there."""
        if self._accounts_model is None:
            return None
        from .accounts_model import COL_NET_LIQ, COL_EXCESS_LIQ
        label = self._accounts_model.connection_for_account(account)
        if label is None:
            return None
        row_idx = self._accounts_model._row_map.get((label, account))
        if row_idx is None:
            return None
        row_data = self._accounts_model._table_data[row_idx]

        def _f(v) -> float:
            try:
                return float(v) if v else 0.0
            except (ValueError, TypeError):
                return 0.0

        return (_f(row_data[COL_NET_LIQ]),
                _f(row_data[COL_EXCESS_LIQ]),
                self._accounts_model.get_buying_power(account))

    def _push_account_balances(self, account: str) -> None:
        """Push an account's Net Liq / Excess Liq / BP into the position panel
        header — works for flat accounts too (they aren't in the position
        batch, so their balances would otherwise never reach the panel)."""
        bals = self._account_balances(account)
        if bals is None:
            return
        net, excess, bp = bals
        self._account_position.set_account_data(
            account, balance=net, net_liq=net, excess_liq=excess, buying_power=bp)

    def _on_positions_selection_changed(self, accounts) -> None:
        """Selection changed → filter the positions view and immediately push
        balances for the selected accounts so a flat account's Net/Excess/BP
        show right away (rather than only after the next pnlEvent)."""
        self._account_position.set_selected_accounts(accounts)
        for account in (accounts or []):
            self._push_account_balances(account)

    # ── Cash sweep ────────────────────────────────────────────────────────────

    def _sweep_symbol_price(self, symbol: str) -> float:
        """Last price for *symbol* from the order-entry price cache (0 if the
        symbol isn't subscribed / priced yet)."""
        cached = self._order_entry._price_cache.get((symbol or "").upper())
        return cached[0] if cached else 0.0

    def _sweep_quote(self, account: str, symbol: str) -> tuple[float, float]:
        """(price, held_shares) for the account's cash-sweep menu."""
        price = self._sweep_symbol_price(symbol)
        held = self._account_position.held_shares(account, symbol)
        return price, held

    def _on_sweep_requested(self, account: str, direction: str,
                            qty: int, symbol: str) -> None:
        """Cash-sweep menu action → place a MKT order for *symbol* on *account*.
        Sweep in → BUY (park positive USD cash); Sweep out → SELL (cover
        negative USD cash).  Routed through the normal order path (limit-cap /
        RTH / prefs / admin-gate all apply)."""
        if qty <= 0 or not symbol or not account:
            log.warning("FlashTrade: sweep %s ignored — qty=%s symbol=%r acct=%r",
                        direction, qty, symbol, account)
            return
        action = "BUY" if direction == "in" else "SELL"
        payload = {
            "action": action,
            "type": "MKT",
            "symbol": symbol,
            "qty": int(qty),
            "last_price": self._sweep_symbol_price(symbol),
            "_target_accounts": [account],
        }
        log.info("FlashTrade: sweep %s → %s %d %s @MKT account=%s",
                 direction, action, qty, symbol, account)
        self._on_order_requested(payload)

    def _on_pnl_single_updated(self, account: str, con_id: int,
                               daily_pnl: float, realized_pnl: float,
                               unrealized_pnl: float) -> None:
        """Per-position P&L from pnlSingleEvent → update position panel."""
        self._account_position.update_pnl_single(
            account, con_id, daily_pnl, realized_pnl, unrealized_pnl)

    def _on_account_pnl_updated(self, account: str, daily_pnl: float,
                                realized_pnl: float,
                                unrealized_pnl: float) -> None:
        """Account-level P&L from pnlEvent → update account header row.

        pnlEvent fires per-account regardless of positions, so this is also the
        moment to push Net Liq / Excess Liq / BP for flat accounts (which never
        appear in the position batch)."""
        self._account_position.set_account_data(
            account, daily_pnl=daily_pnl,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl)
        self._push_account_balances(account)

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

        checked_list = list(self._account_list.get_selected_accounts())
        if not checked_list:
            log.warning(
                "FlashTrade: cancel-all blocked for '%s' — no accounts checked", symbol)
            return
        self._do_cancel_all_for_symbol(symbol, checked_list)

    def _on_hot_button_cancel(self, symbol: str, accounts: list) -> None:
        """Hot-button group-targeted cancel.  Bypasses the
        AccountListPanel selection and cancels open orders for
        *symbol* on the accounts the caller resolved from the
        configured trading group."""
        accounts_list = list(accounts) if accounts else []
        if not accounts_list:
            log.warning(
                "FlashTrade: hot-button cancel blocked for '%s' — empty "
                "group account list", symbol)
            return
        log.info(
            "FlashTrade: hot-button cancel '%s' on %d group account(s): %s",
            symbol, len(accounts_list), sorted(accounts_list))
        self._do_cancel_all_for_symbol(symbol, accounts_list)

    def _do_cancel_all_for_symbol(
            self, symbol: str, accounts: list[str]) -> None:
        """Shared cancel-all execution.  Caller has already resolved
        the target accounts (from AccountListPanel selection or a
        named hot-button group)."""
        connected_list, _disc = self._partition_accounts_by_connection(
            accounts)
        self._notify_skipped_accounts("Cancel", _disc)
        if not connected_list:
            log.warning(
                "FlashTrade: cancel-all blocked for '%s' — every target "
                "account is on a disconnected connection", symbol)
            return
        checked: set[str] = set(connected_list)
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

        checked_list = list(self._account_list.get_selected_accounts())
        if not checked_list:
            log.warning(
                "FlashTrade: flatten blocked for '%s' — no accounts checked", symbol)
            return
        self._do_flatten_for_symbol(symbol, checked_list)

    def _on_hot_button_flatten(self, symbol: str, accounts: list) -> None:
        """Hot-button group-targeted flatten.  Bypasses the
        AccountListPanel selection and flattens *symbol* on the
        accounts the caller resolved from the configured trading
        group."""
        accounts_list = list(accounts) if accounts else []
        if not accounts_list:
            log.warning(
                "FlashTrade: hot-button flatten blocked for '%s' — empty "
                "group account list", symbol)
            return
        log.info(
            "FlashTrade: hot-button flatten '%s' on %d group account(s): %s",
            symbol, len(accounts_list), sorted(accounts_list))
        self._do_flatten_for_symbol(symbol, accounts_list)

    def _do_flatten_for_symbol(
            self, symbol: str, accounts: list[str]) -> None:
        """Shared flatten execution.  Caller has already resolved the
        target accounts (from AccountListPanel selection or a named
        hot-button group)."""
        connected_list, _disc = self._partition_accounts_by_connection(
            accounts)
        self._notify_skipped_accounts("Flatten", _disc)
        if not connected_list:
            log.warning(
                "FlashTrade: flatten blocked for '%s' — every target "
                "account is on a disconnected connection", symbol)
            return
        checked: set[str] = set(connected_list)
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
        # applies limit cap, RTH, prefs, etc.  Forward _resolved_contract
        # from the worker so the offsetting order routes against the
        # position's actual contract (necessary when the saved-contracts
        # DB has been rolled to a later expiration).
        payload = {
            "action": action,
            "type": "MKT",
            "symbol": symbol,
            "qty": qty,
            "last_price": last_price,
            # Force to a single account — override normal "all checked" logic
            "_flatten_account": account,
        }
        if data.get("_resolved_contract"):
            payload["_resolved_contract"] = data["_resolved_contract"]
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

        checked_list = list(self._account_list.get_selected_accounts())
        if not checked_list:
            log.warning(
                "FlashTrade: reverse blocked for '%s' — no accounts checked", symbol)
            return
        connected_list, _disc = self._partition_accounts_by_connection(checked_list)
        self._notify_skipped_accounts("Reverse", _disc)
        if not connected_list:
            log.warning(
                "FlashTrade: reverse blocked for '%s' — every checked "
                "account is on a disconnected connection", symbol)
            return
        checked: set[str] = set(connected_list)

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
        if data.get("_resolved_contract"):
            payload["_resolved_contract"] = data["_resolved_contract"]
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

    def _on_contract_replaced(self, sym: str) -> None:
        """A futures root was re-pointed at a different expiration —
        refresh market data in both panels regardless of which one
        hosted the pick."""
        self._order_entry.refresh_symbol_contract(sym)
        self._book_trading.refresh_symbol_contract(sym)

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

        checked_list = list(self._account_list.get_selected_accounts())
        if not checked_list:
            log.warning(
                "FlashTrade: close_tier %s blocked for '%s' — "
                "no accounts checked", side, symbol)
            return
        # Drop accounts on offline workers so the user sees a status
        # message instead of having close requests vanish silently.
        connected_list, _disc = self._partition_accounts_by_connection(checked_list)
        self._notify_skipped_accounts("Close tier", _disc)
        if not connected_list:
            log.warning(
                "FlashTrade: close_tier %s blocked for '%s' — every "
                "checked account is on a disconnected connection", side, symbol)
            return
        checked: set[str] = set(connected_list)

        # Resolve an absolute reference price using the same fallback
        # chain the bracket-children path uses on the worker side:
        # cached last → payload last → cached bid/ask midpoint.  Last
        # resort, surface a modal so the user knows why their click
        # did nothing instead of dropping the request silently.
        last_price = 0.0
        cached = self._order_entry._price_cache.get(symbol.upper())
        if cached:
            last_price = float(cached[0])
        if last_price <= 0:
            last_price = float(data.get("last_price") or 0.0)
        if last_price <= 0 and cached:
            bid = float(cached[1] or 0.0)
            ask = float(cached[2] or 0.0)
            if bid > 0 and ask > 0:
                last_price = (bid + ask) / 2.0
                log.info(
                    "FlashTrade: close_tier %s '%s' using bid/ask mid "
                    "%.4f (no live last)", side, symbol, last_price)
        if last_price <= 0:
            log.warning(
                "FlashTrade: close_tier %s '%s' blocked — no live price",
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

        self._ensure_worker_connected(worker)

        # ── Step 1: cancel ALL open orders for this account (no symbol filter).
        # This runs even for a flat account so a header-row Flatten still clears
        # any resting orders that have no matching position yet.
        worker.cancel_all_for_account(account)
        log.info("FlashTrade: cancel_all_for_account sent for %s", account)

        # ── Step 2: close each non-zero position via the normal flatten path
        pos_keys = [
            (acct, con_id)
            for acct, con_id in self._account_position.get_position_keys()
            if acct == account
        ]
        for _, con_id in pos_keys:
            symbol = self._account_position.get_symbol_for_position(account, con_id)
            if symbol:
                worker.flatten(symbol, account)
                log.info(
                    "FlashTrade: position flatten %s for account %s", symbol, account)

        log.info(
            "FlashTrade: account flatten complete — cancelled open orders, "
            "%d position(s) closed in %s", len(pos_keys), account)

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
        # Only snapshot _right_splitter sizes when the panel is
        # visible — otherwise the AccountListPanel slot is collapsed
        # to 0 and persisting that wipes the user's column width.
        if isinstance(self._account_list, AccountListPanel) \
                and self._account_list.isVisible():
            layout[_K_RIGHT_H] = self._right_splitter.sizes()
        layout[_K_LEFT_V]  = self._top_right_splitter.sizes()
        layout[_K_ACCT_VIS] = (
            self._account_list.isVisible()
            if isinstance(self._account_list, AccountListPanel)
            else True)
        config_manager.save_layout(layout)
        log.debug("FlashTrade layout saved")

    def _restore_layout(self) -> None:
        layout = config_manager.load_layout()
        # Default sizes matching the target layout (pixels, ~1500px wide x ~720px tall)
        self._main_splitter.setSizes(layout.get(_K_MAIN, [265, 285, 950]))
        self._right_splitter.setSizes(layout.get(_K_RIGHT_H, [800, 150]))
        self._top_right_splitter.setSizes(layout.get(_K_LEFT_V, [380, 340]))
        # Restore accounts-panel visibility; default True for
        # backwards-compatibility with users who never hid it.
        if isinstance(self._account_list, AccountListPanel):
            visible = bool(layout.get(_K_ACCT_VIS, True))
            if not visible:
                self.set_accounts_panel_visible(False)
        log.debug("FlashTrade layout restored")

    # ── Accounts-panel show/hide ──────────────────────────────────────────────

    # A splitter-collapsed AccountListPanel reports ``isVisible() == True``
    # but has effectively zero width.  We treat anything below this
    # threshold as "hidden" for toggle purposes so the user can drag the
    # splitter shut and then click the Account pill to reopen the panel
    # at a usable size on the next click.
    _ACCT_PANEL_MIN_USABLE_W = 60
    # Fallback width used when "show" is requested from a collapsed state
    # and we have no usable saved sizes to restore.
    _ACCT_PANEL_DEFAULT_W = 240

    def is_accounts_panel_visible(self) -> bool:
        if not isinstance(self._account_list, AccountListPanel):
            return False
        if not self._account_list.isVisible():
            return False
        # Treat a splitter-collapsed panel as hidden so the next pill
        # click expands it instead of toggling it off.
        return self._account_list.width() >= self._ACCT_PANEL_MIN_USABLE_W

    def set_accounts_panel_visible(self, visible: bool) -> None:
        """Show or hide the AccountListPanel inside the right
        splitter.  Snapshots the current splitter sizes on hide and
        restores them on show so the user keeps their column width.
        When the panel was collapsed via the splitter (width near 0)
        we ignore the stale saved sizes and force a sensible default
        on show so a "Account" pill click always reveals a usable
        panel.  Persists the new state and emits
        ``accounts_panel_visibility_changed`` so the host can flip
        the 'Show Account' pill in the top bar."""
        if not isinstance(self._account_list, AccountListPanel):
            return
        currently = self.is_accounts_panel_visible()
        if currently == bool(visible):
            return
        if not visible:
            # Capture sizes before the hide so the next show can put
            # the splitter back exactly where it was.
            self._right_splitter_saved_sizes = (
                self._right_splitter.sizes())
            self._account_list.hide()
        else:
            self._account_list.show()
            saved = self._right_splitter_saved_sizes
            usable = (
                saved is not None
                and len(saved) >= 2
                and saved[1] >= self._ACCT_PANEL_MIN_USABLE_W
            )
            if usable:
                self._right_splitter.setSizes(saved)
            else:
                # No usable saved sizes (either never hidden, or the
                # last hide captured a collapsed state).  Fall back to
                # a default width derived from the splitter's current
                # total so the panel always reappears at a sensible
                # size regardless of how the user got here.
                sizes = self._right_splitter.sizes()
                total = sum(sizes) or self._right_splitter.width()
                if total <= 0:
                    self._right_splitter.setSizes(
                        [800, self._ACCT_PANEL_DEFAULT_W])
                else:
                    acct_w = max(
                        self._ACCT_PANEL_DEFAULT_W, total // 4)
                    acct_w = min(acct_w, total - 200)
                    main_w = total - acct_w
                    self._right_splitter.setSizes([main_w, acct_w])
        self.accounts_panel_visibility_changed.emit(bool(visible))
        # Persist immediately rather than waiting for the splitter
        # debounce — visibility changes are explicit user actions
        # and should survive an immediate restart.
        self._persist_layout()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), _C_MAIN)
        p.end()

    def showEvent(self, event):
        """Re-subscribe to live price and wire any new/reconnected workers
        when the tab becomes visible."""
        super().showEvent(event)
        # Any Python exception raised inside a Qt virtual override
        # (showEvent is one) trips a Shiboken bug on macOS that turns
        # the exception into a segfault.  Catch + log here so the app
        # stays alive and we get a stack trace instead of a crash.
        try:
            self._wire_all_workers()
        except Exception:
            log.exception("FlashTrade: _wire_all_workers raised in showEvent")
        try:
            self._order_entry.refresh_price_subscription()
        except Exception:
            log.exception(
                "FlashTrade: refresh_price_subscription raised in showEvent")
