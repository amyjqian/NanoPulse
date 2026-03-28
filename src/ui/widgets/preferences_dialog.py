"""
Preferences dialog – Display & Trading settings for NanoPulse.

Auto-closes when focus is lost.  Persists all settings to app_config.json.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.core import config_manager

log = logging.getLogger(__name__)

# ── Colour palette (matches main app dark theme) ─────────────────────────────
_BG_DIALOG = "#1e2230"
_BG_SIDEBAR = "#161a25"
_BG_SIDEBAR_HOVER = "#1c2030"
_BG_SIDEBAR_SEL = "#263050"
_BG_PANEL = "#1e2230"
_BG_INPUT = "#272b3a"
_BORDER = "#30363D"
_TEXT = "#C9D1D9"
_TEXT_DIM = "#8B949E"
_ACCENT_BLUE = "#0071e3"

_FONT_TITLE = QFont("Helvetica Neue, Arial", 14)
_FONT_TITLE.setWeight(QFont.Weight.Bold)
_FONT_SECTION = QFont("Helvetica Neue, Arial", 12)
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
        "buy_bid_offset": 0,
        "buy_bid_unit": "ticks",
        "sell_ask_offset": 0,
        "sell_ask_unit": "ticks",
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Styled sidebar list
# ═══════════════════════════════════════════════════════════════════════════════

def _styled_sidebar() -> QListWidget:
    lw = QListWidget()
    lw.setFixedWidth(140)
    lw.setFrameShape(QFrame.Shape.NoFrame)
    lw.setFont(QFont("Helvetica Neue, Arial", 12))
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
    QSpinBox, QComboBox {{
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
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
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
        chart_fg = QColor("#3FB950") if is_dark else QColor("#0071e3")
        bar_red = QColor("#c62828") if is_dark else QColor("#d32f2f")
        bar_green = QColor("#2e7d32") if is_dark else QColor("#388e3c")
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
        p.setFont(QFont("Helvetica Neue, Arial", 11, QFont.Weight.Medium))
        p.drawText(0, h - 18, w, 18, Qt.AlignmentFlag.AlignHCenter, self._label)
        p.end()


# ═══════════════════════════════════════════════════════════════════════════════
#  Display settings page
# ═══════════════════════════════════════════════════════════════════════════════

class _DisplayPage(QWidget):
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

    def collect(self) -> dict:
        return {"theme": self._prefs.get("display", {}).get("theme", "Dark")}


# ═══════════════════════════════════════════════════════════════════════════════
#  Trading (Futures) settings page
# ═══════════════════════════════════════════════════════════════════════════════

class _TradingPage(QWidget):
    def __init__(self, prefs: dict, parent=None):
        super().__init__(parent)
        self._prefs = prefs
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(14)

        tp = self._prefs.get("trading_futures", {})

        # ── Header: title + Revert to Defaults ──────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel("Futures")
        title.setFont(_FONT_TITLE)
        title.setStyleSheet(f"color: {_TEXT}; border: none;")
        hdr.addWidget(title)
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

        lbl_qss = f"color: {_TEXT}; border: none; font-size: 12px;"

        # ── Quantity ─────────────────────────────────────────────────────────
        qty_row = QHBoxLayout()
        qty_row.setSpacing(10)
        qty_lbl = QLabel("Quantity")
        qty_lbl.setStyleSheet(lbl_qss)
        qty_lbl.setFixedWidth(130)
        self._qty_spin = _spin(tp.get("quantity", 1), 1, 9999)
        self._qty_unit = _combo(["Contracts"], tp.get("quantity_unit", "Contracts"))
        qty_row.addWidget(qty_lbl)
        qty_row.addWidget(self._qty_spin)
        qty_row.addWidget(self._qty_unit)
        qty_row.addStretch()
        root.addLayout(qty_row)

        # ── Limit cap ────────────────────────────────────────────────────────
        lc_row = QHBoxLayout()
        lc_row.setSpacing(10)
        lc_lbl = QLabel("Limit cap")
        lc_lbl.setStyleSheet(lbl_qss)
        lc_lbl.setFixedWidth(130)
        self._lc_spin = _spin(tp.get("limit_cap", 0), 0, 99999)
        self._lc_unit = _combo(["amt", "%", "ticks"], tp.get("limit_cap_unit", "ticks"))
        lc_row.addWidget(lc_lbl)
        lc_row.addWidget(self._lc_spin)
        lc_row.addWidget(self._lc_unit)
        lc_row.addStretch()
        root.addLayout(lc_row)

        root.addSpacing(6)

        # ── Mid price Behavior ───────────────────────────────────────────────
        mid_row = QHBoxLayout()
        mid_row.setSpacing(10)
        mid_lbl = QLabel("Mid price Behavior")
        mid_lbl.setStyleSheet(lbl_qss)
        mid_lbl.setFixedWidth(130)
        self._mid_toggle = _ToggleGroup(
            ["Patient", "Normal", "Urgent"],
            tp.get("mid_price_behavior", "Normal"),
        )
        mid_row.addWidget(mid_lbl)
        mid_row.addWidget(self._mid_toggle)
        mid_row.addStretch()
        root.addLayout(mid_row)

        root.addSpacing(6)

        # ── buy bid ──────────────────────────────────────────────────────────
        bb_row = QHBoxLayout()
        bb_row.setSpacing(10)
        bb_lbl = QLabel("buy bid")
        bb_lbl.setStyleSheet(lbl_qss)
        bb_lbl.setFixedWidth(130)
        self._bb_spin = _spin(tp.get("buy_bid_offset", 0), 0, 99999)
        self._bb_unit = _combo(["amt", "%", "ticks"], tp.get("buy_bid_unit", "ticks"))
        bb_row.addWidget(bb_lbl)
        bb_row.addWidget(self._bb_spin)
        bb_row.addWidget(self._bb_unit)
        bb_row.addStretch()
        root.addLayout(bb_row)

        # ── sell ask ─────────────────────────────────────────────────────────
        sa_row = QHBoxLayout()
        sa_row.setSpacing(10)
        sa_lbl = QLabel("sell ask")
        sa_lbl.setStyleSheet(lbl_qss)
        sa_lbl.setFixedWidth(130)
        self._sa_spin = _spin(tp.get("sell_ask_offset", 0), 0, 99999)
        self._sa_unit = _combo(["amt", "%", "ticks"], tp.get("sell_ask_unit", "ticks"))
        sa_row.addWidget(sa_lbl)
        sa_row.addWidget(self._sa_spin)
        sa_row.addWidget(self._sa_unit)
        sa_row.addStretch()
        root.addLayout(sa_row)

        root.addStretch()

    def _revert_defaults(self) -> None:
        """Reset all widgets to their default values."""
        d = _DEFAULTS["trading_futures"]
        self._qty_spin.setValue(d["quantity"])
        self._qty_unit.setCurrentText(d["quantity_unit"])
        self._lc_spin.setValue(d["limit_cap"])
        self._lc_unit.setCurrentText(d["limit_cap_unit"])
        self._mid_toggle.set_value(d["mid_price_behavior"])
        self._bb_spin.setValue(d["buy_bid_offset"])
        self._bb_unit.setCurrentText(d["buy_bid_unit"])
        self._sa_spin.setValue(d["sell_ask_offset"])
        self._sa_unit.setCurrentText(d["sell_ask_unit"])
        log.info("Trading preferences reverted to defaults")

    def collect(self) -> dict:
        return {
            "quantity": self._qty_spin.value(),
            "quantity_unit": self._qty_unit.currentText(),
            "limit_cap": self._lc_spin.value(),
            "limit_cap_unit": self._lc_unit.currentText(),
            "mid_price_behavior": self._mid_toggle.value(),
            "buy_bid_offset": self._bb_spin.value(),
            "buy_bid_unit": self._bb_unit.currentText(),
            "sell_ask_offset": self._sa_spin.value(),
            "sell_ask_unit": self._sa_unit.currentText(),
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  Main Preferences Dialog
# ═══════════════════════════════════════════════════════════════════════════════

class PreferencesDialog(QDialog):
    """Preferences dialog that auto-closes on focus loss and persists
    all settings to ``app_config.json``."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.resize(620, 440)
        self.setMinimumSize(500, 350)
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
        self._sidebar = _styled_sidebar()
        self._sidebar.addItem(QListWidgetItem("Display"))
        self._sidebar.addItem(QListWidgetItem("Trading"))
        self._sidebar.setCurrentRow(0)
        self._sidebar.currentRowChanged.connect(self._on_page_changed)
        main_lay.addWidget(self._sidebar)

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

        self._stack.addWidget(self._display_page)
        self._stack.addWidget(self._trading_page)
        main_lay.addWidget(self._stack, stretch=1)

    # ── Navigation ────────────────────────────────────────────────────────────

    def _on_page_changed(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)

    # ── Auto-close on focus loss (deferred to avoid combo-popup false trigger)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == event.Type.ActivationChange:
            if not self.isActiveWindow():
                self._close_timer.start()

    def _check_should_close(self) -> None:
        """Only close if this dialog is still not the active window after
        the timer fires (gives combo popups time to return focus)."""
        if self.isVisible() and not self.isActiveWindow():
            self.close()

    # ── Save on close ─────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        self._save()
        super().closeEvent(event)

    def _save(self) -> None:
        self._prefs["display"] = self._display_page.collect()
        self._prefs["trading_futures"] = self._trading_page.collect()
        config_manager.save_preferences(self._prefs)
        log.info("Preferences saved")


