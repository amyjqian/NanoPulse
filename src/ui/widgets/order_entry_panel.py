from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.core import config_manager

log = logging.getLogger(__name__)

# ── Colours ───────────────────────────────────────────────────────────────────
BG_PANEL = "#1e2230"
BG_INPUT = "#272b3a"
BG_MAIN = "#161a25"
TEXT = "#C9D1D9"
TEXT_DIM = "#8B949E"
BORDER = "#30363D"
ACCENT = "#3FB950"

CLR_BUY = "#2e7d32"
CLR_SELL = "#c62828"
CLR_FLATTEN = "#cc6600"
CLR_REVERSE = "#8E44AD"
CLR_CLOSE = "#8E8E93"
CLR_CANCEL = "#3A3A3C"

# Qty presets per asset class
_FUT_QTYS = [1, 2, 3, 5]
_STK_QTYS = [10, 25, 50, 100, 500]
_FUT_DEFAULT = 1
_STK_DEFAULT = 100

# ── Fonts ─────────────────────────────────────────────────────────────────────
_TITLE_FONT = QFont("Helvetica Neue, Arial", 11)
_TITLE_FONT.setWeight(QFont.Weight.Bold)

_SYMBOL_FONT = QFont("Helvetica Neue, Arial", 14)
_SYMBOL_FONT.setWeight(QFont.Weight.Bold)

_PRICE_FONT = QFont("Menlo, Consolas, monospace", 14)
_PRICE_FONT.setWeight(QFont.Weight.Bold)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_btn(text: str, colour: str, min_h: int = 32) -> QPushButton:
    """Flat styled action button."""
    btn = QPushButton(text)
    btn.setMinimumHeight(min_h)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {colour};
            color: #ffffff;
            border: none;
            border-radius: 4px;
            padding: 4px 8px;
            font-weight: bold;
            font-size: 11px;
        }}
        QPushButton:hover {{ background: {colour}; border: 1px solid #ffffff30; }}
        QPushButton:pressed {{ background: {colour}; border: 1px solid #ffffff60; }}
    """)
    return btn


def _sec_type_for(sym: str) -> str:
    """Return 'FUT', 'STK', 'OPT', or '?' for a symbol.

    When multiple contracts share the same base symbol (e.g. NVDA has STK
    and OPT entries), prioritise STK > FUT > OPT so the quantity preset
    matches what the user most likely intended.
    """
    contracts = config_manager.load_contracts()
    q = sym.upper()
    # Collect all sec_types that match
    matched_types: set[str] = set()
    for c in contracts:
        if c.get("symbol", "").upper() == q or c.get("local_symbol", "").upper() == q:
            matched_types.add(c.get("sec_type", "?"))
    if not matched_types:
        # prefix fallback
        for c in contracts:
            if c.get("symbol", "").upper().startswith(q) or c.get("local_symbol", "").upper().startswith(q):
                matched_types.add(c.get("sec_type", "?"))
    if not matched_types:
        return "?"
    # Priority: STK > FUT > OPT > anything else
    for preferred in ("STK", "FUT", "OPT"):
        if preferred in matched_types:
            return preferred
    return matched_types.pop()


def _apply_chip_style(btn: QPushButton, selected: bool) -> None:
    """Apply blue-selected or grey-unselected style to a symbol chip button."""
    if selected:
        btn.setStyleSheet(
            "QPushButton{background:#0071e3;color:white;border:none;"
            "border-radius:6px;padding:4px 12px;font-weight:bold;font-size:12px}"
            "QPushButton:hover{background:#0077ed}")
    else:
        btn.setStyleSheet(
            "QPushButton{background:#3a3a4a;color:#ccc;border:none;"
            "border-radius:6px;padding:4px 12px;font-weight:600;font-size:12px}"
            "QPushButton:hover{background:#4a4a5a}")



# ═══════════════════════════════════════════════════════════════════════════════
#  OrderEntryPanel
# ═══════════════════════════════════════════════════════════════════════════════

_COMBO_SS = f"""
    QComboBox {{
        background: {BG_INPUT}; color: {TEXT};
        border: 1px solid {BORDER}; border-radius: 4px;
        padding: 3px 8px; font-size: 11px;
    }}
    QComboBox:hover {{ border-color: {ACCENT}; }}
    QComboBox:focus {{ border-color: {ACCENT}; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox::down-arrow {{ image: none; border: none; width: 0; }}
    QComboBox QAbstractItemView {{
        background: {BG_INPUT}; color: {TEXT};
        border: 1px solid {BORDER}; border-radius: 4px;
        selection-background-color: #3a3f50;
        selection-color: {TEXT};
        padding: 2px; outline: none;
    }}
"""


class OrderEntryPanel(QWidget):
    """Full Order Entry widget – drop-in replacement for _titled_panel."""

    # Emitted when user clicks an action button:
    #   {"action": "BUY"/"SELL", "type": "MKT"/"BID"/"ASK",
    #    "symbol": str, "qty": int}
    order_requested = Signal(dict)

    def __init__(self, active_threads: dict | None = None, parent=None):
        super().__init__(parent)
        self._active_threads: dict = active_threads if active_threads is not None else {}
        self._connected_worker = None  # track worker for TWS lookup
        self._price_worker = None      # track worker for live price subscription
        self._price_received = False   # set True when first price tick arrives
        self._price_retry_workers: list = []
        self._price_params: dict = {}
        self._symbols: list[str] = config_manager.load_order_entry_symbols()
        self._active_symbol: str | None = self._symbols[0] if self._symbols else None
        self._active_qty: int = 1
        self._sym_buttons: dict[str, QPushButton] = {}

        self._setup_ui()
        self._rebuild_symbol_buttons()
        self._rebuild_qty_buttons()
        self._refresh_action_labels()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setObjectName("OrderEntryPanel")
        self.setStyleSheet(f"""
            QWidget#OrderEntryPanel {{
                background: {BG_PANEL};
                border: 1px solid {BORDER};
                border-radius: 4px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)

        # ── Title ─────────────────────────────────────────────────────────────
        title = QLabel("Order Entry")
        title.setFont(_TITLE_FONT)
        title.setStyleSheet(f"color: {TEXT}; border: none;")
        root.addWidget(title)

        # ── Search combo + active symbol / price row ──────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        self._combo = QComboBox()
        self._combo.setEditable(True)
        self._combo.setMaxVisibleItems(20)
        self._combo.setFixedHeight(28)
        self._combo.setStyleSheet(_COMBO_SS)
        self._combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo.lineEdit().setPlaceholderText("Add symbol (look up)..")
        self._combo.activated.connect(self._on_combo_activated)
        self._combo.lineEdit().returnPressed.connect(self._on_return_pressed)
        self._refresh_combo_items()

        # Live filter: as user types, matching contracts appear in dropdown
        completer = self._combo.completer()
        if completer:
            completer.setFilterMode(Qt.MatchFlag.MatchContains)
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer.popup().setStyleSheet(f"""
                QAbstractItemView {{
                    background: {BG_INPUT}; color: {TEXT};
                    border: 1px solid {BORDER}; border-radius: 4px;
                    selection-background-color: #3a3f50;
                    selection-color: {TEXT};
                    padding: 2px; outline: none;
                }}
            """)

        self._lbl_symbol = QLabel("")
        self._lbl_symbol.setFont(_SYMBOL_FONT)
        self._lbl_symbol.setStyleSheet(f"color: {TEXT}; border: none;")

        self._lbl_price = QLabel("")
        self._lbl_price.setFont(_PRICE_FONT)
        self._lbl_price.setStyleSheet(f"color: {ACCENT}; border: none;")

        # Right side: symbol + price, right-aligned
        right_side = QHBoxLayout()
        right_side.setSpacing(6)
        right_side.addStretch()
        right_side.addWidget(self._lbl_symbol)
        right_side.addWidget(self._lbl_price)

        top_row.addWidget(self._combo, stretch=1)
        top_row.addLayout(right_side, stretch=1)
        root.addLayout(top_row)

        root.addSpacing(8)

        # ── Symbol toggle buttons (grid: 6 cols, STK rows then FUT rows) ────────
        self._sym_grid = QGridLayout()
        self._sym_grid.setSpacing(4)
        self._sym_grid.setContentsMargins(0, 0, 0, 0)
        sym_container = QWidget()
        sym_container.setStyleSheet("border: none;")
        sym_container.setContentsMargins(0, 0, 0, 0)
        sym_container.setLayout(self._sym_grid)
        root.addWidget(sym_container)

        root.addSpacing(12)

        # ── Quantity buttons row ──────────────────────────────────────────────
        self._qty_layout = QHBoxLayout()
        self._qty_layout.setSpacing(4)
        self._qty_layout.setContentsMargins(0, 0, 0, 0)
        qty_container = QWidget()
        qty_container.setStyleSheet("border: none;")
        qty_container.setContentsMargins(0, 0, 0, 0)
        qty_container.setLayout(self._qty_layout)
        root.addWidget(qty_container)

        root.addSpacing(12)

        # ── Action buttons (2×2) ─────────────────────────────────────────────
        action_grid = QGridLayout()
        action_grid.setSpacing(4)
        action_grid.setContentsMargins(0, 0, 0, 0)
        self._btn_buy_mkt = _make_btn("Buy @MKT", CLR_BUY, 36)
        self._btn_sell_mkt = _make_btn("Sell @MKT", CLR_SELL, 36)
        self._btn_buy_bid = _make_btn("Buy @Bid", CLR_BUY, 36)
        self._btn_sell_ask = _make_btn("Sell @Ask", CLR_SELL, 36)
        action_grid.addWidget(self._btn_buy_mkt, 0, 0)
        action_grid.addWidget(self._btn_sell_mkt, 0, 1)
        action_grid.addWidget(self._btn_buy_bid, 1, 0)
        action_grid.addWidget(self._btn_sell_ask, 1, 1)

        self._btn_buy_mkt.clicked.connect(lambda: self._emit_order("BUY", "MKT"))
        self._btn_sell_mkt.clicked.connect(lambda: self._emit_order("SELL", "MKT"))
        self._btn_buy_bid.clicked.connect(lambda: self._emit_order("BUY", "BID"))
        self._btn_sell_ask.clicked.connect(lambda: self._emit_order("SELL", "ASK"))

        action_w = QWidget()
        action_w.setStyleSheet("border: none;")
        action_w.setContentsMargins(0, 0, 0, 0)
        action_w.setLayout(action_grid)
        root.addWidget(action_w)

        # ── Position management (3×2) ────────────────────────────────────────
        pm_grid = QGridLayout()
        pm_grid.setSpacing(4)
        pm_grid.setContentsMargins(0, 0, 0, 0)
        pm_grid.addWidget(_make_btn("25%", CLR_CLOSE, 30), 0, 0)
        pm_grid.addWidget(_make_btn("50%", CLR_CLOSE, 30), 0, 1)
        pm_grid.addWidget(_make_btn("75%", CLR_CLOSE, 30), 1, 0)
        pm_grid.addWidget(_make_btn("Flatten", CLR_FLATTEN, 30), 1, 1)
        pm_grid.addWidget(_make_btn("Reverse", CLR_REVERSE, 30), 2, 0)
        pm_grid.addWidget(_make_btn("Cancel", CLR_CANCEL, 30), 2, 1)

        pm_w = QWidget()
        pm_w.setStyleSheet("border: none;")
        pm_w.setContentsMargins(0, 0, 0, 0)
        pm_w.setLayout(pm_grid)
        root.addWidget(pm_w)

        root.addStretch(1)

    # ── Combo / symbol search ─────────────────────────────────────────────────

    def _refresh_combo_items(self) -> None:
        """Populate the combo dropdown with saved contracts not already added."""
        self._combo.blockSignals(True)
        self._combo.clear()
        contracts = config_manager.load_contracts()
        added_upper = {s.upper() for s in self._symbols}
        seen: set[str] = set()
        for c in contracts:
            sym = c.get("symbol", "")
            if sym.upper() in added_upper:
                continue
            sec = c.get("sec_type", "")
            local = c.get("local_symbol", "")
            exchange = c.get("exchange", "")
            key = f"{sym}_{local}_{sec}"
            if key in seen:
                continue
            seen.add(key)
            detail = local if local != sym else exchange
            label = f"{sym}  –  {detail}  ({sec})" if detail else f"{sym}  ({sec})"
            self._combo.addItem(label, sym)
        self._combo.setCurrentText("")
        self._combo.blockSignals(False)

    def _on_combo_activated(self, index: int) -> None:
        """User selected an item from the dropdown."""
        sym = self._combo.itemData(index)
        if sym:
            self._add_symbol(sym)

    def _on_return_pressed(self) -> None:
        """Enter pressed – filter matches. If one, add it. If many, open popup."""
        text = self._combo.currentText().strip().upper()
        if not text:
            return

        # Check if user selected a full combo item (exact label match)
        idx = self._combo.currentIndex()
        if idx >= 0 and self._combo.itemData(idx):
            label_text = self._combo.itemText(idx).strip().upper()
            typed_text = text
            if typed_text == label_text or self._combo.itemData(idx).upper() == typed_text:
                self._add_symbol(self._combo.itemData(idx))
                return

        # Find all matching contracts
        contracts = config_manager.load_contracts()
        matches: list[dict] = []
        seen: set[str] = set()
        for c in contracts:
            sym = (c.get("symbol") or "").upper()
            local = (c.get("local_symbol") or "").upper()
            key = f"{sym}_{local}_{c.get('sec_type')}"
            if key in seen:
                continue
            if text in sym or text in local or sym.startswith(text) or local.startswith(text):
                seen.add(key)
                matches.append(c)

        if len(matches) == 1:
            # Single match – add directly
            self._add_symbol(matches[0].get("symbol", ""))
        elif len(matches) > 1:
            # Multiple matches – rebuild combo with filtered items and show popup
            self._combo.blockSignals(True)
            self._combo.clear()
            for c in matches:
                sym = c.get("symbol", "")
                sec = c.get("sec_type", "")
                local = c.get("local_symbol", "")
                exchange = c.get("exchange", "")
                detail = local if local != sym else exchange
                label = f"{sym}  –  {detail}  ({sec})" if detail else f"{sym}  ({sec})"
                self._combo.addItem(label, sym)
            self._combo.blockSignals(False)
            self._combo.showPopup()
        else:
            # No local match → try TWS lookup (assume STK)
            self._tws_lookup(text)

    def _add_symbol(self, sym: str) -> None:
        """Add a symbol to the chip bar and activate it."""
        if not sym:
            return
        sym = sym.upper()
        log.info("OrderEntry: adding symbol '%s'", sym)
        if sym not in self._symbols:
            self._symbols.append(sym)
            config_manager.save_order_entry_symbols(self._symbols)
        self._active_symbol = sym
        self._combo.setCurrentText("")
        # Restore full list for next search
        self._refresh_combo_items()
        self._rebuild_symbol_buttons()
        self._rebuild_qty_buttons()
        self._refresh_action_labels()

    # ── Symbol buttons ────────────────────────────────────────────────────────

    _CHIP_COLS = 5        # max buttons per row
    _CHIP_HEIGHT = 28

    def _rebuild_symbol_buttons(self) -> None:
        """Recreate symbol chip grid: STK rows first, then FUT rows (max 2 each)."""
        # Clear grid
        while self._sym_grid.count():
            item = self._sym_grid.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._sym_buttons.clear()

        # Partition symbols into STK and FUT buckets (preserve order)
        stk_syms: list[str] = []
        fut_syms: list[str] = []
        for sym in self._symbols:
            sec = _sec_type_for(sym)
            if sec == "FUT":
                fut_syms.append(sym)
            else:
                stk_syms.append(sym)  # STK, OPT, unknown all go here

        # Cap at 2 rows each (12 symbols)
        max_per_group = self._CHIP_COLS * 2
        stk_syms = stk_syms[:max_per_group]
        fut_syms = fut_syms[:max_per_group]

        row = 0
        # ── STK rows ──
        for i, sym in enumerate(stk_syms):
            r = row + i // self._CHIP_COLS
            c = i % self._CHIP_COLS
            btn = self._make_chip(sym)
            self._sym_grid.addWidget(btn, r, c)
        if stk_syms:
            row += (len(stk_syms) - 1) // self._CHIP_COLS + 1

        # ── FUT rows ──
        for i, sym in enumerate(fut_syms):
            r = row + i // self._CHIP_COLS
            c = i % self._CHIP_COLS
            btn = self._make_chip(sym)
            self._sym_grid.addWidget(btn, r, c)

        self._update_symbol_display()

    def _make_chip(self, sym: str) -> QPushButton:
        """Create a symbol chip button."""
        btn = QPushButton(sym)
        btn.setFixedHeight(self._CHIP_HEIGHT)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _apply_chip_style(btn, sym == self._active_symbol)
        btn.clicked.connect(lambda checked, s=sym: self._on_symbol_clicked(s))
        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(
            lambda pos, s=sym: self._remove_symbol(s)
        )
        self._sym_buttons[sym] = btn
        return btn

    def _on_symbol_clicked(self, sym: str) -> None:
        self._active_symbol = sym
        for s, btn in self._sym_buttons.items():
            _apply_chip_style(btn, s == sym)
        self._update_symbol_display()
        self._rebuild_qty_buttons()
        self._refresh_action_labels()

    def _update_symbol_display(self) -> None:
        if self._active_symbol:
            self._lbl_symbol.setText(self._active_symbol)
            self._lbl_price.setText("")  # clear until live tick arrives
            self._subscribe_price(self._active_symbol)
        else:
            self._lbl_symbol.setText("")
            self._lbl_price.setText("")
            self._cancel_price_subscription()

    def _remove_symbol(self, sym: str) -> None:
        """Right-click removes a symbol button."""
        if sym in self._symbols:
            self._symbols.remove(sym)
            config_manager.save_order_entry_symbols(self._symbols)
        if self._active_symbol == sym:
            self._active_symbol = self._symbols[0] if self._symbols else None
        self._rebuild_symbol_buttons()
        self._rebuild_qty_buttons()
        self._refresh_action_labels()

    # ── Quantity buttons ──────────────────────────────────────────────────────

    def _rebuild_qty_buttons(self) -> None:
        """Swap qty preset buttons based on FUT vs STK."""
        while self._qty_layout.count():
            item = self._qty_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        sec = _sec_type_for(self._active_symbol or "")
        if sec == "STK":
            presets = _STK_QTYS
            default = _STK_DEFAULT
        else:
            presets = _FUT_QTYS
            default = _FUT_DEFAULT

        self._active_qty = default
        self._qty_btn_group: list[QPushButton] = []

        for q in presets:
            btn = QPushButton(str(q))
            btn.setFixedHeight(28)
            btn.setMinimumWidth(36)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            _apply_chip_style(btn, q == default)
            btn.clicked.connect(lambda checked, v=q: self._on_qty_preset(v))
            self._qty_btn_group.append(btn)
            self._qty_layout.addWidget(btn)

        self._custom_qty = QLineEdit()
        self._custom_qty.setPlaceholderText("Qty")
        self._custom_qty.setFixedHeight(28)
        self._custom_qty.setMaximumWidth(60)
        self._custom_qty.setStyleSheet(
            "QLineEdit{background:#3a3a4a;color:#ccc;border:none;"
            "border-radius:6px;padding:4px 8px;font-weight:600;font-size:12px}"
            "QLineEdit:focus{background:#4a4a5a;color:white}"
        )
        self._custom_qty.textChanged.connect(self._on_custom_qty)
        self._qty_layout.addWidget(self._custom_qty)

    def _on_qty_preset(self, qty: int) -> None:
        self._active_qty = qty
        self._custom_qty.blockSignals(True)
        self._custom_qty.clear()
        self._custom_qty.blockSignals(False)
        for btn in self._qty_btn_group:
            _apply_chip_style(btn, int(btn.text()) == qty)
        self._refresh_action_labels()

    def _on_custom_qty(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        try:
            qty = int(text)
            if qty > 0:
                self._active_qty = qty
                for btn in self._qty_btn_group:
                    _apply_chip_style(btn, False)
                self._refresh_action_labels()
        except ValueError:
            pass

    # ── Action label refresh ──────────────────────────────────────────────────

    def _refresh_action_labels(self) -> None:
        q = self._active_qty
        self._btn_buy_mkt.setText(f"Buy {q} @MKT")
        self._btn_sell_mkt.setText(f"Sell {q} @MKT")
        self._btn_buy_bid.setText(f"Buy {q} @Bid")
        self._btn_sell_ask.setText(f"Sell {q} @Ask")

    # ── Order signal ──────────────────────────────────────────────────────────

    def _emit_order(self, action: str, order_type: str) -> None:
        if not self._active_symbol:
            return
        payload = {
            "action": action,
            "type": order_type,
            "symbol": self._active_symbol,
            "qty": self._active_qty,
        }
        log.info("Order requested: %s", payload)
        self.order_requested.emit(payload)

    # ── TWS fallback lookup ─────────────────────────────────────────────────────

    def _find_workers_by_priority(self) -> list:
        """Return connected workers sorted by config priority (1 = highest)."""
        from src.core import config_manager
        ordered_labels = config_manager.get_labels_by_priority()
        workers = []
        for label in ordered_labels:
            w = self._active_threads.get(label)
            if w is not None and w.isRunning() and w.is_ib_connected:
                workers.append(w)
        # Include any connected workers whose label wasn't in the config
        for label, w in self._active_threads.items():
            if w not in workers and w.isRunning() and w.is_ib_connected:
                workers.append(w)
        return workers

    def _tws_lookup(self, symbol: str) -> None:
        """Query TWS to qualify a stock symbol not found locally."""
        workers = self._find_workers_by_priority()
        if not workers:
            log.warning("OrderEntry: no TWS connection for lookup of '%s'", symbol)
            self._combo.lineEdit().setPlaceholderText("No TWS connection!")
            from PySide6.QtCore import QTimer
            QTimer.singleShot(2000, lambda: self._combo.lineEdit().setPlaceholderText(
                "Add symbol (look up).."))
            return

        log.info("OrderEntry: looking up '%s' via TWS", symbol)
        self._combo.lineEdit().setPlaceholderText(f"⏳ Looking up {symbol}…")
        self._pending_lookup = symbol
        self._pending_lookup_workers = workers[1:]  # remaining for retry

        self._start_tws_lookup_with_worker(workers[0], symbol)

    def _start_tws_lookup_with_worker(self, worker, symbol: str) -> None:
        """Wire signals to *worker* and kick off the browse request."""
        # Disconnect previous signals if any
        if self._connected_worker is not None:
            try:
                self._connected_worker.browse_results.disconnect(self._on_tws_results)
            except (RuntimeError, TypeError):
                pass
            try:
                self._connected_worker.browse_failed.disconnect(self._on_tws_failed)
            except (RuntimeError, TypeError):
                pass

        self._connected_worker = worker
        worker.browse_results.connect(self._on_tws_results)
        worker.browse_failed.connect(self._on_tws_failed)
        log.info("OrderEntry: trying worker %s for lookup of '%s'", worker._label, symbol)
        worker.request_browse({
            "sec_type": "STK",
            "symbol": symbol,
            "exchange": "SMART",
            "currency": "USD",
        })

    def _on_tws_results(self, results: list[dict]) -> None:
        """TWS returned contract details – save and add symbol."""
        self._combo.lineEdit().setPlaceholderText("Add symbol (look up)..")
        # Disconnect signals
        if self._connected_worker is not None:
            try:
                self._connected_worker.browse_results.disconnect(self._on_tws_results)
            except (RuntimeError, TypeError):
                pass
            try:
                self._connected_worker.browse_failed.disconnect(self._on_tws_failed)
            except (RuntimeError, TypeError):
                pass
            self._connected_worker = None

        if not results:
            log.warning("OrderEntry: TWS returned no results for '%s'",
                        getattr(self, '_pending_lookup', '?'))
            return

        # Save the contract(s) to app_config.json
        existing = config_manager.load_contracts()
        existing_ids = {c.get("conid") for c in existing}
        added = 0
        sym_to_add = ""
        for r in results:
            if r.get("conid") and r["conid"] not in existing_ids:
                existing.append(r)
                existing_ids.add(r["conid"])
                added += 1
            if not sym_to_add:
                sym_to_add = r.get("symbol", "")

        if added:
            config_manager.save_contracts(existing)
            log.info("OrderEntry: saved %d contract(s) from TWS lookup", added)

        # Add the symbol chip
        if sym_to_add:
            self._add_symbol(sym_to_add)

    def _on_tws_failed(self, msg: str) -> None:
        """TWS lookup failed — retry next priority worker or give up."""
        # Disconnect signals from current worker
        if self._connected_worker is not None:
            try:
                self._connected_worker.browse_results.disconnect(self._on_tws_results)
            except (RuntimeError, TypeError):
                pass
            try:
                self._connected_worker.browse_failed.disconnect(self._on_tws_failed)
            except (RuntimeError, TypeError):
                pass

        # Try next priority worker if available
        pending = getattr(self, "_pending_lookup_workers", [])
        pending_sym = getattr(self, "_pending_lookup", "")
        if pending and pending_sym:
            next_worker = pending.pop(0)
            log.info("OrderEntry: lookup failed on %s (%s), retrying with %s",
                     getattr(self._connected_worker, '_label', '?'), msg,
                     next_worker._label)
            self._start_tws_lookup_with_worker(next_worker, pending_sym)
            return

        # All workers exhausted
        self._connected_worker = None
        self._combo.lineEdit().setPlaceholderText("Add symbol (look up)..")
        log.warning("OrderEntry: TWS lookup failed (all workers exhausted): %s", msg)

    # ── Public API for live price feed ────────────────────────────────────────

    def set_price(self, price_str: str) -> None:
        """Call from outside to update the current price display."""
        self._lbl_price.setText(price_str)

    def refresh_price_subscription(self) -> None:
        """Re-subscribe to the active symbol's price if the current worker
        is no longer valid (e.g. after disconnect/reconnect)."""
        if not self._active_symbol:
            return
        # Check if current price worker is still alive and connected
        pw = self._price_worker
        if pw is not None and pw.isRunning() and pw.is_ib_connected:
            return  # still good
        log.info("OrderEntry: refreshing stale price subscription for '%s'",
                 self._active_symbol)
        self._subscribe_price(self._active_symbol)

    # ── Live price subscription ───────────────────────────────────────────────

    def _subscribe_price(self, sym: str) -> None:
        """Subscribe to live market data for *sym* via the highest-priority worker."""
        self._cancel_price_subscription()

        # Look up contract details from saved contracts
        contracts = config_manager.load_contracts()
        contract = None
        q = sym.upper()
        for c in contracts:
            if (c.get("symbol", "").upper() == q
                    or c.get("local_symbol", "").upper() == q):
                contract = c
                break
        if contract is None:
            log.warning("OrderEntry: no saved contract for '%s' – cannot subscribe price", sym)
            return

        conid = contract.get("conid")
        if not conid:
            log.warning("OrderEntry: saved contract for '%s' has no conid", sym)
            return

        workers = self._find_workers_by_priority()
        if not workers:
            log.warning("OrderEntry: no connected worker for price subscription of '%s'", sym)
            return

        # Store params and remaining workers for retry on error 10197
        self._price_params = {
            "conid": int(conid),
            "symbol": sym,
            "sec_type": contract.get("sec_type", ""),
            "exchange": contract.get("exchange", "SMART"),
            "currency": contract.get("currency", "USD"),
            "local_symbol": contract.get("local_symbol", ""),
            "expiry": contract.get("expiry", ""),
            "multiplier": contract.get("multiplier", ""),
        }
        self._price_retry_workers = workers[1:]  # remaining workers for fallback
        self._start_price_with_worker(workers[0])

    def _start_price_with_worker(self, worker) -> None:
        """Wire signals to *worker* and start price subscription."""
        # Disconnect previous worker if any
        if self._price_worker is not None:
            try:
                self._price_worker.price_updated.disconnect(self._on_price_update)
            except (RuntimeError, TypeError):
                pass
            try:
                self._price_worker.market_data_failed.disconnect(self._on_market_data_failed)
            except (RuntimeError, TypeError):
                pass
            self._price_worker.cancel_market_data()

        self._price_received = False
        self._price_worker = worker
        worker.price_updated.connect(self._on_price_update)
        worker.market_data_failed.connect(self._on_market_data_failed)
        worker.request_market_data(self._price_params)
        log.info("OrderEntry: subscribed to price for '%s' (conId=%s) via worker %s",
                 self._price_params.get("symbol"), self._price_params.get("conid"),
                 worker._label)

        # Start a 3-second timeout — if no price arrives, try next worker
        if not hasattr(self, "_price_timeout"):
            from PySide6.QtCore import QTimer
            self._price_timeout = QTimer(self)
            self._price_timeout.setSingleShot(True)
            self._price_timeout.timeout.connect(self._on_price_timeout)
        self._price_timeout.start(3000)

    def _cancel_price_subscription(self) -> None:
        """Cancel the current market data subscription."""
        if hasattr(self, "_price_timeout") and self._price_timeout.isActive():
            self._price_timeout.stop()
        if self._price_worker is not None:
            try:
                self._price_worker.price_updated.disconnect(self._on_price_update)
            except (RuntimeError, TypeError):
                pass
            try:
                self._price_worker.market_data_failed.disconnect(self._on_market_data_failed)
            except (RuntimeError, TypeError):
                pass
            self._price_worker.cancel_market_data()
            self._price_worker = None
        self._price_retry_workers = []
        self._price_params = {}

    def _on_price_update(self, sym: str, price: float) -> None:
        """Receive a live price tick from the worker thread."""
        if sym.upper() == (self._active_symbol or "").upper():
            self._price_received = True
            if hasattr(self, "_price_timeout") and self._price_timeout.isActive():
                self._price_timeout.stop()
            self._lbl_price.setText(f"{price:,.2f}")

    def _on_price_timeout(self) -> None:
        """No price arrived within 3 seconds — try the next priority worker."""
        if self._price_received:
            return  # price already arrived, ignore stale timeout
        sym = self._price_params.get("symbol", "")
        log.warning("OrderEntry: no price for '%s' within 3s from worker %s — trying next",
                    sym, self._price_worker._label if self._price_worker else "?")
        self._on_market_data_failed(sym)

    def _on_market_data_failed(self, sym: str) -> None:
        """Market data blocked (e.g. error 10197 or timeout) — try the next priority worker."""
        if sym.upper() != (self._active_symbol or "").upper():
            return  # stale signal from a previous symbol

        retry = getattr(self, "_price_retry_workers", [])
        if retry:
            next_worker = retry.pop(0)
            log.info("OrderEntry: market data failed on %s for '%s', "
                     "retrying with worker %s",
                     self._price_worker._label if self._price_worker else "?",
                     sym, next_worker._label)
            self._start_price_with_worker(next_worker)
        else:
            log.warning("OrderEntry: market data failed for '%s' – "
                        "all workers exhausted", sym)

















