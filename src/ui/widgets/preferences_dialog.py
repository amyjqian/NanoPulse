"""
Preferences dialog – Display & Trading settings for NanoPulse.

Auto-closes when focus is lost.  Persists all settings to app_config.json.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, QTimer, QEvent, QPoint, QRect
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QIntValidator, QDoubleValidator
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from src.core import config_manager
from .preferences_columns_page import ColumnsPage

log = logging.getLogger(__name__)

# ── Colour palette (matches main app dark theme) ─────────────────────────────
_BG_DIALOG = "#151922"
_BG_SIDEBAR = "#0E1117"   # keep in sync with Theme.BG_MAIN
_BG_SIDEBAR_HOVER = "#1c2030"
_BG_SIDEBAR_SEL = "#1C2740"
_BG_PANEL = "#151922"
_BG_INPUT = "#1B2130"
_BORDER = "#262E3F"
_TEXT = "#E6EBF2"
_TEXT_DIM = "#A3AFC2"
_ACCENT_BLUE = "#539BF5"

_FONT_TITLE = QFont("Inter, Helvetica Neue, Arial", 14)
_FONT_TITLE.setWeight(QFont.Weight.Bold)
_FONT_SECTION = QFont("Inter, Helvetica Neue, Arial", 12)
_FONT_SECTION.setWeight(QFont.Weight.DemiBold)


# ── Default preference values ─────────────────────────────────────────────────
_DEFAULTS: dict = {
    "display": {
        "theme": "Dark",
    },
    "trading_futures": {
        "quantity": 1,
        "quantity_unit": "Contracts",
        "limit_cap": 0,
        "limit_cap_unit": "ticks",
        "mid_price_behavior": "Normal",
        "buy_bid_behavior": "Patient",
        "sell_ask_behavior": "Patient",
    },
    "trading_stocks": {
        "quantity": 100,
        "quantity_unit": "Shares",
        "limit_cap": 0,
        "limit_cap_unit": "ticks",
        "mid_price_behavior": "Normal",
        "buy_bid_offset": 0,
        "buy_bid_unit": "ticks",
        "sell_ask_offset": 0,
        "sell_ask_unit": "ticks",
    },
    "cash_management": {
        "sweep_symbols": [],
        "sweep_symbol": "",
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Styled sidebar list
# ═══════════════════════════════════════════════════════════════════════════════

def _styled_sidebar() -> QListWidget:
    lw = QListWidget()
    lw.setFixedWidth(140)
    lw.setFrameShape(QFrame.Shape.NoFrame)
    lw.setFont(QFont("Inter, Helvetica Neue, Arial", 12))
    lw.setStyleSheet(f"""
        QListWidget {{
            background: {_BG_SIDEBAR};
            border: none;
            outline: none;
            padding-top: 8px;
        }}
        QListWidget::item {{
            color: {_TEXT_DIM};
            padding: 8px 14px;
            border: none;
            border-radius: 4px;
            margin: 1px 6px;
        }}
        QListWidget::item:hover {{
            background: {_BG_SIDEBAR_HOVER};
            color: {_TEXT};
        }}
        QListWidget::item:selected {{
            background: {_BG_SIDEBAR_SEL};
            color: {_TEXT};
            font-weight: 600;
        }}
    """)
    return lw


# ═══════════════════════════════════════════════════════════════════════════════
#  Styled input helpers
# ═══════════════════════════════════════════════════════════════════════════════

_INPUT_QSS = f"""
    QSpinBox, QComboBox, QLineEdit {{
        background: {_BG_INPUT};
        color: {_TEXT};
        border: 1px solid {_BORDER};
        border-radius: 4px;
        padding: 4px 8px;
        font-size: 12px;
        min-height: 26px;
    }}
    QSpinBox::up-button, QSpinBox::down-button {{
        width: 0; height: 0; border: none;
    }}
    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QComboBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {_TEXT_DIM};
        margin-right: 6px;
    }}
    QComboBox QAbstractItemView {{
        background: {_BG_INPUT};
        color: {_TEXT};
        border: 1px solid {_BORDER};
        selection-background-color: {_BG_SIDEBAR_SEL};
        outline: none;
    }}
"""


def _spin(value: int = 0, minimum: int = 0, maximum: int = 9999) -> QSpinBox:
    sb = QSpinBox()
    sb.setRange(minimum, maximum)
    sb.setValue(value)
    sb.setFixedWidth(80)
    sb.setStyleSheet(_INPUT_QSS)
    return sb


def _text_input(value: str = "", placeholder: str = "") -> QLineEdit:
    """Create a QLineEdit for text input."""
    le = QLineEdit()
    le.setText(str(value))
    if placeholder:
        le.setPlaceholderText(placeholder)
    le.setFixedWidth(80)
    le.setStyleSheet(_INPUT_QSS)
    return le


def _combo(items: list[str], current: str = "") -> QComboBox:
    cb = QComboBox()
    cb.addItems(items)
    if current and current in items:
        cb.setCurrentText(current)
    cb.setFixedWidth(110)
    cb.setStyleSheet(_INPUT_QSS)
    return cb


# ═══════════════════════════════════════════════════════════════════════════════
#  Toggle-button group  (Patient / Normal / Urgent)
# ═══════════════════════════════════════════════════════════════════════════════

class _ToggleGroup(QWidget):
    """Horizontal group of mutually-exclusive toggle buttons."""

    value_changed = Signal(str)

    def __init__(self, options: list[str], selected: str = "", parent=None):
        super().__init__(parent)
        self._buttons: dict[str, QPushButton] = {}
        self._selected = selected or (options[0] if options else "")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        for label in options:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(30)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _checked, l=label: self._on_click(l))
            self._buttons[label] = btn
            lay.addWidget(btn)

        self._apply_styles()

    def _on_click(self, label: str) -> None:
        self._selected = label
        self._apply_styles()
        self.value_changed.emit(label)

    def _apply_styles(self) -> None:
        for lbl, btn in self._buttons.items():
            if lbl == self._selected:
                btn.setChecked(True)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {_ACCENT_BLUE};
                        color: white;
                        border: 1px solid {_ACCENT_BLUE};
                        border-radius: 4px;
                        font-weight: bold;
                        font-size: 12px;
                        padding: 4px 10px;
                        margin: 0 1px;
                    }}
                """)
            else:
                btn.setChecked(False)
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {_BG_INPUT};
                        color: {_TEXT_DIM};
                        border: 1px solid {_BORDER};
                        border-radius: 4px;
                        font-size: 12px;
                        padding: 4px 10px;
                        margin: 0 1px;
                    }}
                    QPushButton:hover {{
                        background: {_BG_SIDEBAR_HOVER};
                        color: {_TEXT};
                    }}
                """)

    def value(self) -> str:
        return self._selected

    def set_value(self, val: str) -> None:
        if val in self._buttons:
            self._selected = val
            self._apply_styles()


# ═══════════════════════════════════════════════════════════════════════════════
#  Theme selector card  (Dark / Light)
# ═══════════════════════════════════════════════════════════════════════════════

class _ThemeCard(QFrame):
    """Clickable card representing a colour theme with a mini chart preview."""

    clicked = Signal(str)

    def __init__(self, label: str, selected: bool = False, parent=None):
        super().__init__(parent)
        self._label = label
        self._selected = selected
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(170, 110)
        self._apply_style()

    def set_selected(self, sel: bool) -> None:
        self._selected = sel
        self._apply_style()

    def _apply_style(self) -> None:
        border_clr = _ACCENT_BLUE if self._selected else _BORDER
        border_w = 2 if self._selected else 1
        bg = _BG_INPUT if self._label == "Dark" else "#e8e8ec"
        self.setStyleSheet(f"""
            QFrame {{
                background: {bg};
                border: {border_w}px solid {border_clr};
                border-radius: 8px;
            }}
        """)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._label)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        is_dark = self._label == "Dark"

        chart_bg = QColor("#1a1e2a") if is_dark else QColor("#f0f0f4")
        chart_fg = QColor("#3FB950") if is_dark else QColor("#539BF5")
        bar_red = QColor("#F85149") if is_dark else QColor("#d32f2f")
        bar_green = QColor("#2EA043") if is_dark else QColor("#388e3c")
        text_clr = QColor(_TEXT) if is_dark else QColor("#333")

        # Chart background
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(chart_bg)
        p.drawRoundedRect(10, 8, w - 20, h - 36, 4, 4)

        # Candlestick-style bars
        bars = [30, 22, 35, 18, 28, 40, 25, 32]
        colors = [bar_red, bar_green, bar_red, bar_green,
                  bar_red, bar_green, bar_red, bar_green]
        for i, bh in enumerate(bars):
            p.setBrush(colors[i])
            p.drawRect(20 + i * 16, h - 38 - bh, 10, bh)

        # Line chart overlay
        pen = QPen(chart_fg, 2)
        p.setPen(pen)
        pts = [(20, 35), (40, 28), (60, 32), (80, 20),
               (100, 25), (120, 15), (140, 22)]
        for i in range(len(pts) - 1):
            p.drawLine(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])

        # Label text
        p.setPen(text_clr)
        p.setFont(QFont("Inter, Helvetica Neue, Arial", 11, QFont.Weight.Medium))
        p.drawText(0, h - 18, w, 18,
                   Qt.AlignmentFlag.AlignHCenter, self._label)
        p.end()


# ═══════════════════════════════════════════════════════════════════════════════
#  Display settings page
# ═══════════════════════════════════════════════════════════════════════════════

class _DisplayPage(QWidget):
    changed = Signal()

    def __init__(self, prefs: dict, parent=None):
        super().__init__(parent)
        self._prefs = prefs
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(16)

        title = QLabel("Display Settings")
        title.setFont(_FONT_TITLE)
        title.setStyleSheet(f"color: {_TEXT}; border: none;")
        root.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_BORDER};")
        sep.setFixedHeight(1)
        root.addWidget(sep)

        root.addSpacing(8)

        theme_lbl = QLabel("Color theme")
        theme_lbl.setFont(_FONT_SECTION)
        theme_lbl.setStyleSheet(f"color: {_TEXT_DIM}; border: none;")
        root.addWidget(theme_lbl)

        cards_lay = QHBoxLayout()
        cards_lay.setSpacing(20)
        cards_lay.setContentsMargins(0, 0, 0, 0)

        current = self._prefs.get("display", {}).get("theme", "Dark")
        self._dark_card = _ThemeCard("Dark", selected=(current == "Dark"))
        self._light_card = _ThemeCard("Light", selected=(current == "Light"))
        self._dark_card.clicked.connect(self._on_theme)
        self._light_card.clicked.connect(self._on_theme)

        cards_lay.addWidget(self._dark_card)
        cards_lay.addWidget(self._light_card)
        cards_lay.addStretch()
        root.addLayout(cards_lay)

        root.addStretch()

    def _on_theme(self, label: str) -> None:
        self._dark_card.set_selected(label == "Dark")
        self._light_card.set_selected(label == "Light")
        self._prefs.setdefault("display", {})["theme"] = label
        self.changed.emit()

    def collect(self) -> dict:
        return {"theme": self._prefs.get("display", {}).get("theme", "Dark")}


# ═══════════════════════════════════════════════════════════════════════════════
#  Cash Management page  (sweep symbol)
# ═══════════════════════════════════════════════════════════════════════════════

class _CashManagementPage(QWidget):
    """Cash-management settings — up to 4 sweep stock symbols.  Each
    configured symbol becomes its own Sweep in / Sweep out action on
    the account right-click menu.  Clicking a field opens the SAME
    standalone symbol-search popup Book Trader's chip bar uses (own
    search box, live IBKR results, filtered to stocks) — a pick fills
    the field; Backspace / Delete clears it."""

    changed = Signal()

    _MAX_SYMBOLS = 4

    def __init__(self, prefs: dict, parent=None, *, workers=None):
        super().__init__(parent)
        self._prefs = prefs
        self._workers = list(workers or [])
        self._popup = None
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(14)

        title = QLabel("Cash Management")
        title.setFont(_FONT_TITLE)
        title.setStyleSheet(f"color: {_TEXT}; border: none;")
        root.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_BORDER};")
        sep.setFixedHeight(1)
        root.addWidget(sep)
        root.addSpacing(6)

        lbl = QLabel("Sweep symbols")
        lbl.setFont(_FONT_SECTION)
        lbl.setStyleSheet(f"color: {_TEXT_DIM}; border: none;")
        root.addWidget(lbl)

        desc = QLabel(
            "Up to 4 stock symbols for cash-sweep operations — each gets "
            "its own action on the account right-click menu.")
        desc.setStyleSheet(
            f"color: {_TEXT_DIM}; border: none; font-size: 11px;")
        root.addWidget(desc)

        cm = self._prefs.get("cash_management", {})
        raw = cm.get("sweep_symbols")
        if not isinstance(raw, list):
            legacy = str(cm.get("sweep_symbol", "") or "")
            raw = [legacy] if legacy else []
        symbols = [str(s or "").strip().upper() for s in raw]

        self._symbol_edits: list[QLineEdit] = []
        for i in range(self._MAX_SYMBOLS):
            edit = QLineEdit()
            edit.setText(symbols[i] if i < len(symbols) else "")
            edit.setPlaceholderText("Search symbol…")
            edit.setFixedWidth(380)
            edit.setStyleSheet(_INPUT_QSS)
            # Selection happens exclusively through the search popup.
            # The field is technically editable — Qt never shows the
            # blinking caret in read-only mode, and the focused field
            # should look like Book Trader's search box — but every
            # text-mutating path is intercepted (typing opens the
            # popup, paste/cut/undo are swallowed, context menu and
            # drops disabled), so a symbol can only enter via a real
            # IBKR pick.
            edit.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            edit.setAcceptDrops(False)
            edit.installEventFilter(self)
            # One-click removal: an inline "×" while the slot holds a
            # symbol.  The icon button is a child widget, so clicking
            # it does NOT hit the edit's event filter — the popup
            # stays closed.
            clear_act = edit.addAction(
                self.style().standardIcon(
                    QStyle.StandardPixmap.SP_LineEditClearButton),
                QLineEdit.ActionPosition.TrailingPosition)
            clear_act.setToolTip("Remove symbol")
            clear_act.triggered.connect(
                lambda _c=False, e=edit: self._clear_slot(e))
            clear_act.setVisible(bool(edit.text()))
            edit.textChanged.connect(
                lambda t, a=clear_act: a.setVisible(bool(t)))
            row = QHBoxLayout()
            row.addWidget(edit)
            row.addStretch()
            root.addLayout(row)
            self._symbol_edits.append(edit)
        root.addStretch()

    # ── Symbol search (the Book Trader chip-bar popup, stocks only) ──────────

    def _open_search(self, edit: QLineEdit) -> None:
        """Open the standalone symbol-search popup under *edit* — the
        exact component Book Trader's "+" chip uses: its own search
        box, live IBKR results, Qt.Popup semantics (outside click
        dismisses, deletes itself on close)."""
        from src.ui.widgets.symbol_search_popup import SymbolSearchPopup
        popup = SymbolSearchPopup(self, workers=self._workers,
                                  exclude=set(), stocks_only=True)
        popup.symbol_selected.connect(
            lambda entry, e=edit: self._on_symbol_picked(e, entry))
        self._popup = popup
        popup.open_below(edit)

    def _on_symbol_picked(self, edit: QLineEdit, entry: dict) -> None:
        """Adopt a popup pick into the field it was opened for.  Only
        the ticker string matters here — sweep orders resolve their
        contract through the normal order path."""
        sym = str(entry.get("symbol", "") or "").strip().upper()
        if not sym:
            return
        edit.setText(sym)
        self._on_edit()

    def _clear_slot(self, edit: QLineEdit) -> None:
        edit.clear()
        self._on_edit()

    def eventFilter(self, obj, event):  # noqa: N802 – Qt override
        if obj in self._symbol_edits:
            t = event.type()
            if t == QEvent.Type.MouseButtonPress:
                # Focus first so Backspace/Delete work right after the
                # popup dismisses (the consumed press would otherwise
                # never focus the field).
                obj.setFocus(Qt.FocusReason.MouseFocusReason)
                self._open_search(obj)
                return True
            if t == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Backspace,
                                   Qt.Key.Key_Delete):
                    obj.clear()
                    self._on_edit()
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter,
                                   Qt.Key.Key_Space, Qt.Key.Key_Down):
                    self._open_search(obj)
                    return True
                # Editing shortcuts must not mutate the pick-only
                # field: swallow paste/cut/undo/redo (copy stays).
                from PySide6.QtGui import QKeySequence
                for seq in (QKeySequence.StandardKey.Paste,
                            QKeySequence.StandardKey.Cut,
                            QKeySequence.StandardKey.Undo,
                            QKeySequence.StandardKey.Redo):
                    if event.matches(seq):
                        return True
                # Keyboard-only type-to-search: any printable character
                # on the focused field opens the popup with that
                # character already in its search box, so Tab → type →
                # ↑/↓ → Enter needs no mouse at all.  (isprintable, not
                # isalnum — "." must not slip into the field either.)
                txt = event.text()
                if (txt and txt.isprintable()
                        and not event.modifiers() & (
                            Qt.KeyboardModifier.ControlModifier
                            | Qt.KeyboardModifier.MetaModifier
                            | Qt.KeyboardModifier.AltModifier)):
                    self._open_search(obj)
                    if self._popup is not None:
                        self._popup._search.setText(txt)
                    return True
        return super().eventFilter(obj, event)

    def close_dropdown(self) -> None:
        """Host hook: close any open search popup when the Settings
        dialog closes.  Qt.Popup usually self-dismisses, but the
        dialog can also be closed programmatically underneath it."""
        popup = self._popup
        self._popup = None
        if popup is not None:
            try:
                popup.close()
            except RuntimeError:
                pass  # already deleted (WA_DeleteOnClose)

    def _symbols(self) -> list[str]:
        """Cleaned symbol list from the inputs: upper-cased, blanks
        skipped, duplicates dropped (first occurrence wins)."""
        out: list[str] = []
        for edit in self._symbol_edits:
            sym = edit.text().strip().upper()
            if sym and sym not in out:
                out.append(sym)
        return out

    def _on_edit(self) -> None:
        for edit in self._symbol_edits:
            sym = edit.text().strip().upper()
            if sym != edit.text():
                edit.setText(sym)
        cm = self._prefs.setdefault("cash_management", {})
        cm["sweep_symbols"] = self._symbols()
        cm["sweep_symbol"] = cm["sweep_symbols"][0] \
            if cm["sweep_symbols"] else ""
        self.changed.emit()

    def collect(self) -> dict:
        symbols = self._symbols()
        # Legacy key kept in sync so older builds restoring this
        # config still find their (first) sweep symbol.
        return {"sweep_symbols": symbols,
                "sweep_symbol": symbols[0] if symbols else ""}


# ═══════════════════════════════════════════════════════════════════════════════
#  Reusable asset-settings block  (Futures / Stocks / …)
# ═══════════════════════════════════════════════════════════════════════════════

class _AssetSettingsGroup(QWidget):
    """Self-contained settings block for one asset class.

    Parameters
    ----------
    title           : str  – Section heading  (e.g. ``"Futures"``).
    qty_label       : str  – Quantity unit    (e.g. ``"Contracts"``).
    increment_label : str  – Integer-step unit for offset combos
                             and ``QIntValidator`` branch (e.g. ``"ticks"``).
    prefs_key       : str  – Key inside the global prefs dict
                             (e.g. ``"trading_futures"``).
    prefs           : dict – Shared preferences dict (read at init;
                             written via ``collect()`` / ``_revert_defaults``).
    """

    changed = Signal()

    def __init__(
        self,
        title: str,
        qty_label: str,
        increment_label: str,
        prefs_key: str,
        prefs: dict,
        use_urgency_toggles: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._title = title
        self._qty_label = qty_label
        self._increment_label = increment_label
        self._prefs_key = prefs_key
        self._prefs = prefs
        self._use_urgency = use_urgency_toggles
        self._build_ui()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(14)

        tp = self._prefs.get(self._prefs_key, {})
        inc = self._increment_label
        lbl_qss = f"color: {_TEXT}; border: none; font-size: 12px;"

        # ── Header: title + Revert to Defaults ──────────────────────────────
        hdr = QHBoxLayout()
        title_lbl = QLabel(self._title)
        title_lbl.setFont(_FONT_TITLE)
        title_lbl.setStyleSheet(f"color: {_TEXT}; border: none;")
        hdr.addWidget(title_lbl)
        hdr.addStretch()
        revert_btn = QPushButton("Revert to Defaults")
        revert_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        revert_btn.setStyleSheet(f"""
            QPushButton {{
                background: {_BG_INPUT};
                color: {_TEXT_DIM};
                border: 1px solid {_BORDER};
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: {_BG_SIDEBAR_HOVER};
                color: {_TEXT};
            }}
        """)
        revert_btn.clicked.connect(self._revert_defaults)
        hdr.addWidget(revert_btn)
        root.addLayout(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_BORDER};")
        sep.setFixedHeight(1)
        root.addWidget(sep)
        root.addSpacing(4)

        # ── Quantity ─────────────────────────────────────────────────────────
        qty_row = QHBoxLayout()
        qty_row.setSpacing(10)
        qty_lbl = QLabel("Quantity")
        qty_lbl.setStyleSheet(lbl_qss)
        qty_lbl.setFixedWidth(160)
        self._qty_spin = _spin(tp.get("quantity", 1), 1, 9999)
        self._qty_unit = QLabel(self._qty_label)
        self._qty_unit.setStyleSheet(
            f"color: {_TEXT_DIM}; border: none; font-size: 12px;")
        qty_row.addWidget(qty_lbl)
        qty_row.addWidget(self._qty_spin)
        qty_row.addWidget(self._qty_unit)
        qty_row.addStretch()
        root.addLayout(qty_row)

        # ── Limit cap ────────────────────────────────────────────────────────
        lc_row = QHBoxLayout()
        lc_row.setSpacing(10)
        lc_lbl = QLabel("Limit Cap Offset")
        lc_lbl.setStyleSheet(lbl_qss)
        lc_lbl.setFixedWidth(160)
        self._lc_spin = _spin(tp.get("limit_cap", 0), 0, 99999)
        self._lc_unit = _combo(
            ["amt", "%", inc], tp.get("limit_cap_unit", inc))
        lc_row.addWidget(lc_lbl)
        lc_row.addWidget(self._lc_spin)
        lc_row.addWidget(self._lc_unit)
        lc_row.addStretch()
        root.addLayout(lc_row)

        root.addSpacing(6)

        # ── Buy/Sell @Mid ─────────────────────────────────────────────────────
        mid_row = QHBoxLayout()
        mid_row.setSpacing(10)
        mid_lbl = QLabel("Buy/Sell @Mid")
        mid_lbl.setStyleSheet(lbl_qss)
        mid_lbl.setFixedWidth(160)
        self._mid_toggle = _ToggleGroup(
            ["Patient", "Normal", "Urgent"],
            tp.get("mid_price_behavior", "Normal"),
        )
        mid_row.addWidget(mid_lbl)
        mid_row.addWidget(self._mid_toggle)
        mid_row.addStretch()
        root.addLayout(mid_row)

        root.addSpacing(6)

        # ── Buy @Bid ──────────────────────────────────────────────────────────
        bb_row = QHBoxLayout()
        bb_row.setSpacing(10)
        bb_lbl = QLabel(
            "Buy @Bid Offset" if not self._use_urgency else "Buy @Bid")
        bb_lbl.setStyleSheet(lbl_qss)
        bb_lbl.setFixedWidth(160)
        bb_row.addWidget(bb_lbl)

        if self._use_urgency:
            self._bb_toggle = _ToggleGroup(
                ["Patient", "Normal", "Urgent"],
                tp.get("buy_bid_behavior", "Normal"),
            )
            bb_row.addWidget(self._bb_toggle)
        else:
            self._bb_spin = _text_input(
                str(tp.get("buy_bid_offset", 0)), "+/- amt")
            self._bb_unit = _combo(
                ["amt", "%", inc], tp.get("buy_bid_unit", inc))
            bb_row.addWidget(self._bb_spin)
            bb_row.addWidget(self._bb_unit)

        bb_row.addStretch()
        root.addLayout(bb_row)

        # ── Sell @Ask ─────────────────────────────────────────────────────────
        sa_row = QHBoxLayout()
        sa_row.setSpacing(10)
        sa_lbl = QLabel(
            "Sell @Ask Offset" if not self._use_urgency else "Sell @Ask")
        sa_lbl.setStyleSheet(lbl_qss)
        sa_lbl.setFixedWidth(160)
        sa_row.addWidget(sa_lbl)

        if self._use_urgency:
            self._sa_toggle = _ToggleGroup(
                ["Patient", "Normal", "Urgent"],
                tp.get("sell_ask_behavior", "Normal"),
            )
            sa_row.addWidget(self._sa_toggle)
        else:
            self._sa_spin = _text_input(
                str(tp.get("sell_ask_offset", 0)), "+/- amt")
            self._sa_unit = _combo(
                ["amt", "%", inc], tp.get("sell_ask_unit", inc))
            sa_row.addWidget(self._sa_spin)
            sa_row.addWidget(self._sa_unit)

        sa_row.addStretch()
        root.addLayout(sa_row)


        # ── Wire all widgets → changed ────────────────────────────────────────
        self._qty_spin.valueChanged.connect(self.changed)
        # qty_unit is a static QLabel — no signal needed
        self._lc_spin.valueChanged.connect(self.changed)
        self._lc_unit.currentTextChanged.connect(self.changed)
        self._mid_toggle.value_changed.connect(self.changed)

        if self._use_urgency:
            self._bb_toggle.value_changed.connect(self.changed)
            self._sa_toggle.value_changed.connect(self.changed)
        else:
            self._bb_spin.editingFinished.connect(self.changed)
            self._bb_unit.currentTextChanged.connect(self.changed)
            self._sa_spin.editingFinished.connect(self.changed)
            self._sa_unit.currentTextChanged.connect(self.changed)
            # Enter handled by eventFilter — returnPressed intentionally NOT wired
            self._bb_spin.installEventFilter(self)
            self._sa_spin.installEventFilter(self)

        # ── Validators (only for text-input mode) ─────────────────────────────
        if not self._use_urgency:
            self._bb_unit.currentTextChanged.connect(
                lambda unit: self._apply_unit_validator(self._bb_spin, unit)
            )
            self._sa_unit.currentTextChanged.connect(
                lambda unit: self._apply_unit_validator(self._sa_spin, unit)
            )
            self._apply_unit_validator(
                self._bb_spin, self._bb_unit.currentText())
            self._apply_unit_validator(
                self._sa_spin, self._sa_unit.currentText())

    # ── Event filter ──────────────────────────────────────────────────────────

    def eventFilter(self, source, event) -> bool:
        """Intercept Return/Enter on offset fields: save, advance focus, consume."""
        if self._use_urgency:
            return super().eventFilter(source, event)
        if (event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and source in (self._bb_spin, self._sa_spin)):
            self.changed.emit()
            source.focusNextChild()
            return True          # consumed — never reaches QDialog
        return super().eventFilter(source, event)

    # ── Validator helper ──────────────────────────────────────────────────────

    def _apply_unit_validator(self, field: QLineEdit, unit: str) -> None:
        """QIntValidator for increment_label, QDoubleValidator(2dp) otherwise."""
        text = field.text().strip()
        if unit == self._increment_label:
            field.setValidator(QIntValidator(-999999, 999999))
            field.setPlaceholderText(f"+/- {self._increment_label}")
            try:
                if text and "." in text:
                    field.setText(str(int(round(float(text)))))
            except ValueError:
                field.setText("0")
        else:
            v = QDoubleValidator(-999999.99, 999999.99, 2)
            v.setNotation(QDoubleValidator.Notation.StandardNotation)
            field.setValidator(v)
            field.setPlaceholderText("+/- amt")

    @staticmethod
    def _parse_offset(text: str) -> float:
        """Safe float conversion — returns 0.0 for blank / non-numeric text."""
        try:
            return float(text.strip()) if text.strip() else 0.0
        except ValueError:
            return 0.0

    # ── Revert ────────────────────────────────────────────────────────────────

    def _revert_defaults(self) -> None:
        d = _DEFAULTS.get(self._prefs_key, _DEFAULTS["trading_futures"])
        self._qty_spin.setValue(d["quantity"])
        # qty_unit is a static QLabel — no reset needed
        self._lc_spin.setValue(d["limit_cap"])
        self._lc_unit.setCurrentText(d["limit_cap_unit"])
        self._mid_toggle.set_value(d["mid_price_behavior"])
        if self._use_urgency:
            self._bb_toggle.set_value(d.get("buy_bid_behavior", "Normal"))
            self._sa_toggle.set_value(d.get("sell_ask_behavior", "Normal"))
        else:
            self._bb_spin.setText(str(d.get("buy_bid_offset", 0)))
            self._bb_unit.setCurrentText(d.get("buy_bid_unit", "ticks"))
            self._sa_spin.setText(str(d.get("sell_ask_offset", 0)))
            self._sa_unit.setCurrentText(d.get("sell_ask_unit", "ticks"))
        log.info("%s preferences reverted to defaults", self._title)
        self.changed.emit()

    # ── Collect ───────────────────────────────────────────────────────────────

    def collect(self) -> dict:
        result = {
            "quantity":           self._qty_spin.value(),
            "quantity_unit":      self._qty_label,
            "limit_cap":          self._lc_spin.value(),
            "limit_cap_unit":     self._lc_unit.currentText(),
            "mid_price_behavior": self._mid_toggle.value(),
        }
        if self._use_urgency:
            result["buy_bid_behavior"] = self._bb_toggle.value()
            result["sell_ask_behavior"] = self._sa_toggle.value()
        else:
            result["buy_bid_offset"] = self._parse_offset(self._bb_spin.text())
            result["buy_bid_unit"] = self._bb_unit.currentText()
            result["sell_ask_offset"] = self._parse_offset(
                self._sa_spin.text())
            result["sell_ask_unit"] = self._sa_unit.currentText()
        return result


# ═══════════════════════════════════════════════════════════════════════════════
#  Trading settings page  (Futures + Stocks stacked in a scroll area)
# ═══════════════════════════════════════════════════════════════════════════════

class _TradingPage(QWidget):
    """Thin host widget: stacks a Futures and a Stocks _AssetSettingsGroup."""

    changed = Signal()

    def __init__(self, prefs: dict, parent=None):
        super().__init__(parent)
        self._prefs = prefs
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Scroll area keeps both blocks accessible on smaller windows
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }")

        container = QWidget()
        container.setStyleSheet(f"background: {_BG_PANEL};")
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Futures block ─────────────────────────────────────────────────────
        self._futures_group = _AssetSettingsGroup(
            "Futures", "Contracts", "ticks", "trading_futures", self._prefs,
            use_urgency_toggles=True,
        )
        lay.addWidget(self._futures_group)

        # ── Divider ───────────────────────────────────────────────────────────
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"color: {_BORDER};")
        div.setFixedHeight(1)
        lay.addWidget(div)

        # ── Stocks block ──────────────────────────────────────────────────────
        self._stocks_group = _AssetSettingsGroup(
            "Stocks", "Shares", "ticks", "trading_stocks", self._prefs
        )
        lay.addWidget(self._stocks_group)
        lay.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)

        # Bubble both groups' changes up to this page's changed signal
        self._futures_group.changed.connect(self.changed)
        self._stocks_group.changed.connect(self.changed)

    def collect(self) -> tuple[dict, dict]:
        """Return ``(futures_dict, stocks_dict)`` for direct config storage."""
        return self._futures_group.collect(), self._stocks_group.collect()


# ═══════════════════════════════════════════════════════════════════════════════
#  Main Preferences Dialog
# ═══════════════════════════════════════════════════════════════════════════════

class PreferencesDialog(QDialog):
    """Preferences dialog that auto-closes on focus loss and persists
    all settings to ``app_config.json``."""

    def __init__(self, parent=None, *, columns_provider=None,
                 auth_context=None, workers=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(920, 540)
        self.setMinimumSize(800, 420)
        self._columns_provider = columns_provider
        # Connected IB workers (priority-ordered) for pages that need a
        # live TWS symbol search — currently the sweep-symbol pickers.
        self._workers = list(workers or [])
        # auth_context (optional): {"user": User, "service": AuthService,
        # "on_sign_out": callable}.  When present the sidebar grows a user
        # chip + Sign out, and admins additionally get a "Users" page.
        self._auth_context = auth_context or {}
        self._auth_user = self._auth_context.get("user")
        self._auth_service = self._auth_context.get("service")
        self.setStyleSheet(f"""
            QDialog {{
                background: {_BG_DIALOG};
                border: 1px solid {_BORDER};
            }}
        """)

        # Load existing prefs, merge defaults
        self._prefs = config_manager.load_preferences()
        for section, defaults in _DEFAULTS.items():
            if section not in self._prefs:
                self._prefs[section] = dict(defaults)
            else:
                for k, v in defaults.items():
                    self._prefs[section].setdefault(k, v)

        self._build_ui()

        # Use a single-shot timer to defer the focus-loss check so that
        # combo-box popups don't trigger an immediate close.
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.setInterval(200)
        self._close_timer.timeout.connect(self._check_should_close)

    def _build_ui(self) -> None:
        main_lay = QHBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # ── Left sidebar ─────────────────────────────────────────────────────
        # Wrap the section list in a container so a user chip + Sign out can
        # sit pinned at the bottom when an auth context is supplied.
        sidebar_host = QWidget()
        sidebar_host.setFixedWidth(140)
        sidebar_host.setStyleSheet(f"background: {_BG_SIDEBAR};")
        sh_lay = QVBoxLayout(sidebar_host)
        sh_lay.setContentsMargins(0, 0, 0, 0)
        sh_lay.setSpacing(0)

        self._sidebar = _styled_sidebar()
        self._sidebar.addItem(QListWidgetItem("Display"))
        self._sidebar.addItem(QListWidgetItem("Trading"))
        self._sidebar.addItem(QListWidgetItem("Columns"))
        self._sidebar.addItem(QListWidgetItem("Cash Management"))
        # Admins additionally manage users.
        self._has_users_page = bool(
            self._auth_user is not None and self._auth_user.is_admin
            and self._auth_service is not None)
        if self._has_users_page:
            self._sidebar.addItem(QListWidgetItem("Users"))
        # Backup & Restore is available to any signed-in user.
        self._has_backup_page = self._auth_user is not None
        if self._has_backup_page:
            self._sidebar.addItem(QListWidgetItem("Backup & Restore"))
        self._sidebar.setCurrentRow(0)
        self._sidebar.currentRowChanged.connect(self._on_page_changed)
        sh_lay.addWidget(self._sidebar, stretch=1)

        if self._auth_user is not None:
            sh_lay.addWidget(self._build_sidebar_footer())

        main_lay.addWidget(sidebar_host)

        # Vertical divider
        div = QFrame()
        div.setFrameShape(QFrame.Shape.VLine)
        div.setStyleSheet(f"color: {_BORDER};")
        div.setFixedWidth(1)
        main_lay.addWidget(div)

        # ── Right stacked pages ──────────────────────────────────────────────
        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background: {_BG_PANEL};")

        self._display_page = _DisplayPage(self._prefs)
        self._trading_page = _TradingPage(self._prefs)
        self._columns_page = ColumnsPage(self._columns_provider, self)
        self._cash_page = _CashManagementPage(self._prefs,
                                              workers=self._workers)

        self._stack.addWidget(self._display_page)
        self._stack.addWidget(self._trading_page)
        self._stack.addWidget(self._columns_page)
        self._stack.addWidget(self._cash_page)

        self._users_page = None
        if self._has_users_page:
            from src.ui.auth.users_page import UsersPage
            self._users_page = UsersPage(self._auth_service)
            self._stack.addWidget(self._users_page)

        self._backup_page = None
        if self._has_backup_page:
            from .backup_page import BackupPage
            self._backup_page = BackupPage(self._auth_user.username)
            self._backup_page.reload_requested.connect(self._on_restore_reload)
            self._stack.addWidget(self._backup_page)

        main_lay.addWidget(self._stack, stretch=1)

        # Auto-save: any change on a page persists to config immediately
        self._display_page.changed.connect(self._save)
        self._trading_page.changed.connect(self._save)
        self._cash_page.changed.connect(self._save)

    # ── Sidebar footer (user chip + change password + sign out) ───────────────

    def _build_sidebar_footer(self) -> QWidget:
        user = self._auth_user
        foot = QWidget()
        foot.setStyleSheet(f"background: {_BG_SIDEBAR};")
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(10, 10, 10, 12)
        fl.setSpacing(8)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_BORDER};")
        sep.setFixedHeight(1)
        fl.addWidget(sep)

        chip = QHBoxLayout()
        chip.setSpacing(9)
        avatar = QLabel((user.display_name or user.username or "?")[:1].upper())
        avatar.setFixedSize(26, 26)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(
            f"background: {_ACCENT_BLUE}; color: #fff; border-radius: 13px;"
            " font-weight: 700; font-size: 12px;")
        chip.addWidget(avatar)
        name_col = QVBoxLayout()
        name_col.setSpacing(0)
        name_lbl = QLabel(user.display_name or user.username)
        name_lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 12px; font-weight: 500; background: transparent;")
        role_lbl = QLabel("Admin · no trading" if user.is_admin else "Trader")
        role_lbl.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 11px; background: transparent;")
        name_col.addWidget(name_lbl)
        name_col.addWidget(role_lbl)
        chip.addLayout(name_col)
        chip.addStretch()
        fl.addLayout(chip)

        # "Lock" sits with the other session actions.  Only offered when
        # the host provided an on_lock callback (MainWindow's session
        # lock) — the dialog itself can't lock anything.
        if callable(self._auth_context.get("on_lock")):
            lock_btn = QPushButton("Lock  (⌘L)")
            lock_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            lock_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; border: 1px solid {_BORDER};"
                f" border-radius: 6px; color: {_TEXT_DIM}; font-size: 12px; padding: 6px; }}"
                f"QPushButton:hover {{ background: {_BG_SIDEBAR_HOVER}; color: {_TEXT}; }}")
            lock_btn.clicked.connect(self._on_lock)
            fl.addWidget(lock_btn)

        pw_btn = QPushButton("Change password")
        pw_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pw_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {_BORDER};"
            f" border-radius: 6px; color: {_TEXT_DIM}; font-size: 12px; padding: 6px; }}"
            f"QPushButton:hover {{ background: {_BG_SIDEBAR_HOVER}; color: {_TEXT}; }}")
        pw_btn.clicked.connect(self._on_change_password)
        fl.addWidget(pw_btn)

        out_btn = QPushButton("Sign out")
        out_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        out_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {_BORDER};"
            f" border-radius: 6px; color: {_TEXT_DIM}; font-size: 12px; padding: 6px; }}"
            f"QPushButton:hover {{ background: {_BG_SIDEBAR_HOVER}; color: {_TEXT}; }}")
        out_btn.clicked.connect(self._on_sign_out)
        fl.addWidget(out_btn)
        return foot

    def _on_change_password(self) -> None:
        from src.ui.auth.change_pw_dialog import ChangePasswordDialog
        ChangePasswordDialog(
            self._auth_service, self._auth_user.username, self).exec()

    def _on_lock(self) -> None:
        cb = self._auth_context.get("on_lock")
        self.close()
        if callable(cb):
            cb()

    def _on_sign_out(self) -> None:
        cb = self._auth_context.get("on_sign_out")
        self.close()
        if callable(cb):
            cb()

    def _on_restore_reload(self) -> None:
        """After a successful restore, return to Login so the freshly restored
        accounts/config/databases take effect (same path as a sign-out)."""
        self._on_sign_out()

    # ── Navigation ────────────────────────────────────────────────────────────

    def _on_page_changed(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        # When jumping into Columns via the sidebar, land on the overview.
        if self._stack.currentWidget() is self._columns_page:
            self._columns_page.navigate_to("overview")
        # Refresh the users table each time it's shown so it reflects any
        # changes made in another session.
        if self._users_page is not None and self._stack.currentWidget() is self._users_page:
            self._users_page.refresh()

    def navigate_to(self, section: str, subview: str | None = None) -> None:
        """Programmatically switch to a sidebar section; for ``Columns`` you
        may additionally pass ``"flat"`` / ``"summary"`` / ``"overview"`` to
        land on a specific subview (used by the Trades gear button)."""
        section_to_row = {"Display": 0, "Trading": 1, "Columns": 2,
                          "Cash Management": 3}
        next_row = 4
        if self._has_users_page:
            section_to_row["Users"] = next_row
            next_row += 1
        if self._has_backup_page:
            section_to_row["Backup & Restore"] = next_row
        row = section_to_row.get(section)
        if row is None:
            return
        self._sidebar.setCurrentRow(row)
        if section == "Columns" and subview:
            self._columns_page.navigate_to(subview)

    # ── Auto-close on focus loss (geometry-based with absolute screen coordinates) ──

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange:
            if not self.isActiveWindow():
                # Defer the check so combo-box popups have time to return focus
                self._close_timer.start()

    def _check_should_close(self) -> None:
        """Close only if the dialog is 100% contained within the main window.

        Logic
        -----
        * ``mapToGlobal(QPoint(0, 0))`` gives the widget's CLIENT-AREA top-left
          in absolute screen pixels — reliable across all monitors and DPI
          configurations.
        * We subtract the frame delta (titlebar height, border widths) to
          extend the rect to the full OS-chrome boundary so the title-bar of
          the dialog is included in the check.
        * ``QRect.contains(other)`` returns True only when *every* pixel of
          ``other`` lies inside ``self``.  Even a 1-pixel overhang returns
          False — so a partially-dragged or second-monitor dialog stays open.
        """
        if not self.isVisible() or self.isActiveWindow():
            return

        # A modal child (Add-user dialog, Change-password dialog, QMessageBox)
        # steals activation from this dialog — don't auto-close underneath it.
        if QApplication.activeModalWidget() is not None:
            return

        # The sweep-symbol search popup (Qt.Popup) also deactivates the
        # dialog while it's up — closing Settings underneath it would
        # yank the popup's anchor away mid-search.
        from src.ui.widgets.symbol_search_popup import SymbolSearchPopup
        for w in QApplication.topLevelWidgets():
            if isinstance(w, SymbolSearchPopup) and w.isVisible():
                return

        parent = self.parent()
        if parent is None:
            self.close()
            return

        def _abs_frame_rect(widget) -> QRect:
            """Return widget's full frame rect in absolute screen coordinates.

            Steps:
              1. mapToGlobal(QPoint(0,0))  → client-area top-left in screen px
              2. frameGeometry / geometry delta → frame insets (title-bar, borders)
              3. Subtract insets to reach the true frame top-left
            """
            client_tl = widget.mapToGlobal(QPoint(0, 0))
            frame_geo = widget.frameGeometry()   # screen-absolute on top-levels,
            client_geo = widget.geometry()         # but we derive it ourselves to
            # be explicit about the mapping.
            # How far the OS frame extends beyond the client area on each side
            frame_dx = client_geo.left() - frame_geo.left()   # left border width
            frame_dy = client_geo.top() - frame_geo.top()    # title-bar height
            return QRect(
                client_tl.x() - frame_dx,
                client_tl.y() - frame_dy,
                frame_geo.width(),
                frame_geo.height(),
            )

        dialog_rect = _abs_frame_rect(self)
        parent_rect = _abs_frame_rect(parent)
        contained = parent_rect.contains(dialog_rect)

        print(f"[AutoClose] dialog  rect (screen px): {dialog_rect}  "
              f"tl=({dialog_rect.left()}, {dialog_rect.top()})  "
              f"br=({dialog_rect.right()}, {dialog_rect.bottom()})")
        print(f"[AutoClose] parent  rect (screen px): {parent_rect}  "
              f"tl=({parent_rect.left()}, {parent_rect.top()})  "
              f"br=({parent_rect.right()}, {parent_rect.bottom()})")
        print(f"[AutoClose] contained               : {contained}  → "
              f"{'CLOSING  (dialog fully inside main window)' if contained else 'KEEPING OPEN  (hanging outside or on 2nd monitor)'}")

        if contained:
            self.close()

    # ── Save on close ─────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        # The sweep-symbol search dropdown is a floating tool window —
        # it doesn't close with the dialog on its own.
        cash = getattr(self, "_cash_page", None)
        if cash is not None:
            cash.close_dropdown()
        self._save()
        super().closeEvent(event)


    def _save(self) -> None:
        self._prefs["display"] = self._display_page.collect()
        futures_data, stocks_data = self._trading_page.collect()
        self._prefs["trading_futures"] = futures_data
        self._prefs["trading_stocks"] = stocks_data
        self._prefs["cash_management"] = self._cash_page.collect()
        config_manager.save_preferences(self._prefs)

        # Push into the in-memory singletons so the next trade uses fresh values
        from src.core.trading_prefs import trading_prefs
        trading_prefs.update(futures=futures_data, stocks=stocks_data)
        from src.core.cash_management import cash_prefs
        cash_prefs.reload()

        log.info("Preferences saved")
