from __future__ import annotations
from src.core.enums import OrderAction, OrderType
from src.ui.theme import Theme

import logging
import math
from typing import Any

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
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
    """Return 'FUT', 'STK', 'OPT', or '?' for *sym* via ReferenceData cache."""
    from src.core.reference_data import get_instance as _rd
    return _rd().get_sec_type(sym)


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



# ═══════════════════════════════════════════════════════════════════════════════
#  TickPriceInput – tick-aware price spinner with floating price-ladder popup
# ═══════════════════════════════════════════════════════════════════════════════

class TickPriceInput(QWidget):
    """Tick-aware price spinner with a floating price-ladder popup.

    Popup behaviour
    ---------------
    * Uses ``Qt.Tool + WA_ShowWithoutActivating`` instead of ``Qt.Popup``.
      ``Qt.Popup`` grabs the mouse exclusively so clicking outside consumes
      the click and users feel "trapped".  ``Qt.Tool`` lets outside clicks
      pass straight through to their targets while still appearing on top.
    * An application-level event filter watches for ``MouseButtonPress``
      events outside the popup rectangle and closes it without swallowing
      the click.
    * Initial list: ``_LADDER_ROWS`` rows each side.  When the user scrolls
      within ``_EXTEND_THRESHOLD`` rows of either end, ``_EXTEND_ROWS`` more
      rows are silently prepended / appended — giving unlimited scroll.

    Public API
    ----------
    text()                            → str
    clear()                           → silently clears (no textChanged)
    textChanged  Signal[str]
    set_market_price(price, tick)     → refresh the spin / ladder reference
    """

    textChanged = Signal(str)

    _LADDER_ROWS      = 150   # initial rows above *and* below the mid price
    _EXTEND_ROWS      = 100   # rows added each time the edge is approached
    _EXTEND_THRESHOLD = 5     # trigger when within this many rows of the edge
    _ROW_H            = 22    # px height of each popup row
    _VISIBLE_ROWS     = 14    # how many rows are visible without scrolling
    _BTN_W            = 16    # px width of the up/down arrow column
    _CHIP_H           = 28    # must match _TierSection._CHIP_H

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._market_price: float           = 0.0
        self._tick_size:    float           = 0.25
        self._popup:        QListWidget | None = None
        self._app_filter_installed          = False
        self._extending_popup               = False
        self._arrow_w:      QWidget | None  = None   # set in _build_ui; used for popup X alignment
        self._build_ui()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText("Price")
        self._edit.setFixedHeight(self._CHIP_H)
        self._edit.setStyleSheet(
            "QLineEdit{background:#3a3a4a;color:#ccc;border:none;"
            "border-radius:4px 0 0 4px;padding:2px 6px;font-size:11px}"
            "QLineEdit:focus{background:#4a4a5a;color:white}"
            "QLineEdit:disabled{background:#252535;color:#444;border:none}"
        )
        self._edit.textChanged.connect(self._relay_text)
        self._edit.installEventFilter(self)

        _arrow_ss = (
            "QToolButton{background:#3a3a4a;color:#aaa;border:none;"
            "font-size:7px;padding:0;margin:0}"
            "QToolButton:hover{background:#4a4a5a;color:white}"
            "QToolButton:pressed{background:#0071e3;color:white}"
        )
        self._btn_up = QToolButton()
        self._btn_up.setText("▲")
        self._btn_up.setFixedSize(self._BTN_W, self._CHIP_H // 2)
        self._btn_up.setStyleSheet(_arrow_ss)
        self._btn_up.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_up.setAutoRepeat(True)
        self._btn_up.setAutoRepeatDelay(400)    # ms before repeat starts
        self._btn_up.setAutoRepeatInterval(80)  # ms between each repeat tick
        self._btn_up.clicked.connect(self._on_up)

        self._btn_dn = QToolButton()
        self._btn_dn.setText("▼")
        self._btn_dn.setFixedSize(self._BTN_W, self._CHIP_H // 2)
        self._btn_dn.setStyleSheet(_arrow_ss)
        self._btn_dn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_dn.setAutoRepeat(True)
        self._btn_dn.setAutoRepeatDelay(400)
        self._btn_dn.setAutoRepeatInterval(80)
        self._btn_dn.clicked.connect(self._on_dn)

        arrow_col = QVBoxLayout()
        arrow_col.setSpacing(0)
        arrow_col.setContentsMargins(0, 0, 0, 0)
        arrow_col.addWidget(self._btn_up)
        arrow_col.addWidget(self._btn_dn)

        self._arrow_w = QWidget()
        self._arrow_w.setFixedSize(self._BTN_W, self._CHIP_H)
        self._arrow_w.setStyleSheet(
            "QWidget{background:#3a3a4a;border:none;border-radius:0 4px 4px 0}")
        self._arrow_w.setLayout(arrow_col)

        lay.addWidget(self._edit, 1)
        lay.addWidget(self._arrow_w)

    # ── Event filter (popup trigger + outside-click dismissal) ────────────────

    def eventFilter(self, obj, event) -> bool:          # noqa: N802
        t = event.type()

        # ── Outside-click closes the popup WITHOUT consuming the click ────
        # This filter is active both on _edit (always) and on QApplication
        # (only while the popup is visible).
        if (self._popup_open()
                and t == QEvent.Type.MouseButtonPress
                and obj is not self._popup):
            # event is QMouseEvent at runtime when type == MouseButtonPress
            try:
                gpos = event.globalPosition().toPoint()  # type: ignore[attr-defined]
            except AttributeError:
                gpos = event.globalPos()                  # type: ignore[attr-defined]
            if not self._popup.geometry().contains(gpos):
                self._close_popup()
                return False                # let the click reach its target

        # ── Trigger popup when user clicks the field (empty or not) ──────────
        if obj is self._edit and t == QEvent.Type.MouseButtonPress:
            if not self._popup_open():
                QTimer.singleShot(50, self._show_popup)

        return False   # never consume events

    def _popup_open(self) -> bool:
        return self._popup is not None and self._popup.isVisible()

    # ── Public API ────────────────────────────────────────────────────────────

    def text(self) -> str:
        return self._edit.text()

    def clear(self) -> None:
        self._edit.blockSignals(True)
        self._edit.clear()
        self._edit.blockSignals(False)

    def setPlaceholderText(self, txt: str) -> None:
        self._edit.setPlaceholderText(txt)

    def set_market_price(self, price: float, tick_size: float | None = None) -> None:
        self._market_price = price
        if tick_size is not None and tick_size > 0:
            self._tick_size = tick_size

    # ── Signal relay ──────────────────────────────────────────────────────────

    def _relay_text(self, text: str) -> None:
        if not self.signalsBlocked():
            self.textChanged.emit(text)

    # ── Tick math ─────────────────────────────────────────────────────────────

    def _snap(self, price: float) -> float:
        """Round *price* to the nearest tick boundary using exact decimal arithmetic.

        ``round(..., 10)`` on native floats can still accumulate drift (e.g.
        ``24360.75 + 100 * 0.25`` may produce ``24385.750000000004``).
        ``decimal.Decimal`` arithmetic is exact for any representable tick size.
        """
        if self._tick_size <= 0:
            return price
        import decimal
        tick    = decimal.Decimal(str(self._tick_size))
        p       = decimal.Decimal(str(round(price, 12)))   # strip extreme noise
        snapped = (p / tick).to_integral_value(
                      rounding=decimal.ROUND_HALF_UP) * tick
        return float(snapped)

    def _decimals(self) -> int:
        """Return the number of decimal places needed to display one tick exactly.

        ``log10``-based formulas fail for non-power-of-10 tick sizes:
        ``log10(0.25) ≈ −0.602 → floor = −1 → 1 decimal`` but 0.25 needs **2**.
        ``decimal.Decimal.as_tuple().exponent`` gives the correct answer directly.
        """
        if self._tick_size >= 1:
            return 0
        import decimal
        try:
            return max(0, -decimal.Decimal(str(self._tick_size)).as_tuple().exponent)
        except Exception:
            # Safe fallback for exotic values
            return max(0, -int(math.floor(math.log10(self._tick_size))))

    def _fmt(self, price: float) -> str:
        return f"{price:.{self._decimals()}f}"

    # ── Up / Down spin ────────────────────────────────────────────────────────

    def _on_up(self) -> None:
        text = self._edit.text().strip()
        if not text:
            if self._market_price > 0:
                self._edit.setText(self._fmt(self._snap(
                    self._market_price + self._tick_size)))
        else:
            try:
                self._edit.setText(self._fmt(self._snap(
                    float(text) + self._tick_size)))
            except ValueError:
                pass

    def _on_dn(self) -> None:
        text = self._edit.text().strip()
        if not text:
            if self._market_price > 0:
                new = self._snap(self._market_price - self._tick_size)
                if new > 0:
                    self._edit.setText(self._fmt(new))
        else:
            try:
                new = self._snap(float(text) - self._tick_size)
                if new > 0:
                    self._edit.setText(self._fmt(new))
            except ValueError:
                pass

    # ── Price ladder popup ────────────────────────────────────────────────────

    def _show_popup(self) -> None:
        if self._popup_open():
            return
        self._close_popup()          # remove any stale hidden reference

        popup = QListWidget()
        popup.setUniformItemSizes(True)

        popup.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        popup.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        popup.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        popup.setStyleSheet("""
            QListWidget {
                background: #1E222A;
                color: #C9D1D9;
                border: 1px solid #3a3f50;
                border-radius: 4px;
                font-size: 12px;
                outline: none;
            }
            QListWidget::item            { padding: 3px 14px; border: none; }
            QListWidget::item:selected,
            QListWidget::item:hover      { background: #0071e3; color: white; }
            QScrollBar:vertical {
                background: #1E222A; width: 6px; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #3a3f50; border-radius: 3px; min-height: 20px;
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical { height: 0; }
        """)

        # ── Determine anchor price ────────────────────────────────────────
        has_price   = self._market_price > 0 or bool(self._edit.text().strip())
        centre      = 0.0
        centre_item = None   # will point at the row to centre in the viewport

        if has_price:
            text = self._edit.text().strip()
            try:
                centre = self._snap(float(text)) if text else self._snap(self._market_price)
            except ValueError:
                centre = self._snap(self._market_price)
            if centre <= 0:
                has_price = False

        # ── Populate price rows ───────────────────────────────────────────
        if has_price:
            mid   = centre
            steps = self._LADDER_ROWS

            # "Market" header (non-selectable)
            hdr = QListWidgetItem("  Market")
            hdr.setForeground(QColor("#8B949E"))
            hdr.setFont(_TITLE_FONT)
            hdr.setFlags(hdr.flags()
                         & ~Qt.ItemFlag.ItemIsSelectable
                         & ~Qt.ItemFlag.ItemIsEnabled)
            popup.addItem(hdr)

            mid_row_in_popup = steps + 1   # index of the centre price row (+1 for header)
            for i in range(steps * 2 + 1):
                price_val = mid + (steps - i) * self._tick_size
                price_str = self._fmt(self._snap(price_val))
                item = QListWidgetItem(price_str)
                if i == steps:          # centre / current-price row
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                    item.setForeground(QColor("#3FB950"))
                    centre_item = item
                popup.addItem(item)
        else:
            ph = QListWidgetItem("  Waiting for market data…")
            ph.setForeground(QColor("#6B7280"))
            ph.setFlags(ph.flags()
                        & ~Qt.ItemFlag.ItemIsSelectable
                        & ~Qt.ItemFlag.ItemIsEnabled)
            popup.addItem(ph)

        # ── Geometry ──────────────────────────────────────────────────────
        popup_w = max(self._BTN_W + 4, 120)
        popup_h = self._VISIBLE_ROWS * self._ROW_H + 8
        popup.setFixedSize(popup_w, popup_h)

        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication as _QApp

        # X ── ask the arrow widget directly for its global left edge.
        #      This is exact; computing self.width()-_BTN_W is fragile
        #      because layout rounding can shift it by 1-2 px.
        if self._arrow_w is not None:
            arrow_global_x = self._arrow_w.mapToGlobal(QPoint(0, 0)).x()
        else:
            arrow_global_x = self.mapToGlobal(QPoint(self.width() - self._BTN_W, 0)).x()

        # Y ── vertical centre of the popup aligns with the vertical centre
        #      of the TickPriceInput so the highlighted price row sits flush
        #      with the input field once scrollToItem centres it.
        widget_global_cy = self.mapToGlobal(QPoint(0, self.height() // 2)).y()
        popup_top_y      = widget_global_cy - popup_h // 2

        global_origin = QPoint(arrow_global_x, popup_top_y)

        # Clamp to the screen that actually contains the widget.
        # primaryScreen() is wrong on multi-monitor setups — it always
        # references the main display even when the widget lives on a
        # secondary one.  screenAt() resolves the correct screen.
        widget_centre_global = self.mapToGlobal(
            QPoint(self.width() // 2, self.height() // 2))
        screen = _QApp.screenAt(widget_centre_global) or _QApp.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            gx = max(sg.left(),  min(global_origin.x(), sg.right()  - popup_w))
            gy = max(sg.top(),   min(global_origin.y(), sg.bottom() - popup_h))
            global_origin = QPoint(gx, gy)

        # ── Wire signals ──────────────────────────────────────────────────
        popup.itemClicked.connect(self._on_popup_picked)
        popup.destroyed.connect(self._on_popup_destroyed)
        self._popup = popup

        # show() first, then move() — on macOS the window manager repositions
        # Tool windows during show(), so any pre-show move() is ignored.
        # Calling move() immediately after show() overrides that adjustment.
        popup.show()
        popup.move(global_origin)   # ← exact alignment enforced post-show
        popup.raise_()              # keep it on top of the main window

        # IMPORTANT: scrollToItem(PositionAtCenter) must be called AFTER
        # show() so Qt has computed the actual viewport height.  Calling it
        # before show() means Qt doesn't know how tall the visible area is
        # and the item ends up at the top rather than the centre.
        if centre_item is not None:
            _ci = centre_item   # local ref for the lambda capture
            QTimer.singleShot(0, lambda: popup.scrollToItem(
                _ci, QAbstractItemView.ScrollHint.PositionAtCenter))

        # Dynamic scroll-extension: more rows added when user reaches either end
        popup.verticalScrollBar().valueChanged.connect(self._on_popup_scroll)

        # App-level filter: detect outside clicks to close without swallowing them
        app = _QApp.instance()
        if app and not self._app_filter_installed:
            app.installEventFilter(self)
            self._app_filter_installed = True


    # ── Dynamic scroll extension ──────────────────────────────────────────────

    def _on_popup_scroll(self, value: int) -> None:
        """Extend the ladder when the user scrolls near either end."""
        if not self._popup_open() or self._extending_popup:
            return
        vbar      = self._popup.verticalScrollBar()
        threshold = self._EXTEND_THRESHOLD * self._ROW_H
        if value <= threshold:
            self._extend_popup_top()
        elif value >= vbar.maximum() - threshold:
            self._extend_popup_bottom()

    def _extend_popup_top(self) -> None:
        """Prepend _EXTEND_ROWS price rows above the current top of the list.

        Uses scrollToItem instead of manual pixel arithmetic so the viewport
        stays perfectly stable regardless of the actual rendered row height.
        """
        popup = self._popup
        if not popup:
            return
        # Item 0 = "Market" header (non-selectable); item 1 = current top price
        top_item = popup.item(1)
        if not top_item:
            return
        try:
            top_price = float(top_item.text().strip())
        except ValueError:
            return

        self._extending_popup = True
        N = self._EXTEND_ROWS
        # Insert N rows right after the header (index 1 … N), highest price first
        for i in range(N):
            price_val = top_price + (N - i) * self._tick_size
            popup.insertItem(1 + i, QListWidgetItem(
                self._fmt(self._snap(price_val))))

        # The original top-price row has moved from index 1 → index N+1.
        # Scroll it back to the top of the viewport — Qt handles the exact pixels.
        popup.scrollToItem(popup.item(N + 1),
                           QAbstractItemView.ScrollHint.PositionAtTop)
        self._extending_popup = False

    def _extend_popup_bottom(self) -> None:
        """Append _EXTEND_ROWS price rows below the current bottom of the list.

        Uses scrollToItem to keep the viewport stable after appending.
        """
        popup = self._popup
        if not popup:
            return
        old_count   = popup.count()
        bottom_item = popup.item(old_count - 1)
        if not bottom_item:
            return
        try:
            bottom_price = float(bottom_item.text().strip())
        except ValueError:
            return

        self._extending_popup = True
        N = self._EXTEND_ROWS
        for i in range(1, N + 1):
            price_val = bottom_price - i * self._tick_size
            if price_val <= 0:
                break
            popup.addItem(QListWidgetItem(self._fmt(self._snap(price_val))))

        # Scroll the old last row back to the bottom of the viewport
        popup.scrollToItem(popup.item(old_count - 1),
                           QAbstractItemView.ScrollHint.PositionAtBottom)
        self._extending_popup = False

    # ── Item selection ────────────────────────────────────────────────────────

    def _on_popup_picked(self, item: QListWidgetItem) -> None:
        if item.flags() & Qt.ItemFlag.ItemIsEnabled:
            self._edit.setText(item.text().strip())
        self._close_popup()
        self._edit.setFocus()

    def _on_popup_destroyed(self) -> None:
        self._popup = None
        self._extending_popup = False
        self._remove_app_filter()

    def _remove_app_filter(self) -> None:
        if self._app_filter_installed:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app:
                try:
                    app.removeEventFilter(self)
                except RuntimeError:
                    pass
            self._app_filter_installed = False

    def _close_popup(self) -> None:
        self._extending_popup = False
        self._remove_app_filter()
        if self._popup is not None:
            try:
                self._popup.destroyed.disconnect(self._on_popup_destroyed)
            except RuntimeError:
                pass
            self._popup.close()
            self._popup.deleteLater()
            self._popup = None




# ═══════════════════════════════════════════════════════════════════════════════
#  _TierSection
# ═══════════════════════════════════════════════════════════════════════════════

class _TierSection(QWidget):
    """Header (label + dollar display + toggle) + tier chips + custom input.

    Chip labels show the raw offset only (e.g. "5").
    The dollar display in the header shows  ``{title}: ${dollar*qty}``
    in a caller-specified colour whenever a chip is selected.
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
    _SS_INPUT = (
        "QLineEdit{background:#3a3a4a;color:#ccc;border:none;"
        "border-radius:4px;padding:2px 6px;font-size:11px}"
        "QLineEdit:focus{background:#4a4a5a;color:white}"
        "QLineEdit:disabled{background:#252535;color:#444;border:none}"
    )

    def __init__(self, title: str, tiers: list[dict],
                 enabled: bool = False,
                 label_color: str = Theme.ACCENT,
                 parent=None):
        super().__init__(parent)
        self._title_str    = title
        self._label_color  = label_color
        self._tiers:             list[dict]  = tiers
        self._dollar_per_tier:   list[float] = []
        self._qty:               int         = 1
        self._selected_idx:      int | None  = None
        self._chip_buttons:      list[QPushButton] = []
        self._input:             TickPriceInput | None = None
        self._toggle:            _ToggleSwitch | None = None
        self._state_lbl:         QLabel | None = None
        self._dollar_lbl:        QLabel | None = None
        self._chips_layout:      QHBoxLayout | None = None
        self._build_ui(title, enabled)

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self, title: str, enabled: bool) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 4)
        root.setSpacing(4)

        # Header row:  [title: $dollar]  [stretch]  [On/Off]  [toggle]
        # _dollar_lbl acts as the *combined* title + value label on the left:
        #   • no chip / no dollar  →  "Take profit"          (plain white)
        #   • chip selected, off   →  "Take profit: $500"    (dim grey preview)
        #   • chip selected, on    →  "Take profit: $500"    (accent green/red)
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        hdr.setContentsMargins(0, 0, 0, 0)

        self._dollar_lbl = QLabel(title)
        self._dollar_lbl.setStyleSheet(
            f"color: {Theme.TEXT}; font-size: 11px; font-weight: bold; border: none;")
        hdr.addWidget(self._dollar_lbl)
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

        # Snapshot dollar values parallel to chip indices
        self._dollar_per_tier = [t.get("dollar", 0.0) for t in self._tiers]

        # Pre-select chip 0 so the dollar label shows as a preview even before
        # the user explicitly clicks a chip.
        self._selected_idx = 0 if self._tiers else None

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

        self._input = TickPriceInput()
        self._input.setFixedHeight(self._CHIP_H)
        self._input.setFixedWidth(80)    # ~half the expanding width it used to take
        self._input.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._input.textChanged.connect(self._on_input_changed)
        lay.addWidget(self._input)

        self._update_chip_styles()
        self._update_dollar_display()

    # ── Dollar display ────────────────────────────────────────────────────────

    def _update_dollar_display(self) -> None:
        """Refresh the combined title+dollar label in the section header.

        Title text is always white.
        Only the dollar value is coloured:
          • No chip / dollar == 0  → plain white title, no value shown
          • Toggle OFF (preview)   → "Take profit: " white + "$500" dim grey
          • Toggle ON  (active)    → "Take profit: " white + "$500" green/red
        """
        if self._dollar_lbl is None:
            return

        # Label stylesheet is fixed at white — colour is applied via HTML span
        _base_ss = (f"color: {Theme.TEXT}; font-size: 11px; "
                    f"font-weight: bold; border: none;")

        # ── No valid selection or no dollar value ──────────────────────────
        if (self._selected_idx is None
                or self._selected_idx >= len(self._dollar_per_tier)):
            self._dollar_lbl.setStyleSheet(_base_ss)
            self._dollar_lbl.setText(self._title_str)
            return

        dollar = self._dollar_per_tier[self._selected_idx]

        if not dollar or dollar <= 0:
            self._dollar_lbl.setStyleSheet(_base_ss)
            self._dollar_lbl.setText(self._title_str)
            return

        # ── Colour only the value, keep the title white ────────────────────
        scaled    = dollar * self._qty
        toggle_on = self._toggle.is_on if self._toggle else True
        val_clr   = self._label_color if toggle_on else "#6B7280"

        self._dollar_lbl.setStyleSheet(_base_ss)
        self._dollar_lbl.setText(
            f'{self._title_str}: '
            f'<span style="color:{val_clr};font-weight:bold;">'
            f'{scaled:,.0f}</span>'
        )

    def set_qty(self, qty: int) -> None:
        """Update the quantity and refresh the dollar display (no chip rebuild)."""
        self._qty = qty
        self._update_dollar_display()

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_toggled(self, on: bool) -> None:
        self._state_lbl.setText("On" if on else "Off")
        if not on:
            # Keep _selected_idx so the dollar label stays as an informational
            # preview even when the section is disabled.  Clear any custom text.
            if self._input:
                self._input.blockSignals(True)
                self._input.clear()
                self._input.blockSignals(False)
        self._update_chip_styles()
        self._update_dollar_display()
        self.changed.emit()

    def _on_chip_clicked(self, idx: int) -> None:
        if self._toggle and not self._toggle.is_on:
            self._toggle.set_on(True)
            self._state_lbl.setText("On")
        self._selected_idx = idx
        if self._input:
            self._input.blockSignals(True)
            self._input.clear()
            self._input.blockSignals(False)
        self._update_chip_styles()
        self._update_dollar_display()
        self.changed.emit()

    def _on_input_changed(self, text: str) -> None:
        stripped = text.strip()
        if stripped:
            try:
                if float(stripped) > 0 and self._toggle and not self._toggle.is_on:
                    self._toggle.set_on(True)
                    self._state_lbl.setText("On")
            except ValueError:
                pass
            self._selected_idx = None
            self._update_chip_styles()
            self._update_dollar_display()
        self.changed.emit()

    def _update_chip_styles(self) -> None:
        toggle_on = self._toggle.is_on if self._toggle else True
        for i, btn in enumerate(self._chip_buttons):
            # Blue highlight only when the section is active (toggle ON).
            # While inactive the chip shows grey but the dollar preview is
            # still driven by _selected_idx.
            _apply_chip_style(btn, i == self._selected_idx and toggle_on)

    # ── Public API ────────────────────────────────────────────────────────────

    def reload_tiers(self, tiers: list[dict]) -> None:
        """Reload chip buttons (e.g. when symbol or preferences change)."""
        self._tiers = tiers
        self._build_chips(enabled=True)

    def set_market_price(self, price: float, tick_size: float = 0.25) -> None:
        """Forward the live market price and tick size to the TickPriceInput."""
        if self._input is not None:
            self._input.set_market_price(price, tick_size)

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
                # TickPriceInput returns an absolute price, not a tick offset
                return {"value": float(custom), "unit": "price", "label": "Custom"}
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
        # Restore last-active symbol; fall back to MNQ, then the first symbol.
        _saved_active = config_manager.load_active_order_entry_symbol()
        if _saved_active and _saved_active in self._symbols:
            self._active_symbol: str | None = _saved_active
        elif "MNQ" in self._symbols:
            self._active_symbol = "MNQ"
        else:
            self._active_symbol = self._symbols[0] if self._symbols else None
        self._active_qty: int = 1
        self._sym_buttons: dict[str, QPushButton] = {}

        self._setup_ui()
        self._rebuild_symbol_buttons()
        self._rebuild_qty_buttons()      # sets _active_qty to the correct default
        self._refresh_action_labels()
        # Sync the correct qty into the tier sections.
        # _setup_ui() builds them before _rebuild_qty_buttons() runs, so
        # they start with _qty=1.  Push the real default here (e.g. 100 for STK).
        self._pt_section.set_qty(self._active_qty)
        self._sl_section.set_qty(self._active_qty)

        # Auto-refresh when reference data changes (contract added/removed or
        # strategy tiers saved from the StrategyEditor tab).
        from src.core.reference_data import get_instance as _rd
        _rd().contracts_changed.connect(self._refresh_combo_items)
        _rd().strategy_tiers_changed.connect(self._reload_tier_sections)

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
        self._pt_section = _TierSection(
            "Take profit", pt_tiers, enabled=False,
            label_color="#3FB950")   # green
        root.addWidget(self._pt_section)

        # ── Stop Loss ─────────────────────────────────────────────────────────
        self._sl_section = _TierSection(
            "Stop loss", sl_tiers, enabled=False,
            label_color="#F85149")   # red
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
        from src.core.reference_data import get_instance as _rd
        self._combo.blockSignals(True)
        self._combo.clear()
        contracts = _rd().contracts
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
        from src.core.reference_data import get_instance as _rd
        contracts = _rd().contracts
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
        config_manager.save_active_order_entry_symbol(sym)
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
        config_manager.save_active_order_entry_symbol(sym)
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
                    if hasattr(self, "_pt_section"):
                        ts = self._tick_size_for_symbol(self._active_symbol)
                        self._pt_section.set_market_price(last, ts)
                        self._sl_section.set_market_price(last, ts)
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

        # Push the new default qty into the tier sections so their dollar
        # display is correct immediately (without waiting for the user to
        # click a qty chip).
        if hasattr(self, "_pt_section"):
            self._pt_section.set_qty(self._active_qty)
        if hasattr(self, "_sl_section"):
            self._sl_section.set_qty(self._active_qty)

    def _on_qty_preset(self, qty: int) -> None:
        self._active_qty = qty
        self._custom_qty.blockSignals(True)
        self._custom_qty.clear()
        self._custom_qty.blockSignals(False)
        for btn in self._qty_btn_group:
            _apply_chip_style(btn, int(btn.text()) == qty)
        self._refresh_action_labels()
        # Update the dollar display in tier headers without rebuilding chips
        if hasattr(self, "_pt_section"):
            self._pt_section.set_qty(qty)
        if hasattr(self, "_sl_section"):
            self._sl_section.set_qty(qty)

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
                # Update the dollar display in tier headers without rebuilding chips
                if hasattr(self, "_pt_section"):
                    self._pt_section.set_qty(qty)
                if hasattr(self, "_sl_section"):
                    self._sl_section.set_qty(qty)
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
        """Return (profit_take_tiers, stop_loss_tiers) for the active symbol.

        Lookup order
        ------------
        1. Exact symbol rows in strategy_tiers.db  (e.g. "NQ")
        2. Asset-class fallback rows ("FUT" or "STK") in strategy_tiers.db
        3. Preferences-based tiers (global per asset-class, set in Settings)

        Chip labels show the raw offset only (e.g. "5").
        The dollar amount is displayed in the section header by _TierSection.
        """
        sym = self._active_symbol or ""
        from src.core.reference_data import get_instance as _rd
        rd = _rd()

        pt = rd.get_tiers_for_symbol(sym, "take_profit")
        sl = rd.get_tiers_for_symbol(sym, "stop_loss")

        # Set offset-only chip labels for DB-sourced tiers
        if pt is not None:
            pt = self._format_tier_labels(pt)
        if sl is not None:
            sl = self._format_tier_labels(sl)

        # Fall back to preferences when ReferenceData has no rows
        if pt is None or sl is None:
            sec  = _sec_type_for(sym)
            key  = "trading_futures" if sec == "FUT" else "trading_stocks"
            prefs = config_manager.load_preferences()
            section = prefs.get(key, {})
            mult = rd.get_multiplier(sym)          # $/point  (e.g. 20 for NQ, 1 for stocks)
            tick = self._tick_size_for_symbol(sym)  # points/tick (e.g. 0.25 for NQ, 0.01 for stocks)
            if pt is None:
                raw = section.get("profit_take", self._TIER_FALLBACK)
                pt  = self._format_tier_labels(self._enrich_pref_tiers(raw, mult, tick))
            if sl is None:
                raw = section.get("stop_loss",   self._TIER_FALLBACK)
                sl  = self._format_tier_labels(self._enrich_pref_tiers(raw, mult, tick))

        return pt, sl

    @staticmethod
    def _format_tier_labels(tiers: list[dict]) -> list[dict]:
        """Return a copy of *tiers* with chip labels showing the offset only.

        E.g. offset=5 → label="5".  Falls back to the 'value' key so that
        preference-sourced tiers (which use 'value' instead of 'offset') are
        also formatted as plain numbers.  The dollar display lives in the
        section header (``_TierSection._dollar_lbl``) and is updated via
        ``set_qty()``.
        """
        result = []
        for t in tiers:
            raw = t.get("offset") or t.get("value", 0.0)
            try:
                raw = float(raw)
            except (TypeError, ValueError):
                raw = 0.0
            label = f"{raw:g}" if raw > 0 else t.get("label", "")
            result.append({**t, "label": label})
        return result

    @staticmethod
    def _enrich_pref_tiers(tiers: list[dict],
                           multiplier: float,
                           tick_size: float = 0.01) -> list[dict]:
        """Add ``offset`` and ``dollar`` fields to preference-sourced tiers.

        Preference tiers store the number of ticks in ``value``.  The
        per-unit dollar value is:

            dollar = offset_ticks × tick_size × multiplier

        Examples
        --------
        NVDA  (tick=0.01, mult=1):   20 ticks → 20×0.01×1  = $0.20 / share
        NQ    (tick=0.25, mult=20):  20 ticks → 20×0.25×20 = $100  / contract
        MNQ   (tick=0.25, mult=2):   20 ticks → 20×0.25×2  = $10   / contract
        """
        result = []
        for t in tiers:
            raw = t.get("offset") or t.get("value", 0.0)
            try:
                offset = float(raw)
            except (TypeError, ValueError):
                offset = 0.0
            dollar = round(offset * tick_size * multiplier, 4)
            result.append({**t, "offset": offset, "dollar": dollar})
        return result

    def _reload_tier_sections(self) -> None:
        """Reload PT/SL tier chips when the active symbol or preferences change."""
        if not hasattr(self, "_pt_section"):
            return
        pt_tiers, sl_tiers = self._load_tiers_for_active()
        self._pt_section.reload_tiers(pt_tiers)
        self._sl_section.reload_tiers(sl_tiers)
        # Sync current qty so the dollar display is correct on next chip click
        self._pt_section.set_qty(self._active_qty)
        self._sl_section.set_qty(self._active_qty)
        # Sync live price into the TickPriceInput spinners
        if self._last_price > 0:
            ts = self._tick_size_for_symbol(self._active_symbol or "")
            self._pt_section.set_market_price(self._last_price, ts)
            self._sl_section.set_market_price(self._last_price, ts)

    def _tick_size_for_symbol(self, sym: str) -> float:
        """Return the minimum tick size for *sym*.

        Priority
        --------
        1. ``tick_size`` / ``min_tick`` / ``minTick`` field in the saved
           contract row  (populated by ContractsView since the Tick column
           was added).
        2. Per-symbol lookup table for known futures (covers contracts saved
           before the Tick column existed).
        3. Coarse sec-type fallback: FUT → 0.25, everything else → 0.01.
        """
        # ── 1. Contract-row field ─────────────────────────────────────────────
        from src.core.reference_data import get_instance as _rd
        for c in _rd().contracts_by_symbol.get((sym or "").upper(), []):
            for key in ("tick_size", "min_tick", "minTick"):
                tick = c.get(key)
                if tick:
                    try:
                        v = float(tick)
                        if v > 0:
                            return v
                    except (TypeError, ValueError):
                        pass

        # ── 2. Known-futures symbol table ─────────────────────────────────────
        _FUT_TICK: dict[str, float] = {
            "MNQ": 0.25, "NQ":  0.25,
            "MES": 0.25, "ES":  0.25,
            "M2K": 0.1,  "RTY": 0.1,
            "MYM": 1.0,  "YM":  1.0,
            "MGC": 0.1,  "GC":  0.1,
            "MCL": 0.01, "CL":  0.01,
        }
        if sym:
            hit = _FUT_TICK.get(sym.strip().upper())
            if hit is not None:
                return hit

        # ── 3. Coarse sec-type fallback ───────────────────────────────────────
        sec = _sec_type_for(sym or "")
        return 0.25 if sec == "FUT" else 0.01

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
            from src.core.reference_data import get_instance as _rd
            _rd().reload_contracts()
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
        """Build market data params for a single symbol from the reference cache."""
        from src.core.reference_data import get_instance as _rd
        q = sym.upper()
        for c in _rd().contracts_by_symbol.get(q, []):
            conid = c.get("conid")
            if conid:
                return {
                    "conid":        int(conid),
                    "symbol":       sym,
                    "sec_type":     c.get("sec_type", ""),
                    "exchange":     c.get("exchange", "SMART"),
                    "currency":     c.get("currency", "USD"),
                    "local_symbol": c.get("local_symbol", ""),
                    "expiry":       c.get("expiry", ""),
                    "multiplier":   c.get("multiplier", ""),
                }
        return None

    def _on_bg_price_update(self, sym: str, last: float, bid: float, ask: float) -> None:
        """Receive a background price tick — update cache and display if active.

        Bid/ask are preserved from the last valid tick rather than being
        overwritten with 0 when a tick carries only a last-price update.
        IB delayed data (type 3) for futures often streams ``last`` ticks
        without bid/ask, which previously cleared the label on every tick.
        """
        # Retrieve the last-known cache entry so we can fall back to its
        # bid/ask when the incoming values are absent (0 or never received).
        cached = self._price_cache.get(sym.upper(), (0.0, 0.0, 0.0))

        # Only replace cached values when the incoming values are actually valid
        effective_last = last if last > 0 else cached[0]
        effective_bid  = bid  if bid  > 0 else cached[1]
        effective_ask  = ask  if ask  > 0 else cached[2]

        self._price_cache[sym.upper()] = (effective_last, effective_bid, effective_ask)

        if sym.upper() == (self._active_symbol or "").upper():
            if last > 0:
                self._last_price = last
                self._lbl_price.setText(f"{last:,.2f}")
                if hasattr(self, "_pt_section"):
                    ts = self._tick_size_for_symbol(sym)
                    self._pt_section.set_market_price(last, ts)
                    self._sl_section.set_market_price(last, ts)
            # Preserve last known bid/ask — only replace when newly valid
            if bid > 0:
                self._last_bid = bid
            if ask > 0:
                self._last_ask = ask
            self._update_bid_ask_display(self._last_bid, self._last_ask)
            self._update_bid_ask_buttons(self._last_bid, self._last_ask)

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
