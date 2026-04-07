from __future__ import annotations
from src.core.enums import OrderAction, OrderType
from src.ui.theme import Theme

import logging
import math
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.core import config_manager

log = logging.getLogger(__name__)


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
#  Toggle switch
# ═══════════════════════════════════════════════════════════════════════════════

class _ToggleSwitch(QWidget):
    """Compact iOS-style on/off toggle (custom-painted, no images needed)."""

    toggled = Signal(bool)
    _W, _H = 40, 22

    def __init__(self, on: bool = False, parent=None):
        super().__init__(parent)
        self._on = on
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        self._on = not self._on
        self.update()
        self.toggled.emit(self._on)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._H // 2
        # Track
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#0071e3") if self._on else QColor("#4a4a5a"))
        p.drawRoundedRect(0, 1, self._W, self._H - 2, r, r)
        # Thumb
        thumb_d = self._H - 6
        thumb_x = self._W - thumb_d - 4 if self._on else 4
        p.setBrush(QColor("white"))
        p.drawEllipse(thumb_x, 3, thumb_d, thumb_d)
        p.end()

    @property
    def is_on(self) -> bool:
        return self._on

    def set_on(self, value: bool) -> None:
        if self._on != value:
            self._on = value
            self.update()


# ═══════════════════════════════════════════════════════════════════════════════
#  Tier section  (Take Profit / Stop Loss)
# ═══════════════════════════════════════════════════════════════════════════════

class _TierSection(QWidget):
    """Header (label + toggle) + a row of tier chip buttons + custom input field.

    Chip labels come from the ``tiers`` list loaded from preferences.
    The last element in the row is always a free-text input (custom value).
    """

    changed = Signal()

    _CHIP_H = 28
    _SS_SEL = (
        "QPushButton{background:#0071e3;color:white;border:none;"
        "border-radius:6px;padding:4px 12px;font-weight:bold;font-size:12px}"
        "QPushButton:hover{background:#0077ed}"
    )
    _SS_NORM = (
        "QPushButton{background:#3a3a4a;color:#ccc;border:none;"
        "border-radius:6px;padding:4px 12px;font-weight:600;font-size:12px}"
        "QPushButton:hover{background:#4a4a5a}"
    )
    _SS_DIS = (
        "QPushButton{background:#252535;color:#444;border:none;"
        "border-radius:6px;padding:4px 12px;font-size:12px}"
    )
    _SS_INPUT = (
        "QLineEdit{background:#3a3a4a;color:#ccc;border:none;"
        "border-radius:4px;padding:2px 6px;font-size:11px}"
        "QLineEdit:focus{background:#4a4a5a;color:white}"
        "QLineEdit:disabled{background:#252535;color:#444;border:none}"
    )

    def __init__(self, title: str, tiers: list[dict],
                 enabled: bool = False, parent=None):
        super().__init__(parent)
        self._tiers: list[dict] = tiers
        self._selected_idx: int | None = None
        self._chip_buttons: list[QPushButton] = []
        self._input: QLineEdit | None = None
        self._toggle: _ToggleSwitch | None = None
        self._state_lbl: QLabel | None = None
        self._chips_layout: QHBoxLayout | None = None
        self._build_ui(title, enabled)

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self, title: str, enabled: bool) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 4)
        root.setSpacing(4)

        # Header row
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        hdr.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(title)
        lbl.setStyleSheet(
            f"color: {Theme.TEXT}; font-size: 11px; font-weight: bold; border: none;")
        hdr.addWidget(lbl)
        hdr.addStretch()
        self._state_lbl = QLabel("On" if enabled else "Off")
        self._state_lbl.setStyleSheet(
            f"color: {Theme.TEXT_DIM}; font-size: 11px; border: none;")
        hdr.addWidget(self._state_lbl)
        self._toggle = _ToggleSwitch(enabled)
        self._toggle.toggled.connect(self._on_toggled)
        hdr.addWidget(self._toggle)
        root.addLayout(hdr)

        # Chips container (rebuilt on reload_tiers)
        chips_w = QWidget()
        chips_w.setStyleSheet("border: none;")
        self._chips_layout = QHBoxLayout(chips_w)
        self._chips_layout.setContentsMargins(0, 0, 0, 0)
        self._chips_layout.setSpacing(4)
        root.addWidget(chips_w)

        self._build_chips(enabled)

    def _build_chips(self, enabled: bool) -> None:
        """(Re)populate the chips row from self._tiers."""
        lay = self._chips_layout
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._chip_buttons.clear()
        self._selected_idx = None

        for i, tier in enumerate(self._tiers):
            label = tier.get("label", f"Tier {i + 1}")
            btn = QPushButton(label)
            btn.setFixedHeight(self._CHIP_H)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda chk, idx=i: self._on_chip_clicked(idx))
            self._chip_buttons.append(btn)
            lay.addWidget(btn)

        # Custom input (always last, always enabled, expands with the rest)
        self._input = QLineEdit()
        self._input.setFixedHeight(self._CHIP_H)
        self._input.setMinimumWidth(48)
        self._input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._input.setStyleSheet(self._SS_INPUT)
        self._input.textChanged.connect(self._on_input_changed)
        lay.addWidget(self._input)
        # No addStretch() — chips fill all available width

        self._update_chip_styles()

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_toggled(self, on: bool) -> None:
        self._state_lbl.setText("On" if on else "Off")
        if not on:
            # Unselect all chips and clear input when toggled OFF
            self._selected_idx = None
            if self._input:
                self._input.blockSignals(True)
                self._input.clear()
                self._input.blockSignals(False)
        self._update_chip_styles()
        self.changed.emit()

    def _on_chip_clicked(self, idx: int) -> None:
        # Auto-switch toggle ON if currently OFF
        if self._toggle and not self._toggle.is_on:
            self._toggle.set_on(True)
            self._state_lbl.setText("On")
        self._selected_idx = idx
        if self._input:
            self._input.blockSignals(True)
            self._input.clear()
            self._input.blockSignals(False)
        self._update_chip_styles()
        self.changed.emit()

    def _on_input_changed(self, text: str) -> None:
        stripped = text.strip()
        if stripped:
            # Auto-switch toggle ON if value > 0 and toggle is currently OFF
            try:
                if float(stripped) > 0 and self._toggle and not self._toggle.is_on:
                    self._toggle.set_on(True)
                    self._state_lbl.setText("On")
            except ValueError:
                pass
            self._selected_idx = None
            self._update_chip_styles()
        self.changed.emit()

    def _update_chip_styles(self) -> None:
        # Delegate to the same helper used by symbol chips for identical styling.
        for i, btn in enumerate(self._chip_buttons):
            _apply_chip_style(btn, i == self._selected_idx)

    # ── Public API ────────────────────────────────────────────────────────────

    def reload_tiers(self, tiers: list[dict]) -> None:
        """Reload chip buttons when preferences change."""
        self._tiers = tiers
        self._build_chips(enabled=True)  # chips are always interactive

    @property
    def is_enabled(self) -> bool:
        return self._toggle.is_on if self._toggle else False

    def get_value(self) -> dict | None:
        """Return selected tier value dict, or None if disabled/nothing chosen."""
        if not self.is_enabled:
            return None
        custom = self._input.text().strip() if self._input else ""
        if custom:
            try:
                return {"value": float(custom), "unit": "ticks", "label": "Custom"}
            except ValueError:
                return None
        if self._selected_idx is not None and self._selected_idx < len(self._tiers):
            t = self._tiers[self._selected_idx]
            return {
                "value": t.get("value", 0),
                "unit":  t.get("unit", "ticks"),
                "label": t.get("label", ""),
            }
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  OrderEntryPanel
# ═══════════════════════════════════════════════════════════════════════════════
_COMBO_SS = f"""
    QComboBox {{
        background: {Theme.BG_INPUT}; color: {Theme.TEXT};
        border: 1px solid {Theme.BORDER}; border-radius: 4px;
        padding: 3px 8px; font-size: 11px;
    }}
    QComboBox:hover {{ border-color: {Theme.ACCENT}; }}
    QComboBox:focus {{ border-color: {Theme.ACCENT}; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox::down-arrow {{ image: none; border: none; width: 0; }}
    QComboBox QAbstractItemView {{
        background: {Theme.BG_INPUT}; color: {Theme.TEXT};
        border: 1px solid {Theme.BORDER}; border-radius: 4px;
        selection-background-color: #3a3f50;
        selection-color: {Theme.TEXT};
        padding: 2px; outline: none;
    }}
"""


class OrderEntryPanel(QWidget):
    """Full Order Entry widget – drop-in replacement for _titled_panel."""

    # Emitted when user clicks an action button:
    #   {"action": OrderAction.BUY/OrderAction.SELL, "type": "MKT"/"BID"/"ASK",
    #    "symbol": str, "qty": int}
    order_requested = Signal(dict)

    # Emitted when user clicks the Cancel button in position management:
    #   symbol (str) – cancel all open orders for this symbol.
    cancel_all_requested = Signal(str)

    # Emitted when user clicks the Flatten button:
    #   symbol (str) – cancel all open orders then close position for this symbol.
    flatten_requested = Signal(str)

    # Emitted when user clicks the Reverse button:
    #   symbol (str) – cancel all open orders then flip position for this symbol.
    reverse_requested = Signal(str)

    # Emitted when user clicks a partial-close button (25% / 50% / 75%):
    #   (symbol: str, pct: int) – close *pct* percent of the position.
    close_pct_requested = Signal(str, int)

    def __init__(self, active_threads: dict | None = None, parent=None):
        super().__init__(parent)
        self._active_threads: dict = active_threads if active_threads is not None else {}
        self._connected_worker = None  # track worker for TWS lookup
        self._bg_worker = None         # track worker for background price streams
        # sym → (last, bid, ask)
        self._price_cache: dict[str, tuple[float, float, float]] = {}
        self._last_price: float = 0.0  # latest live price for limit-order calculation
        self._last_bid: float = 0.0    # latest live bid price
        self._last_ask: float = 0.0    # latest live ask price
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
                background: {Theme.BG_PANEL};
                border: 1px solid {Theme.BORDER};
                border-radius: 4px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)

        # ── Title ─────────────────────────────────────────────────────────────
        title = QLabel("Order Entry")
        title.setFont(_TITLE_FONT)
        title.setStyleSheet(f"color: {Theme.TEXT}; border: none;")
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
            completer.setCompletionMode(
                QCompleter.CompletionMode.PopupCompletion)
            completer.popup().setStyleSheet(f"""
                QAbstractItemView {{
                    background: {Theme.BG_INPUT}; color: {Theme.TEXT};
                    border: 1px solid {Theme.BORDER}; border-radius: 4px;
                    selection-background-color: #3a3f50;
                    selection-color: {Theme.TEXT};
                    padding: 2px; outline: none;
                }}
            """)

        self._lbl_symbol = QLabel("")
        self._lbl_symbol.setFont(_SYMBOL_FONT)
        self._lbl_symbol.setStyleSheet(f"color: {Theme.TEXT}; border: none;")

        self._lbl_price = QLabel("")
        self._lbl_price.setFont(_PRICE_FONT)
        self._lbl_price.setStyleSheet(f"color: {Theme.ACCENT}; border: none;")

        self._lbl_bid_ask = QLabel("")
        self._lbl_bid_ask.setFont(QFont("Menlo, Consolas, monospace", 10))
        self._lbl_bid_ask.setStyleSheet(
            f"color: {Theme.TEXT_DIM}; border: none;")
        self._lbl_bid_ask.setAlignment(Qt.AlignmentFlag.AlignRight)

        # Right side: symbol + price row, then bid/ask below
        sym_price_row = QHBoxLayout()
        sym_price_row.setSpacing(6)
        sym_price_row.addStretch()
        sym_price_row.addWidget(self._lbl_symbol)
        sym_price_row.addWidget(self._lbl_price)

        right_side = QVBoxLayout()
        right_side.setSpacing(2)
        right_side.addLayout(sym_price_row)
        right_side.addWidget(
            self._lbl_bid_ask, alignment=Qt.AlignmentFlag.AlignRight)

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

        # ── Action buttons (4×2) ─────────────────────────────────────────────
        action_grid = QGridLayout()
        action_grid.setSpacing(4)
        action_grid.setContentsMargins(0, 0, 0, 0)
        self._btn_buy_mkt = _make_btn("Buy @MKT", Theme.CLR_BUY, 36)
        self._btn_sell_mkt = _make_btn("Sell @MKT", Theme.CLR_SELL, 36)
        self._btn_buy_mid = _make_btn("Buy @Mid", Theme.CLR_BUY, 36)
        self._btn_sell_mid = _make_btn("Sell @Mid", Theme.CLR_SELL, 36)
        self._btn_buy_bid = _make_btn("Buy @Bid", Theme.CLR_BUY, 36)
        self._btn_sell_ask = _make_btn("Sell @Ask", Theme.CLR_SELL, 36)
        self._btn_buy_bid_lmt = _make_btn("Buy @Bid Limit", Theme.CLR_BUY, 36)
        self._btn_sell_ask_lmt = _make_btn(
            "Sell @Ask Limit", Theme.CLR_SELL, 36)
        action_grid.addWidget(self._btn_buy_mkt, 0, 0)
        action_grid.addWidget(self._btn_sell_mkt, 0, 1)
        action_grid.addWidget(self._btn_buy_mid, 1, 0)
        action_grid.addWidget(self._btn_sell_mid, 1, 1)
        action_grid.addWidget(self._btn_buy_bid, 2, 0)
        action_grid.addWidget(self._btn_sell_ask, 2, 1)
        action_grid.addWidget(self._btn_buy_bid_lmt, 3, 0)
        action_grid.addWidget(self._btn_sell_ask_lmt, 3, 1)

        self._btn_buy_mkt.clicked.connect(
            lambda: self._emit_order(OrderAction.BUY, "MKT"))
        self._btn_sell_mkt.clicked.connect(
            lambda: self._emit_order(OrderAction.SELL, "MKT"))
        self._btn_buy_mid.clicked.connect(
            lambda: self._emit_order(OrderAction.BUY, "MID"))
        self._btn_sell_mid.clicked.connect(
            lambda: self._emit_order(OrderAction.SELL, "MID"))
        self._btn_buy_bid.clicked.connect(
            lambda: self._emit_order(OrderAction.BUY, "BID"))
        self._btn_sell_ask.clicked.connect(
            lambda: self._emit_order(OrderAction.SELL, "ASK"))
        self._btn_buy_bid_lmt.clicked.connect(
            lambda: self._emit_bid_ask_lmt_order(OrderAction.BUY))
        self._btn_sell_ask_lmt.clicked.connect(
            lambda: self._emit_bid_ask_lmt_order(OrderAction.SELL))

        action_w = QWidget()
        action_w.setStyleSheet("border: none;")
        action_w.setContentsMargins(0, 0, 0, 0)
        action_w.setLayout(action_grid)
        root.addWidget(action_w)

        # ── Position management (3×2) ────────────────────────────────────────
        pm_grid = QGridLayout()
        pm_grid.setSpacing(4)
        pm_grid.setContentsMargins(0, 0, 0, 0)


        self._btn_close25 = _make_btn("25%", Theme.CLR_CLOSE, 30)
        self._btn_close50 = _make_btn("50%", Theme.CLR_CLOSE, 30)
        self._btn_close75 = _make_btn("75%", Theme.CLR_CLOSE, 30)
        pm_grid.addWidget(self._btn_close25, 1, 0)
        pm_grid.addWidget(self._btn_close50, 1, 1)
        pm_grid.addWidget(self._btn_close75, 2, 1)
        self._btn_close25.clicked.connect(lambda: self._on_close_pct(25))
        self._btn_close50.clicked.connect(lambda: self._on_close_pct(50))
        self._btn_close75.clicked.connect(lambda: self._on_close_pct(75))

        self._btn_flatten = _make_btn("Flatten", Theme.CLR_FLATTEN, 30)
        pm_grid.addWidget(self._btn_flatten, 0, 0)
        self._btn_flatten.clicked.connect(self._on_flatten)

        self._btn_reverse = _make_btn("Reverse", Theme.CLR_REVERSE, 30)
        pm_grid.addWidget(self._btn_reverse, 2, 0)
        self._btn_reverse.clicked.connect(self._on_reverse)

        self._btn_cancel = _make_btn("Cancel", Theme.CLR_CANCEL, 30)
        pm_grid.addWidget(self._btn_cancel, 0, 1)
        self._btn_cancel.clicked.connect(self._on_cancel_all)

        pm_w = QWidget()
        pm_w.setStyleSheet("border: none;")
        pm_w.setContentsMargins(0, 0, 0, 0)
        pm_w.setLayout(pm_grid)
        root.addWidget(pm_w)

        root.addSpacing(8)

        # ── Take Profit ───────────────────────────────────────────────────────
        pt_tiers, sl_tiers = self._load_tiers_for_active()
        self._pt_section = _TierSection("Take profit", pt_tiers, enabled=False)
        root.addWidget(self._pt_section)

        # ── Stop Loss ─────────────────────────────────────────────────────────
        self._sl_section = _TierSection("Stop loss", sl_tiers, enabled=False)
        root.addWidget(self._sl_section)

        root.addStretch(1)

    # ── Combo / symbol search ─────────────────────────────────────────────────

    def _on_cancel_all(self) -> None:
        """Cancel button clicked → emit cancel_all_requested for the active symbol."""
        sym = self._active_symbol
        if sym:
            self.cancel_all_requested.emit(sym)
        else:
            log.warning("OrderEntryPanel: Cancel clicked but no active symbol")

    def _on_flatten(self) -> None:
        """Flatten button clicked → emit flatten_requested for the active symbol."""
        sym = self._active_symbol
        if sym:
            self.flatten_requested.emit(sym)
        else:
            log.warning(
                "OrderEntryPanel: Flatten clicked but no active symbol")

    def _on_reverse(self) -> None:
        """Reverse button clicked → emit reverse_requested for the active symbol."""
        sym = self._active_symbol
        if sym:
            self.reverse_requested.emit(sym)
        else:
            log.warning(
                "OrderEntryPanel: Reverse clicked but no active symbol")

    def _on_close_pct(self, pct: int) -> None:
        """Partial close button clicked → emit close_pct_requested."""
        sym = self._active_symbol
        if sym:
            self.close_pct_requested.emit(sym, pct)
        else:
            log.warning(
                "OrderEntryPanel: Close %d%% clicked but no active symbol", pct)

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
            # Subscribe new symbol to background stream
            if (self._bg_worker is not None
                    and self._bg_worker.isRunning()
                    and self._bg_worker.is_ib_connected):
                params = self._build_bg_params_for_symbol(sym)
                if params:
                    self._bg_worker.subscribe_background_symbols([params])
        self._active_symbol = sym
        self._combo.setCurrentText("")
        # Restore full list for next search
        self._refresh_combo_items()
        self._rebuild_symbol_buttons()
        self._rebuild_qty_buttons()
        self._refresh_action_labels()
        self._reload_tier_sections()

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
        self._reload_tier_sections()

    def _update_symbol_display(self) -> None:
        if self._active_symbol:
            self._lbl_symbol.setText(self._active_symbol)

            # Instant read from background cache
            cached = self._price_cache.get(self._active_symbol.upper())
            if cached:
                last, bid, ask = cached
                self._last_price = last
                self._last_bid = bid
                self._last_ask = ask
                if last > 0:
                    self._lbl_price.setText(f"{last:,.2f}")
                else:
                    self._lbl_price.setText("")
                self._update_bid_ask_display(bid, ask)
            else:
                self._lbl_price.setText("")
                self._lbl_bid_ask.setText("")
                self._last_price = 0.0
                self._last_bid = 0.0
                self._last_ask = 0.0

            # Ensure background subscriptions are active
            self._ensure_bg_subscriptions()
        else:
            self._lbl_symbol.setText("")
            self._lbl_price.setText("")
            self._lbl_bid_ask.setText("")
            self._last_price = 0.0
            self._last_bid = 0.0
            self._last_ask = 0.0

    def _remove_symbol(self, sym: str) -> None:
        """Right-click removes a symbol button."""
        if sym in self._symbols:
            self._symbols.remove(sym)
            config_manager.save_order_entry_symbols(self._symbols)
        # Unsubscribe from background stream
        if self._bg_worker is not None:
            self._bg_worker.unsubscribe_background_symbol(sym)
        self._price_cache.pop(sym.upper(), None)
        if self._active_symbol == sym:
            self._active_symbol = self._symbols[0] if self._symbols else None
        self._rebuild_symbol_buttons()
        self._rebuild_qty_buttons()
        self._refresh_action_labels()
        self._reload_tier_sections()

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
        self._btn_buy_mid.setText(f"Buy {q} @Mid")
        self._btn_sell_mid.setText(f"Sell {q} @Mid")
        self._btn_buy_bid.setText(f"Buy {q} @Bid")
        self._btn_sell_ask.setText(f"Sell {q} @Ask")
        self._btn_buy_bid_lmt.setText(f"Buy {q} @Bid Limit")
        self._btn_sell_ask_lmt.setText(f"Sell {q} @Ask Limit")

    # ── Tier helpers ──────────────────────────────────────────────────────────

    _TIER_FALLBACK = [
        {"label": "Tier 1", "value": 20,  "unit": "ticks"},
        {"label": "Tier 2", "value": 40,  "unit": "ticks"},
        {"label": "Tier 3", "value": 80,  "unit": "ticks"},
        {"label": "Tier 4", "value": 160, "unit": "ticks"},
    ]

    def _load_tiers_for_active(self) -> tuple[list[dict], list[dict]]:
        """Return (profit_take_tiers, stop_loss_tiers) for the active symbol's asset class."""
        sec = _sec_type_for(self._active_symbol or "")
        key = "trading_futures" if sec == "FUT" else "trading_stocks"
        prefs = config_manager.load_preferences()
        section = prefs.get(key, {})
        pt = section.get("profit_take", self._TIER_FALLBACK)
        sl = section.get("stop_loss",   self._TIER_FALLBACK)
        return pt, sl

    def _reload_tier_sections(self) -> None:
        """Reload PT/SL tier chips when the active symbol or preferences change."""
        if not hasattr(self, "_pt_section"):
            return
        pt_tiers, sl_tiers = self._load_tiers_for_active()
        self._pt_section.reload_tiers(pt_tiers)
        self._sl_section.reload_tiers(sl_tiers)

    # ── Order signal ──────────────────────────────────────────────────────────

    def _emit_order(self, action: str, order_type: str) -> None:
        if not self._active_symbol:
            return
        sec = _sec_type_for(self._active_symbol)
        payload = {
            "action": action,
            "type": order_type,
            "symbol": self._active_symbol,
            "qty": self._active_qty,
            "sec_type": sec,
            "last_price": self._last_price,
            "profit_take": self._pt_section.get_value() if hasattr(self, "_pt_section") else None,
            "stop_loss":   self._sl_section.get_value() if hasattr(self, "_sl_section") else None,
        }
        log.info("Order requested: %s", payload)
        self.order_requested.emit(payload)

    def _emit_bid_ask_lmt_order(self, action: str) -> None:
        """Emit a BID_LMT or ASK_LMT order using the live bid/ask as the limit price.

        BUY  → uses ``self._last_bid`` (buy at the current bid)
        SELL → uses ``self._last_ask`` (sell at the current ask)

        Falls back to ``self._last_price`` when bid/ask are unavailable
        (e.g. overnight hours where SMART doesn't stream quotes).
        """
        if not self._active_symbol:
            return

        if action == OrderAction.BUY:
            price = self._last_bid
            label = "bid"
        else:
            price = self._last_ask
            label = "ask"

        # Fall back to last traded / displayed price when bid/ask are
        # not available (common during overnight sessions).
        if math.isnan(price) or price <= 0:
            price = self._last_price
            if not math.isnan(price) and price > 0:
                log.info("OrderEntry: %s price unavailable, falling back to last price %.4f",
                         label, price)

        # ── Safety check ──────────────────────────────────────────────────
        if math.isnan(price) or price <= 0:
            QMessageBox.warning(
                self,
                "Invalid Price",
                f"Cannot place order: live {label} price is unavailable or invalid.\n"
                f"Please wait for market data.",
            )
            return

        sec = _sec_type_for(self._active_symbol)
        order_type = "BID_LMT" if action == OrderAction.BUY else "ASK_LMT"
        payload = {
            "action": action,
            "type": order_type,
            "symbol": self._active_symbol,
            "qty": self._active_qty,
            "sec_type": sec,
            "last_price": self._last_price,
            "limit_price": price,
        }
        log.info("Order requested (%s): %s @ %.4f",
                 order_type, self._active_symbol, price)
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
            log.warning(
                "OrderEntry: no TWS connection for lookup of '%s'", symbol)
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
                self._connected_worker.browse_results.disconnect(
                    self._on_tws_results)
            except (RuntimeError, TypeError):
                pass
            try:
                self._connected_worker.browse_failed.disconnect(
                    self._on_tws_failed)
            except (RuntimeError, TypeError):
                pass

        self._connected_worker = worker
        worker.browse_results.connect(self._on_tws_results)
        worker.browse_failed.connect(self._on_tws_failed)
        log.info("OrderEntry: trying worker %s for lookup of '%s'",
                 worker._label, symbol)
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
                self._connected_worker.browse_results.disconnect(
                    self._on_tws_results)
            except (RuntimeError, TypeError):
                pass
            try:
                self._connected_worker.browse_failed.disconnect(
                    self._on_tws_failed)
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
                self._connected_worker.browse_results.disconnect(
                    self._on_tws_results)
            except (RuntimeError, TypeError):
                pass
            try:
                self._connected_worker.browse_failed.disconnect(
                    self._on_tws_failed)
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
        log.warning(
            "OrderEntry: TWS lookup failed (all workers exhausted): %s", msg)

    # ── Public API for live price feed ────────────────────────────────────────

    def set_price(self, price_str: str) -> None:
        """Call from outside to update the current price display."""
        self._lbl_price.setText(price_str)

    def refresh_price_subscription(self) -> None:
        """Re-subscribe background streams if the current worker is stale.

        Called from FlashTradeView.showEvent and _wire_all_workers to ensure
        background market data is always flowing.
        """
        if not self._symbols:
            return
        pw = self._bg_worker
        if pw is not None and pw.isRunning() and pw.is_ib_connected:
            # Worker still valid — re-submit in case it reconnected and
            # lost subscriptions.  The worker skips already-subscribed symbols.
            params = self._build_bg_params()
            if params:
                pw.subscribe_background_symbols(params)
            return
        # Worker is gone or disconnected — find a new one
        log.info("OrderEntry: refreshing background price subscriptions")
        if self._bg_worker is not None:
            try:
                self._bg_worker.bg_price_updated.disconnect(
                    self._on_bg_price_update)
            except (RuntimeError, TypeError):
                pass
            self._bg_worker = None
        self._subscribe_all_background()

    # ── Background market data cache ──────────────────────────────────────────

    def _subscribe_all_background(self) -> None:
        """Subscribe to background market data for all symbol chips via the
        highest-priority connected worker."""
        workers = self._find_workers_by_priority()
        if not workers:
            return
        worker = workers[0]

        # If already subscribed on this worker, skip
        if self._bg_worker is worker:
            return

        # Disconnect from old worker
        if self._bg_worker is not None:
            try:
                self._bg_worker.bg_price_updated.disconnect(
                    self._on_bg_price_update)
            except (RuntimeError, TypeError):
                pass
            self._bg_worker.unsubscribe_all_background()
            self._bg_worker = None

        # Build params for all symbols
        params_list = self._build_bg_params()
        if not params_list:
            return

        # Connect signal and subscribe
        self._bg_worker = worker
        worker.bg_price_updated.connect(self._on_bg_price_update)
        worker.subscribe_background_symbols(params_list)
        log.info("OrderEntry: background streams for %d symbol(s) via worker %s",
                 len(params_list), worker._label)

    def _ensure_bg_subscriptions(self) -> None:
        """Ensure background subscriptions are active on a valid worker."""
        pw = self._bg_worker
        if pw is not None and pw.isRunning() and pw.is_ib_connected:
            return  # still good
        self._subscribe_all_background()

    def _build_bg_params(self) -> list[dict]:
        """Build market data params for all symbol chips."""
        params_list = []
        seen: set[str] = set()
        for sym in self._symbols:
            q = sym.upper()
            if q in seen:
                continue
            p = self._build_bg_params_for_symbol(sym)
            if p:
                params_list.append(p)
                seen.add(q)
        return params_list

    def _build_bg_params_for_symbol(self, sym: str) -> dict | None:
        """Build market data params for a single symbol from saved contracts."""
        contracts = config_manager.load_contracts()
        q = sym.upper()
        for c in contracts:
            if (c.get("symbol", "").upper() == q
                    or c.get("local_symbol", "").upper() == q):
                conid = c.get("conid")
                if conid:
                    return {
                        "conid": int(conid),
                        "symbol": sym,
                        "sec_type": c.get("sec_type", ""),
                        "exchange": c.get("exchange", "SMART"),
                        "currency": c.get("currency", "USD"),
                        "local_symbol": c.get("local_symbol", ""),
                        "expiry": c.get("expiry", ""),
                        "multiplier": c.get("multiplier", ""),
                    }
        return None

    def _on_bg_price_update(self, sym: str, last: float, bid: float, ask: float) -> None:
        """Receive a background price tick — update cache and display if active."""
        self._price_cache[sym.upper()] = (last, bid, ask)

        if sym.upper() == (self._active_symbol or "").upper():
            self._last_price = last
            self._last_bid = bid
            self._last_ask = ask
            if last > 0:
                self._lbl_price.setText(f"{last:,.2f}")
            self._update_bid_ask_display(bid, ask)
            self._update_bid_ask_buttons(bid, ask)

    def _update_bid_ask_display(self, bid: float, ask: float) -> None:
        """Update the bid/ask label beneath the price."""
        if bid > 0 and ask > 0:
            self._lbl_bid_ask.setText(f"B: {bid:,.2f}  ·  A: {ask:,.2f}")
        elif bid > 0:
            self._lbl_bid_ask.setText(f"B: {bid:,.2f}")
        elif ask > 0:
            self._lbl_bid_ask.setText(f"A: {ask:,.2f}")
        else:
            self._lbl_bid_ask.setText("")

    def _update_bid_ask_buttons(self, bid: float, ask: float) -> None:
        """Keep @Bid / @Ask button labels in sync with the current quantity.

        Prices are shown in the bid/ask label beneath the main price display,
        not on the buttons themselves, so the buttons stay concise.
        """
        self._refresh_action_labels()
