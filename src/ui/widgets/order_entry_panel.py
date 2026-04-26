from __future__ import annotations
from src.core.enums import OrderAction, OrderType
from src.ui.theme import Theme

import logging
import math
from typing import Any

from PySide6.QtCore import QEvent, QRect, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QDoubleValidator,
    QFont,
    QFontMetrics,
    QIntValidator,
    QPainter,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.core import config_manager
from src.ui.widgets.order_panel_parts import DraggableChip

log = logging.getLogger(__name__)


# Qty presets per asset class
_FUT_QTYS = [1, 2, 3, 5]
_STK_QTYS = [10, 25, 50, 100, 500]

# Symbols whose ticker-strip %change column is hidden.  The index futures
# on CME (NQ / MNQ / ES / MES) have tiny daily % swings relative to the
# price magnitude and the user finds the column visually noisy — their
# absolute day change is still shown.  Stocks always show both.
_NO_PCT_CHANGE_SYMBOLS: set[str] = {"NQ", "MNQ", "ES", "MES"}
_FUT_DEFAULT = 1
_STK_DEFAULT = 100

# Popup ladder values for the qty input
_FUT_QTY_POPUP: list[int] = list(range(1, 101))          # 1 – 100
_STK_QTY_POPUP: list[int] = (
    list(range(1, 11))                                    # 1 – 10
    + list(range(20, 110, 10))                            # 20, 30, … 100
    + list(range(200, 1100, 100))                         # 200, 300, … 1000
    + list(range(1100, 10100, 100))                       # 1100, 1200, … 10000
    + list(range(11000, 51000, 1000))                     # 11000, 12000, … 50000
)

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


class _ActionButton(QPushButton):
    """Buy / Sell action button with a big primary line plus a small
    secondary line that summarises the currently-armed Take Profit /
    Stop Loss / Trail amounts.

    Two-line layout (centred) drawn manually so each line uses its own
    font.  QSS still drives the background / hover / pressed visuals
    via :meth:`QStyle.drawControl` — only the text rendering is custom.
    """

    _PRIMARY_PT = 13
    _SECONDARY_PT = 10

    def __init__(self, primary: str, colour: str, parent=None) -> None:
        super().__init__(parent)
        self._primary = primary
        self._secondary = ""
        self.setMinimumHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(f"""
            QPushButton {{
                background: {colour};
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 4px 8px;
            }}
            QPushButton:hover {{ background: {colour}; border: 1px solid #ffffff30; }}
            QPushButton:pressed {{ background: {colour}; border: 1px solid #ffffff60; }}
        """)

    def setPrimary(self, text: str) -> None:  # noqa: N802
        if self._primary == text:
            return
        self._primary = text
        self.update()

    def setSecondary(self, text: str) -> None:  # noqa: N802
        if self._secondary == text:
            return
        self._secondary = text
        self.update()

    # Override the inherited setText so an accidental setText() call
    # routes to setPrimary() rather than silently breaking the custom
    # paint (which never reads QPushButton.text()).
    def setText(self, text: str) -> None:  # noqa: N802
        self.setPrimary(text)

    def text(self) -> str:
        return self._primary

    def paintEvent(self, event):  # noqa: N802
        # Draw the QSS background only (suppress Qt's own text rendering
        # by zeroing opt.text), then paint our two-line text on top.
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        opt.text = ""
        painter = QPainter(self)
        try:
            self.style().drawControl(
                QStyle.ControlElement.CE_PushButton, opt, painter, self)
            painter.setPen(QColor("#ffffff"))

            r = self.rect()
            f_primary = QFont(self.font())
            f_primary.setPointSize(self._PRIMARY_PT)
            f_primary.setBold(True)
            fm_p = QFontMetrics(f_primary)

            if self._secondary:
                f_secondary = QFont(self.font())
                f_secondary.setPointSize(self._SECONDARY_PT)
                f_secondary.setBold(True)
                fm_s = QFontMetrics(f_secondary)
                gap = 2
                total_h = fm_p.height() + gap + fm_s.height()
                top = (r.height() - total_h) // 2

                painter.setFont(f_primary)
                painter.drawText(
                    QRect(r.x(), top, r.width(), fm_p.height()),
                    Qt.AlignmentFlag.AlignHCenter
                    | Qt.AlignmentFlag.AlignVCenter,
                    self._primary)
                painter.setFont(f_secondary)
                painter.drawText(
                    QRect(r.x(), top + fm_p.height() + gap,
                          r.width(), fm_s.height()),
                    Qt.AlignmentFlag.AlignHCenter
                    | Qt.AlignmentFlag.AlignVCenter,
                    self._secondary)
            else:
                painter.setFont(f_primary)
                painter.drawText(r, Qt.AlignmentFlag.AlignCenter, self._primary)
        finally:
            painter.end()


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
        self._edit.setPlaceholderText("Dist")
        self._edit.setFixedHeight(self._CHIP_H)
        self._edit.setMinimumWidth(0)
        self._edit.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._edit.setStyleSheet(
            "QLineEdit{background:#3a3a4a;color:#ccc;border:none;"
            "border-radius:6px 0 0 6px;padding:2px 6px;font-size:11px}"
            "QLineEdit:focus{background:#4a4a5a;color:white}"
            "QLineEdit:disabled{background:#252535;color:#444;border:none}"
        )
        # Reject non-numeric keystrokes; positive floats only
        _dv = QDoubleValidator(0.0, 1e12, 10, self._edit)
        _dv.setNotation(QDoubleValidator.Notation.StandardNotation)
        self._edit.setValidator(_dv)
        self._edit.textChanged.connect(self._relay_text)
        self._edit.returnPressed.connect(self._close_popup)
        self._edit.editingFinished.connect(self._snap_to_tick)
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
            "QWidget{background:#3a3a4a;border:none;border-radius:0 6px 6px 0}")
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

    def setText(self, text: str) -> None:  # noqa: N802 — Qt-style mixed case
        """Programmatic setter mirroring :class:`QLineEdit`'s API so
        ``_TierSection.restore_state`` can push a saved Dist value
        back into the input.  Emits ``textChanged`` for downstream
        listeners."""
        self._edit.setText(text)

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

    def _snap_to_tick(self) -> None:
        """If the field holds a non-tick-aligned number, round it to the nearest tick."""
        text = self._edit.text().strip()
        if not text or self._tick_size <= 0:
            return
        try:
            val = float(text)
        except ValueError:
            return
        snapped = self._snap(val)
        formatted = self._fmt(snapped)
        if formatted != text:
            self._edit.blockSignals(True)
            self._edit.setText(formatted)
            self._edit.blockSignals(False)
            self.textChanged.emit(formatted)

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
        popup.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
        """)

        # ── Populate tick-based offset rows (1*tick, 2*tick, ..., N*tick) ──
        # Large values at top, small values at bottom.  Scroll defaults to
        # the bottom so the smallest tick is visible on open.
        centre_item = None
        if self._tick_size > 0:
            current_text = self._edit.text().strip()
            try:
                current_val = self._snap(float(current_text)) if current_text else 0.0
            except ValueError:
                current_val = 0.0

            ladder_max = 5000  # number of tick multiples to populate
            for i in range(ladder_max, 0, -1):
                tick_val = self._snap(self._tick_size * i)
                item = QListWidgetItem(self._fmt(tick_val))
                if current_val > 0 and abs(tick_val - current_val) < self._tick_size / 2:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                    item.setForeground(QColor("#3FB950"))
                    centre_item = item
                popup.addItem(item)
        else:
            ph = QListWidgetItem("  Tick size unknown")
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

        # Scroll: highlight current value if any, otherwise show smallest tick
        # at the bottom.  Deferred to after show() so Qt has computed the
        # viewport height.
        if centre_item is not None:
            _ci = centre_item   # local ref for the lambda capture
            QTimer.singleShot(0, lambda: popup.scrollToItem(
                _ci, QAbstractItemView.ScrollHint.PositionAtCenter))
        else:
            QTimer.singleShot(0, popup.scrollToBottom)

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
#  _QtyInput  –  integer quantity field with ▲/▼ spinners + preset popup
# ═══════════════════════════════════════════════════════════════════════════════

class _QtyInput(QWidget):
    """Styled integer input (matches TickPriceInput look) with a preset popup."""

    _CHIP_H       = 28
    _BTN_W        = 18
    _VISIBLE_ROWS = 10
    _ROW_H        = 22

    textChanged   = Signal(str)
    returnPressed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._presets:              list[int]          = []
        self._popup:                QListWidget | None = None
        self._arrow_w:              QWidget     | None = None
        self._app_filter_installed: bool               = False
        self._win_filter_installed: bool               = False
        self._tracked_win:          QWidget     | None = None
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText("Qty")
        self._edit.setFixedHeight(self._CHIP_H)
        self._edit.setMinimumWidth(0)
        self._edit.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        # Reject non-numeric keystrokes at input time
        self._edit.setValidator(QIntValidator(1, 999_999_999, self._edit))
        self._edit.setStyleSheet(
            "QLineEdit{background:#3a3a4a;color:#ccc;border:none;"
            "border-radius:6px 0 0 6px;padding:2px 6px;font-size:11px}"
            "QLineEdit:focus{background:#4a4a5a;color:white}"
        )
        self._edit.textChanged.connect(self._relay_text)
        self._edit.returnPressed.connect(self._close_popup)
        self._edit.returnPressed.connect(self.returnPressed.emit)
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
        self._btn_up.setAutoRepeatDelay(400)
        self._btn_up.setAutoRepeatInterval(80)
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
            "QWidget{background:#3a3a4a;border:none;border-radius:0 6px 6px 0}")
        self._arrow_w.setLayout(arrow_col)

        lay.addWidget(self._edit, 1)
        lay.addWidget(self._arrow_w)

        self.setFixedHeight(self._CHIP_H)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_presets(self, values: list[int]) -> None:
        self._presets = values

    def text(self) -> str:
        return self._edit.text()

    def clear(self) -> None:
        self._edit.blockSignals(True)
        self._edit.clear()
        self._edit.blockSignals(False)

    def setText(self, txt: str) -> None:
        self._edit.setText(txt)

    def setPlaceholderText(self, txt: str) -> None:
        self._edit.setPlaceholderText(txt)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _relay_text(self, text: str) -> None:
        if not self.signalsBlocked():
            self.textChanged.emit(text)

    def _current_value(self) -> int | None:
        try:
            v = int(self._edit.text().strip())
            return v if v > 0 else None
        except ValueError:
            return None

    def _on_up(self) -> None:
        v = self._current_value()
        self._edit.setText("1" if v is None else str(v + 1))

    def _on_dn(self) -> None:
        v = self._current_value()
        if v is not None and v > 1:
            self._edit.setText(str(v - 1))

    # ── Popup ─────────────────────────────────────────────────────────────────

    def eventFilter(self, obj, event) -> bool:          # noqa: N802
        t = event.type()
        # Close when the main window is dragged/moved
        if obj is self._tracked_win and t == QEvent.Type.Move:
            self._close_popup()
            return False
        # Close on click outside the popup
        if (self._popup is not None and self._popup.isVisible()
                and t == QEvent.Type.MouseButtonPress
                and obj is not self._popup):
            try:
                gpos = event.globalPosition().toPoint()  # type: ignore[attr-defined]
            except AttributeError:
                gpos = event.globalPos()                  # type: ignore[attr-defined]
            if not self._popup.geometry().contains(gpos):
                self._close_popup()
                return False
        if obj is self._edit and t == QEvent.Type.MouseButtonPress:
            if self._popup is None or not self._popup.isVisible():
                QTimer.singleShot(50, self._show_popup)
        return False

    def _remove_app_filter(self) -> None:
        if self._app_filter_installed:
            from PySide6.QtWidgets import QApplication as _QApp
            app = _QApp.instance()
            if app:
                try:
                    app.removeEventFilter(self)
                except RuntimeError:
                    pass
            self._app_filter_installed = False

    def _remove_win_filter(self) -> None:
        if self._win_filter_installed and self._tracked_win is not None:
            try:
                self._tracked_win.removeEventFilter(self)
            except RuntimeError:
                pass
            self._win_filter_installed = False
            self._tracked_win = None

    def _close_popup(self) -> None:
        self._remove_app_filter()
        self._remove_win_filter()
        if self._popup is not None:
            try:
                self._popup.destroyed.disconnect(self._on_popup_destroyed)
            except RuntimeError:
                pass
            self._popup.close()
            self._popup.deleteLater()
            self._popup = None

    def _on_popup_destroyed(self) -> None:
        self._popup = None
        self._remove_app_filter()
        self._remove_win_filter()

    def _show_popup(self) -> None:
        if self._popup is not None and self._popup.isVisible():
            return
        self._close_popup()

        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication as _QApp

        popup = QListWidget()
        popup.setUniformItemSizes(True)
        popup.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        popup.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        popup.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        popup.setStyleSheet("""
            QListWidget {
                background: #1E222A; color: #C9D1D9;
                border: 1px solid #3a3f50; border-radius: 4px;
                font-size: 12px; outline: none;
            }
            QListWidget::item            { padding: 3px 14px; border: none; }
            QListWidget::item:selected,
            QListWidget::item:hover      { background: #0071e3; color: white; }
        """)

        # Large values at top, small values at bottom; initial view shows the bottom
        current     = self._current_value()
        centre_item: QListWidgetItem | None = None
        for v in reversed(self._presets):
            item = QListWidgetItem(str(v))
            if v == current:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
                item.setForeground(QColor("#3FB950"))
                centre_item = item
            popup.addItem(item)

        popup_w = 90
        popup_h = min(len(self._presets), self._VISIBLE_ROWS) * self._ROW_H + 8
        popup.setFixedSize(popup_w, popup_h)
        popup.itemClicked.connect(self._on_popup_item)
        popup.destroyed.connect(self._on_popup_destroyed)

        # Position popup next to the arrow widget, centred on the input row
        if self._arrow_w is not None:
            arrow_gx = self._arrow_w.mapToGlobal(QPoint(0, 0)).x()
        else:
            arrow_gx = self.mapToGlobal(QPoint(self.width() - self._BTN_W, 0)).x()
        widget_gcy    = self.mapToGlobal(QPoint(0, self.height() // 2)).y()
        global_origin = QPoint(arrow_gx, widget_gcy - popup_h // 2)

        wc = self.mapToGlobal(QPoint(self.width() // 2, self.height() // 2))
        screen = _QApp.screenAt(wc) or _QApp.primaryScreen()
        if screen:
            sg = screen.availableGeometry()
            gx = max(sg.left(), min(global_origin.x(), sg.right()  - popup_w))
            gy = max(sg.top(),  min(global_origin.y(), sg.bottom() - popup_h))
            global_origin = QPoint(gx, gy)

        popup.move(global_origin)
        popup.show()
        self._popup = popup

        # If a value is already selected scroll to it, otherwise show the bottom
        # (smallest values) so the user scrolls up to reach larger ones.
        if centre_item is not None:
            popup.scrollToItem(centre_item, QAbstractItemView.ScrollHint.PositionAtBottom)
        else:
            popup.scrollToBottom()

        # Track app-level clicks for outside-dismiss
        app = _QApp.instance()
        if app:
            app.installEventFilter(self)
            self._app_filter_installed = True

        # Track top-level window moves to dismiss popup
        top_win = self.window()
        if top_win and top_win is not self:
            top_win.installEventFilter(self)
            self._tracked_win          = top_win
            self._win_filter_installed = True

    def _on_popup_item(self, item: QListWidgetItem) -> None:
        self._edit.setText(item.text())
        self._close_popup()


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
    # Emitted when the user clicks the Take Profit / Stop Loss / Trail Stop
    # header button.  Carries no args — the parent (OrderEntryPanel) knows
    # which section it connected and queries get_value() for the offset.
    submit_requested = Signal()

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

    # Cap the number of preset chips displayed.  Three chips + the
    # custom Dist input + the title button is the most that fits on a
    # single row at the panel's typical width — additional tiers from
    # the user's strategy config are silently dropped here.
    _MAX_CHIPS = 3

    def __init__(self, title: str, tiers: list[dict],
                 label_color: str = Theme.ACCENT,
                 *,
                 short_title: str | None = None,
                 parent=None):
        super().__init__(parent)
        self._title_str    = title
        # Compact label shown alongside the dollar amount once a tier
        # is armed.  Defaults to the long title; sections like Stop
        # Loss / Trailing pass an abbreviation ("SL" / "TS") so the
        # button doesn't blow out width when the dollar suffix lands.
        self._short_title  = short_title or title
        self._label_color  = label_color
        self._tiers:             list[dict]  = tiers
        self._dollar_per_tier:   list[float] = []
        self._qty:               int         = 1
        self._multiplier:        float       = 1.0
        self._selected_idx:      int | None  = None
        self._chip_buttons:      list[QPushButton] = []
        self._input:             TickPriceInput | None = None
        self._dollar_lbl:        QPushButton | None = None
        self._chips_layout:      QHBoxLayout | None = None
        self._build_ui()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Single row:  [chip1] [chip2] [chip3] [Dist input] [Title button]
        # The toggle / On-Off label that used to live in a header row is
        # gone — selection happens implicitly when the user clicks a
        # chip or types into the Dist input, and the ARMED-strip
        # ``✕`` button reset() the row.
        self._chips_layout = QHBoxLayout(self)
        self._chips_layout.setContentsMargins(0, 0, 0, 4)
        self._chips_layout.setSpacing(4)
        self._build_chips()

    # Stretch factors for the row.  Chosen so the relative slice each
    # cell takes matches the design pixel widths at a typical ~900 px
    # panel: chip 138, Dist input 190, title button 270.
    # Total = 3 × 138 + 190 + 270 = 874 stretch units.
    _CHIP_STRETCH = 138
    _INPUT_STRETCH = 190
    _BUTTON_STRETCH = 270

    def _build_chips(self) -> None:
        """(Re)populate the chips row from ``self._tiers``.  Only the
        first ``_MAX_CHIPS`` tiers are rendered; the row ends with the
        custom Dist input and the ``TP`` / ``SL`` / ``TS`` title
        button.  Chip + input widths are 7/10 of an equal split so the
        title button gets the extra room — see the stretch constants
        above."""
        lay = self._chips_layout
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._chip_buttons.clear()
        self._selected_idx = None

        tiers = self._tiers[:self._MAX_CHIPS]
        # Snapshot dollar values parallel to chip indices.
        self._dollar_per_tier = [t.get("dollar", 0.0) for t in tiers]

        for i, tier in enumerate(tiers):
            label = tier.get("label", f"Tier {i + 1}")
            btn = QPushButton(label)
            btn.setFixedHeight(self._CHIP_H)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda chk, idx=i: self._on_chip_clicked(idx))
            self._chip_buttons.append(btn)
            lay.addWidget(btn, self._CHIP_STRETCH)

        # Custom Dist input.
        self._input = TickPriceInput()
        self._input.setFixedHeight(self._CHIP_H)
        self._input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._input.textChanged.connect(self._on_input_changed)
        lay.addWidget(self._input, self._INPUT_STRETCH)

        # Title / dollar button — right-most in the row.  Doubles as
        # the close-the-position submit button (clickable only when
        # something is armed).  Wider stretch factor so it absorbs the
        # 30% trimmed off the chips and Dist input.
        self._dollar_lbl = _make_btn(
            self._title_str, self._label_color, self._CHIP_H)
        self._dollar_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._dollar_lbl.clicked.connect(self._on_header_submit)
        self._dollar_lbl.setEnabled(False)
        lay.addWidget(self._dollar_lbl, self._BUTTON_STRETCH)

        self._update_chip_styles()
        self._update_dollar_display()

    # ── Dollar display ────────────────────────────────────────────────────────

    def _update_dollar_display(self) -> None:
        """Refresh the Take Profit / Stop Loss / Trail Stop button label.

        Shows just the title when nothing is selected; shows
        ``Title | $X`` once a chip or a positive Dist provides a value.
        """
        if self._dollar_lbl is None:
            return

        input_text = (self._input.text().strip()
                      if self._input is not None else "")
        has_chip = self._selected_idx is not None
        has_input = bool(input_text)
        active = has_chip or has_input
        self._dollar_lbl.setEnabled(active)

        if not active:
            self._dollar_lbl.setText(self._title_str)
            return

        # Per-unit dollar: custom Dist wins over chip selection.
        dollar = 0.0
        if input_text:
            try:
                dist = float(input_text)
                if dist > 0:
                    dollar = dist * self._multiplier
            except ValueError:
                pass
        if dollar <= 0 and has_chip and self._selected_idx < len(
                self._dollar_per_tier):
            dollar = self._dollar_per_tier[self._selected_idx]

        if dollar <= 0:
            self._dollar_lbl.setText(self._title_str)
            return

        scaled = int(round(dollar * self._qty))
        # Armed → use the compact label so the button fits the dollar
        # value without overflowing (e.g. ``"SL $20"`` instead of
        # ``"Stop Loss $20"``).
        self._dollar_lbl.setText(f"{self._short_title} ${scaled:,}")

    def set_qty(self, qty: int) -> None:
        """Update the quantity and refresh the dollar display (no chip rebuild)."""
        self._qty = qty
        self._update_dollar_display()

    def set_multiplier(self, multiplier: float) -> None:
        """Update the contract multiplier used for custom-distance dollar math."""
        self._multiplier = multiplier if multiplier and multiplier > 0 else 1.0
        self._update_dollar_display()

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_header_submit(self) -> None:
        """Title button click → fire ``submit_requested`` so the host
        (OrderEntryPanel) routes it as a close-the-position order.  No-op
        when nothing is armed; the button is also disabled in that
        state via ``_update_dollar_display`` so this is defensive."""
        if self.get_value() is None:
            return
        self.submit_requested.emit()

    def _on_chip_clicked(self, idx: int) -> None:
        # Click a chip while it's already selected → deselect (so the
        # user can disarm one section without resetting the others).
        if self._selected_idx == idx:
            self._selected_idx = None
        else:
            self._selected_idx = idx
            # Custom Dist and chip selection are mutually exclusive —
            # picking a chip clears any prior typed value.
            if self._input is not None:
                self._input.blockSignals(True)
                self._input.clear()
                self._input.blockSignals(False)
        self._update_chip_styles()
        self._update_dollar_display()
        self.changed.emit()

    def _on_input_changed(self, _text: str) -> None:
        # Typing a positive value into Dist supersedes chip selection;
        # an empty / non-positive value just clears the dollar preview.
        stripped = (self._input.text().strip()
                    if self._input is not None else "")
        try:
            val = float(stripped) if stripped else 0.0
        except ValueError:
            val = 0.0
        if val > 0 and self._selected_idx is not None:
            self._selected_idx = None
        self._update_chip_styles()
        self._update_dollar_display()
        self.changed.emit()

    def _update_chip_styles(self) -> None:
        for i, btn in enumerate(self._chip_buttons):
            _apply_chip_style(btn, i == self._selected_idx)

    # ── Public API ────────────────────────────────────────────────────────────

    def reload_tiers(self, tiers: list[dict]) -> None:
        """Reload chip buttons (e.g. when symbol or preferences change)."""
        self._tiers = tiers
        self._build_chips()

    def set_market_price(self, price: float, tick_size: float = 0.25) -> None:
        """Forward the live market price and tick size to the TickPriceInput."""
        if self._input is not None:
            self._input.set_market_price(price, tick_size)

    @property
    def is_enabled(self) -> bool:
        """Section is "armed" whenever the user has expressed a choice
        — either a chip is selected or the custom Dist input holds a
        positive number.  There's no separate on/off toggle anymore."""
        if self._selected_idx is not None:
            return True
        text = (self._input.text().strip()
                if self._input is not None else "")
        if not text:
            return False
        try:
            return float(text) > 0
        except ValueError:
            return False

    def reset(self) -> None:
        """Return the section to its pristine, unselected state: no chip
        highlighted, custom Dist cleared.  Used by the ARMED-strip close
        button to disarm everything in one gesture."""
        had_state = (self._selected_idx is not None
                     or (self._input is not None
                         and bool(self._input.text().strip())))
        if self._input is not None:
            self._input.blockSignals(True)
            self._input.clear()
            self._input.blockSignals(False)
        self._selected_idx = None
        self._update_chip_styles()
        self._update_dollar_display()
        if had_state:
            # Only emit when something actually changed — avoids
            # spurious refreshes that ripple into the ARMED strip.
            self.changed.emit()

    # ── Per-symbol state capture / restore ───────────────────────────────────

    def capture_state(self) -> dict:
        """Snapshot the section's current armed selection so the host
        can stash it away (e.g. keyed by symbol) and restore it later
        via :meth:`restore_state`.  Lightweight by design — only the
        chip index and the custom Dist text are persisted."""
        return {
            "selected_idx": self._selected_idx,
            "input_text": (
                self._input.text() if self._input is not None else ""),
        }

    def restore_state(self, state: dict | None) -> None:
        """Apply a snapshot produced by :meth:`capture_state`.  Pass
        ``None`` (or a falsy dict) to reset the section to its
        pristine state — same as calling :meth:`reset`."""
        if not state:
            self.reset()
            return
        idx = state.get("selected_idx")
        text = state.get("input_text", "")
        # Defensive: a chip index from the previous symbol's tier list
        # may exceed the new tier count.  Drop it if so.
        if idx is not None and (
                not isinstance(idx, int) or idx < 0
                or idx >= len(self._tiers)):
            idx = None
        # Push the snapshot in without bouncing through ``changed`` until
        # both fields are settled, then emit once at the end.
        self._selected_idx = idx
        if self._input is not None:
            self._input.blockSignals(True)
            self._input.clear()
            if text:
                self._input.setText(text)
            self._input.blockSignals(False)
        self._update_chip_styles()
        self._update_dollar_display()
        self.changed.emit()

    def get_value(self) -> dict | None:
        """Return the selected tier as ``{"value", "unit", "label"}``,
        or None when the section is unarmed."""
        if not self.is_enabled:
            return None
        custom = (self._input.text().strip()
                  if self._input is not None else "")
        if custom:
            try:
                return {"value": float(custom), "unit": "price",
                        "label": "Custom"}
            except ValueError:
                return None
        if (self._selected_idx is not None
                and self._selected_idx < len(self._tiers)):
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

    # Emitted when user clicks the Take Profit / Stop Loss header button.
    # Payload:
    #   {"side": "tp"|"sl", "symbol": str, "qty": int,
    #    "offset_pts": float, "last_price": float}
    # The consumer (FlashTradeView) queries each checked account for the
    # current position, sizes the order to min(|pos|, qty), computes the
    # target price from last_price ± offset, and submits a LMT (tp) or
    # STP/STP LMT (sl) exit order.  No-op for zero-position accounts.
    close_tier_requested = Signal(dict)

    def __init__(self, active_threads: dict | None = None, parent=None):
        super().__init__(parent)
        self._active_threads: dict = active_threads if active_threads is not None else {}
        self._connected_worker = None  # track worker for TWS lookup
        self._bg_worker = None         # track worker for background price streams
        # sym → (last, bid, ask)
        self._price_cache: dict[str, tuple[float, float, float]] = {}
        # sym → prior-session close, used by the ticker strip to compute
        # day change and %change.  Populated by bg_price_updated ticks.
        self._close_cache: dict[str, float] = {}
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
        # In-memory cache of the user's TP / SL / Trail selections per
        # symbol — keyed by uppercase symbol, value is a dict
        # ``{"pt": <_TierSection.capture_state>, "sl": ..., "tr": ...}``.
        # Lives only for the lifetime of the app (deliberately NOT
        # persisted to ``app_config.json``) so a fresh launch always
        # starts every symbol unarmed, while in-session symbol
        # switches preserve whatever the user had picked.
        self._tier_state_by_symbol: dict[str, dict] = {}

        self._setup_ui()
        self._rebuild_symbol_buttons()
        self._rebuild_qty_buttons()      # sets _active_qty to the correct default
        self._refresh_action_labels()
        # Sync the correct qty into the tier sections.
        # _setup_ui() builds them before _rebuild_qty_buttons() runs, so
        # they start with _qty=1.  Push the real default here (e.g. 100 for STK).
        self._pt_section.set_qty(self._active_qty)
        self._sl_section.set_qty(self._active_qty)
        self._tr_section.set_qty(self._active_qty)

        # Push the contract multiplier + tick size for the initial active symbol
        # so the custom Dist input calculates dollars correctly before the user
        # first clicks a different symbol chip (which would call
        # _reload_tier_sections).
        from src.core.reference_data import get_instance as _rd
        _init_mult = _rd().get_multiplier(self._active_symbol or "") or 1.0
        self._pt_section.set_multiplier(_init_mult)
        self._sl_section.set_multiplier(_init_mult)
        self._tr_section.set_multiplier(_init_mult)
        _init_ts = self._tick_size_for_symbol(self._active_symbol or "")
        self._pt_section.set_market_price(self._last_price, _init_ts)
        self._sl_section.set_market_price(self._last_price, _init_ts)
        self._tr_section.set_market_price(self._last_price, _init_ts)

        # Auto-refresh when reference data changes (contract added/removed or
        # strategy tiers saved from the StrategyEditor tab).
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

        # ── Active-symbol ticker strip ────────────────────────────────────────
        # Compact single-row strip replaces the old "Add symbol (look up)…"
        # combo + two-line price block so the top takes minimal space:
        #   NQ  26,698.75  26,698.25 26,699.50  -126.75 -0.47%  [+]
        #   sym last       bid(g)    ask(r)     chg     chg%    add
        # No stretch between the five data columns — they stay tightly
        # packed on the left; the "+" button is pinned to the far right.
        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        top_row.setContentsMargins(0, 0, 0, 0)

        _ticker_dim = f"color: {Theme.TEXT_DIM}; border: none;"
        # Slightly smaller than _SYMBOL_FONT / _PRICE_FONT so the whole
        # strip can fit the narrow Order Entry column without wrapping.
        _sym_font = QFont("Helvetica Neue, Arial", 12)
        _sym_font.setWeight(QFont.Weight.Bold)
        _price_font = QFont("Menlo, Consolas, monospace", 12)
        _price_font.setWeight(QFont.Weight.Bold)
        _ticker_font = QFont("Menlo, Consolas, monospace", 11)
        _ticker_font.setWeight(QFont.Weight.Bold)

        self._lbl_symbol = QLabel("")
        self._lbl_symbol.setFont(_sym_font)
        self._lbl_symbol.setStyleSheet(
            f"color: {Theme.TEXT}; border: none;")

        self._lbl_price = QLabel("")
        self._lbl_price.setFont(_price_font)
        self._lbl_price.setStyleSheet(
            f"color: {Theme.TEXT}; border: none;")

        self._lbl_bid = QLabel("")
        self._lbl_bid.setFont(_ticker_font)
        self._lbl_bid.setStyleSheet(
            f"color: {Theme.CLR_BUY}; border: none;")

        self._lbl_ask = QLabel("")
        self._lbl_ask.setFont(_ticker_font)
        self._lbl_ask.setStyleSheet(
            f"color: {Theme.CLR_SELL}; border: none;")

        # Day change and %change vs prior-session close.  Coloured per
        # sign in _refresh_price_change_display.
        self._lbl_change = QLabel("")
        self._lbl_change.setFont(_ticker_font)
        self._lbl_change.setStyleSheet(_ticker_dim)

        self._lbl_change_pct = QLabel("")
        self._lbl_change_pct.setFont(_ticker_font)
        self._lbl_change_pct.setStyleSheet(_ticker_dim)

        # Plus button → opens a dropdown listing available contracts from
        # ReferenceData that aren't already on the symbol-chip bar.
        self._btn_add_symbol = QPushButton("+")
        self._btn_add_symbol.setFixedSize(22, 22)
        self._btn_add_symbol.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add_symbol.setToolTip("Add symbol")
        self._btn_add_symbol.setStyleSheet(
            f"QPushButton{{background:{Theme.BG_INPUT};color:{Theme.TEXT};"
            f"border:1px solid {Theme.BORDER};border-radius:4px;"
            f"font-size:14px;font-weight:bold;padding:0}}"
            f"QPushButton:hover{{border-color:{Theme.ACCENT}}}")
        self._btn_add_symbol.clicked.connect(self._show_add_symbol_menu)

        top_row.addWidget(self._lbl_symbol)
        top_row.addWidget(self._lbl_price)
        top_row.addSpacing(6)
        top_row.addWidget(self._lbl_bid)
        top_row.addWidget(self._lbl_ask)
        top_row.addSpacing(6)
        top_row.addWidget(self._lbl_change)
        top_row.addWidget(self._lbl_change_pct)
        top_row.addStretch(1)
        top_row.addWidget(self._btn_add_symbol)
        root.addLayout(top_row)

        # The lookup combo is kept as a hidden child widget because other
        # code paths still reference ``self._combo`` (TWS fallback status
        # messages via setPlaceholderText, _add_symbol's setCurrentText
        # reset, etc.).  It is NOT added to any layout, so it never
        # occupies screen space.  The "+" button's QMenu is the only
        # user-visible way to add symbols now.
        self._combo = QComboBox()
        self._combo.setEditable(True)
        self._combo.setMaxVisibleItems(20)
        self._combo.setStyleSheet(_COMBO_SS)
        self._combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo.lineEdit().setPlaceholderText("Add symbol (look up)..")
        self._combo.activated.connect(self._on_combo_activated)
        self._combo.lineEdit().returnPressed.connect(self._on_return_pressed)
        self._combo.hide()
        self._refresh_combo_items()

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

        # Tighten the qty-chips ↔ ARMED banner ↔ Buy/Sell action grid
        # gap from the original 12 px down to 4 px so the whole
        # action stack reads as one connected block.
        root.addSpacing(4)

        # ── Pre-build tier sections so the ARMED strip below can
        # subscribe to their ``changed`` signal.  The widgets stay
        # detached for now and are added to ``root`` later, at their
        # normal position below the position-management grid.
        pt_tiers, sl_tiers, tr_tiers = self._load_tiers_for_active()
        self._pt_section = _TierSection(
            "Take Profit", pt_tiers, label_color=Theme.CLR_BUY,
            short_title="TP")
        self._pt_section.submit_requested.connect(
            lambda: self._on_tier_submit("tp"))
        self._sl_section = _TierSection(
            "Stop Loss", sl_tiers, label_color=Theme.CLR_SELL,
            short_title="SL")
        self._sl_section.submit_requested.connect(
            lambda: self._on_tier_submit("sl"))
        self._tr_section = _TierSection(
            "Trailing", tr_tiers, label_color=Theme.CLR_FLATTEN,
            short_title="TS")

        # The TP / SL / Trail amounts that used to live in a separate
        # ARMED banner above the buttons are now drawn directly on each
        # Buy / Sell button as a small second line — see
        # ``_refresh_action_labels`` and :class:`_ActionButton`.
        for _sec in (self._pt_section, self._sl_section, self._tr_section):
            _sec.changed.connect(self._refresh_action_labels)

        root.addSpacing(4)

        # ── Action buttons (4×2) ─────────────────────────────────────────────
        action_grid = QGridLayout()
        action_grid.setSpacing(4)
        action_grid.setContentsMargins(0, 0, 0, 0)
        self._btn_buy_mkt = _ActionButton("Buy @MKT", Theme.CLR_BUY)
        self._btn_sell_mkt = _ActionButton("Sell @MKT", Theme.CLR_SELL)
        self._btn_buy_mid = _ActionButton("Buy @Mid", Theme.CLR_BUY)
        self._btn_sell_mid = _ActionButton("Sell @Mid", Theme.CLR_SELL)
        self._btn_buy_bid_lmt = _ActionButton("Buy @Bid Limit", Theme.CLR_BUY)
        self._btn_sell_ask_lmt = _ActionButton(
            "Sell @Ask Limit", Theme.CLR_SELL)
        self._btn_buy_bid = _ActionButton("Buy @Bid +1t", Theme.CLR_BUY)
        self._btn_sell_ask = _ActionButton("Sell @Ask -1t", Theme.CLR_SELL)
        action_grid.addWidget(self._btn_buy_mkt, 0, 0)
        action_grid.addWidget(self._btn_sell_mkt, 0, 1)
        action_grid.addWidget(self._btn_buy_mid, 1, 0)
        action_grid.addWidget(self._btn_sell_mid, 1, 1)
        action_grid.addWidget(self._btn_buy_bid_lmt, 2, 0)
        action_grid.addWidget(self._btn_sell_ask_lmt, 2, 1)
        action_grid.addWidget(self._btn_buy_bid, 3, 0)
        action_grid.addWidget(self._btn_sell_ask, 3, 1)

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

        # ── Take Profit / Stop Loss / Trail Stop ─────────────────────────────
        # The widgets themselves were created earlier (so the ARMED strip
        # could connect to their ``changed`` signal); here we just lay
        # them out at the bottom of the root column.
        root.addWidget(self._pt_section)
        root.addWidget(self._sl_section)
        root.addWidget(self._tr_section)

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

    def _on_tier_submit(self, side: str) -> None:
        """Take Profit / Stop Loss header button clicked → emit
        ``close_tier_requested`` with the active symbol, selected tier
        offset, last price and user's qty.  FlashTradeView fans this out
        to each checked account, reads the live position there, and
        submits a LMT (tp) or STP/STP LMT (sl) exit order sized to
        min(|position|, qty).  No-op for zero-position accounts.
        """
        sym = self._active_symbol
        if not sym:
            log.warning(
                "OrderEntryPanel: %s header clicked but no active symbol",
                side.upper())
            return

        section = self._pt_section if side == "tp" else self._sl_section
        tier = section.get_value()
        if tier is None:
            # Button is disabled in this state; defensive no-op.
            return

        # Offset lives in tier["value"]; unit "price" means points directly,
        # "ticks" means multiply by the contract's tick size.  The chip
        # loaders tag tier dicts with unit="price" by default, so for
        # typical usage this is just tier["value"].
        raw_val = float(tier.get("value", 0.0))
        unit = tier.get("unit", "price")
        if unit == "ticks":
            tick = self._tick_size_for_symbol(sym)
            offset_pts = raw_val * (tick if tick > 0 else 0.01)
        else:
            offset_pts = raw_val

        if offset_pts <= 0:
            log.warning(
                "OrderEntryPanel: %s submit ignored — non-positive "
                "offset %.4f", side.upper(), offset_pts)
            return

        self.close_tier_requested.emit({
            "side":       side,
            "symbol":     sym,
            "qty":        int(self._active_qty),
            "offset_pts": offset_pts,
            "last_price": float(self._last_price),
        })

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
            btn = self._make_chip(sym, "STK")
            self._sym_grid.addWidget(btn, r, c)
        if stk_syms:
            row += (len(stk_syms) - 1) // self._CHIP_COLS + 1

        # ── FUT rows ──
        for i, sym in enumerate(fut_syms):
            r = row + i // self._CHIP_COLS
            c = i % self._CHIP_COLS
            btn = self._make_chip(sym, "FUT")
            self._sym_grid.addWidget(btn, r, c)

        self._update_symbol_display()

    def _make_chip(self, sym: str, sec_type: str) -> QPushButton:
        """Create a symbol chip button (draggable within its own group)."""
        btn = DraggableChip(sym, sec_type, self._on_chip_drop)
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

    def _on_chip_drop(self, src_sym: str, dst_sym: str) -> None:
        """Move *src_sym* immediately before *dst_sym* in self._symbols,
        persist, and rebuild the grid.  DraggableChip already enforces the
        same-group constraint, so STK chips stay in the STK rows and FUT
        chips stay in the FUT rows after rebuild."""
        if src_sym not in self._symbols or dst_sym not in self._symbols:
            return
        syms = list(self._symbols)
        syms.remove(src_sym)
        dst_idx = syms.index(dst_sym)
        syms.insert(dst_idx, src_sym)
        if syms == self._symbols:
            return
        self._symbols = syms
        config_manager.save_order_entry_symbols(self._symbols)
        self._rebuild_symbol_buttons()

    def _on_symbol_clicked(self, sym: str) -> None:
        # Snapshot the outgoing symbol's TP / SL / TS selection so that
        # coming back to it restores the same arming.  Skipped on the
        # very first click (no prior active symbol).
        self._capture_tier_state(self._active_symbol)
        self._active_symbol = sym
        config_manager.save_active_order_entry_symbol(sym)
        for s, btn in self._sym_buttons.items():
            _apply_chip_style(btn, s == sym)
        self._update_symbol_display()
        self._rebuild_qty_buttons()
        self._refresh_action_labels()
        self._reload_tier_sections()
        # Restore (or reset) the new symbol's previously-stored arming.
        self._restore_tier_state(sym)

    def _capture_tier_state(self, sym: str | None) -> None:
        if not sym or not hasattr(self, "_pt_section"):
            return
        self._tier_state_by_symbol[sym.upper()] = {
            "pt": self._pt_section.capture_state(),
            "sl": self._sl_section.capture_state(),
            "tr": self._tr_section.capture_state(),
        }

    def _restore_tier_state(self, sym: str | None) -> None:
        if not hasattr(self, "_pt_section"):
            return
        snapshot = self._tier_state_by_symbol.get(
            sym.upper()) if sym else None
        if snapshot:
            self._pt_section.restore_state(snapshot.get("pt"))
            self._sl_section.restore_state(snapshot.get("sl"))
            self._tr_section.restore_state(snapshot.get("tr"))
        else:
            # No prior arming for this symbol — start fresh.
            self._pt_section.reset()
            self._sl_section.reset()
            self._tr_section.reset()

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
                        self._tr_section.set_market_price(last, ts)
                else:
                    self._lbl_price.setText("")
                self._update_bid_ask_display(bid, ask)
            else:
                self._lbl_price.setText("")
                self._update_bid_ask_display(0.0, 0.0)
                self._last_price = 0.0
                self._last_bid = 0.0
                self._last_ask = 0.0

            # Ensure background subscriptions are active
            self._ensure_bg_subscriptions()
        else:
            self._lbl_symbol.setText("")
            self._lbl_price.setText("")
            self._update_bid_ask_display(0.0, 0.0)
            self._last_price = 0.0
            self._last_bid = 0.0
            self._last_ask = 0.0
        self._refresh_price_change_display()

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
        """Rebuild qty row: preset chips + _QtyInput, sized to the asset class."""
        while self._qty_layout.count():
            item = self._qty_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        sec = _sec_type_for(self._active_symbol or "")
        if sec == "STK":
            presets    = _STK_QTYS
            popup_vals = _STK_QTY_POPUP
            default    = _STK_DEFAULT
        else:
            presets    = _FUT_QTYS
            popup_vals = _FUT_QTY_POPUP
            default    = _FUT_DEFAULT

        self._active_qty = default
        self._qty_btn_group: list[QPushButton] = []

        for q in presets:
            btn = QPushButton(str(q))
            btn.setFixedHeight(28)
            btn.setMinimumWidth(36)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            _apply_chip_style(btn, q == default)
            btn.clicked.connect(lambda checked, v=q: self._on_qty_preset(v))
            self._qty_btn_group.append(btn)
            self._qty_layout.addWidget(btn)

        self._custom_qty = _QtyInput()
        self._custom_qty.set_presets(popup_vals)
        self._custom_qty.textChanged.connect(self._on_custom_qty)
        self._custom_qty.returnPressed.connect(self._on_custom_qty_enter)
        self._qty_layout.addWidget(self._custom_qty)

        if hasattr(self, "_pt_section"):
            self._pt_section.set_qty(self._active_qty)
        if hasattr(self, "_sl_section"):
            self._sl_section.set_qty(self._active_qty)
        if hasattr(self, "_tr_section"):
            self._tr_section.set_qty(self._active_qty)

    def _on_qty_preset(self, qty: int) -> None:
        self._active_qty = qty
        self._custom_qty.blockSignals(True)
        self._custom_qty.clear()
        self._custom_qty.blockSignals(False)
        for btn in self._qty_btn_group:
            _apply_chip_style(btn, int(btn.text()) == qty)
        self._refresh_action_labels()
        if hasattr(self, "_pt_section"):
            self._pt_section.set_qty(qty)
        if hasattr(self, "_sl_section"):
            self._sl_section.set_qty(qty)
        if hasattr(self, "_tr_section"):
            self._tr_section.set_qty(qty)

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
                if hasattr(self, "_pt_section"):
                    self._pt_section.set_qty(qty)
                if hasattr(self, "_sl_section"):
                    self._sl_section.set_qty(qty)
                if hasattr(self, "_tr_section"):
                    self._tr_section.set_qty(qty)
                return
        except ValueError:
            pass
        # Invalid (non-numeric or <= 0) → reset qty state to default
        self._reset_qty_to_default()

    def _reset_qty_to_default(self) -> None:
        """Restore qty state to the asset-class default and refresh UI."""
        sec = _sec_type_for(self._active_symbol or "")
        default = _STK_DEFAULT if sec == "STK" else _FUT_DEFAULT
        self._active_qty = default
        for btn in self._qty_btn_group:
            _apply_chip_style(btn, int(btn.text()) == default)
        self._refresh_action_labels()
        if hasattr(self, "_pt_section"):
            self._pt_section.set_qty(default)
        if hasattr(self, "_sl_section"):
            self._sl_section.set_qty(default)
        if hasattr(self, "_tr_section"):
            self._tr_section.set_qty(default)

    def _on_custom_qty_enter(self) -> None:
        """Clear the custom qty field on Enter if its content is invalid."""
        text = self._custom_qty.text().strip()
        try:
            if int(text) > 0:
                return
        except ValueError:
            pass
        self._custom_qty.blockSignals(True)
        self._custom_qty.clear()
        self._custom_qty.blockSignals(False)

    # ── Action label refresh ──────────────────────────────────────────────────

    def _refresh_action_labels(self) -> None:
        q = self._active_qty
        self._btn_buy_mkt.setPrimary(f"Buy {q} @MKT")
        self._btn_sell_mkt.setPrimary(f"Sell {q} @MKT")
        self._btn_buy_mid.setPrimary(f"Buy {q} @Mid")
        self._btn_sell_mid.setPrimary(f"Sell {q} @Mid")
        self._btn_buy_bid.setPrimary(f"Buy {q} @Bid +1t")
        self._btn_sell_ask.setPrimary(f"Sell {q} @Ask -1t")
        self._btn_buy_bid_lmt.setPrimary(f"Buy {q} @Bid Limit")
        self._btn_sell_ask_lmt.setPrimary(f"Sell {q} @Ask Limit")
        # Secondary line — shows TP / SL / Trail amounts armed for this
        # bracket.  Hidden when nothing is armed.
        secondary = self._format_armed_summary()
        for b in (self._btn_buy_mkt, self._btn_sell_mkt,
                  self._btn_buy_mid, self._btn_sell_mid,
                  self._btn_buy_bid, self._btn_sell_ask,
                  self._btn_buy_bid_lmt, self._btn_sell_ask_lmt):
            b.setSecondary(secondary)

    def _format_armed_summary(self) -> str:
        """Build the small second-line text shown on the Buy/Sell action
        buttons, e.g. ``"+$40  -$80  ↗ 50t"``.  Empty string when no
        tier is currently armed."""
        parts: list[str] = []
        tp = self._tier_amount(self._pt_section)
        if tp is not None:
            parts.append(f"+${tp:,}")
        sl = self._tier_amount(self._sl_section)
        if sl is not None:
            parts.append(f"-${sl:,}")
        tr_text = self._trail_text(self._tr_section)
        if tr_text is not None:
            parts.append(f"↗ {tr_text}")
        return "  ".join(parts)

    @staticmethod
    def _tier_amount(section: "_TierSection") -> int | None:
        """Return the section's current armed dollar amount (qty-scaled)
        as an integer, or None when nothing is armed.  Mirrors the math
        in :meth:`_TierSection._update_dollar_display`."""
        if not section.is_enabled:
            return None
        dollar = 0.0
        input_text = (section._input.text().strip()
                      if section._input is not None else "")
        if input_text:
            try:
                dist = float(input_text)
                if dist > 0:
                    dollar = dist * section._multiplier
            except ValueError:
                pass
        if dollar <= 0 and section._selected_idx is not None and (
                section._selected_idx < len(section._dollar_per_tier)):
            dollar = section._dollar_per_tier[section._selected_idx]
        if dollar <= 0:
            return None
        return int(round(dollar * section._qty))

    @staticmethod
    def _trail_text(section: "_TierSection") -> str | None:
        """Format the trail tier as ``"50t"`` (ticks) or ``"$1,500"`` —
        whichever unit the active tier carries.  Returns None when the
        section is unarmed."""
        if not section.is_enabled:
            return None
        v = section.get_value()
        if not v:
            return None
        value = v.get("value", 0)
        unit = v.get("unit", "ticks")
        try:
            fv = float(value)
        except (TypeError, ValueError):
            return None
        if fv <= 0:
            return None
        if unit in ("ticks", "cents"):
            # Drop the trailing .0 for integer tick counts.
            if fv.is_integer():
                return f"{int(fv)}t"
            return f"{fv:g}t"
        if unit == "%":
            return f"{fv:g}%"
        # "amt" / "price" / dollar units.
        if fv.is_integer():
            return f"${int(fv):,}"
        return f"${fv:,.2f}"

    # ── Tier helpers ──────────────────────────────────────────────────────────

    _TIER_FALLBACK = [
        {"label": "Tier 1", "value": 20,  "unit": "price"},
        {"label": "Tier 2", "value": 40,  "unit": "price"},
        {"label": "Tier 3", "value": 80,  "unit": "price"},
        {"label": "Tier 4", "value": 160, "unit": "price"},
    ]

    def _load_tiers_for_active(self) -> tuple[list[dict], list[dict], list[dict]]:
        """Return (profit_take_tiers, stop_loss_tiers, trailing_tiers) for the
        active symbol.

        Lookup order
        ------------
        1. Exact symbol rows in strategy_tiers.db  (e.g. "NQ")
        2. Asset-class fallback rows ("FUT" or "STK") in strategy_tiers.db
        3. Preferences-based tiers (global per asset-class, set in Settings).
           Trailing has no preferences entry — falls back to ``_TIER_FALLBACK``.

        Chip labels show the raw offset only (e.g. "5").
        The dollar amount is displayed in the section header by _TierSection.
        """
        sym = self._active_symbol or ""
        from src.core.reference_data import get_instance as _rd
        rd = _rd()

        pt = rd.get_tiers_for_symbol(sym, "take_profit")
        sl = rd.get_tiers_for_symbol(sym, "stop_loss")
        tr = rd.get_tiers_for_symbol(sym, "trailing")

        # Set offset-only chip labels for DB-sourced tiers
        if pt is not None:
            pt = self._format_tier_labels(pt)
        if sl is not None:
            sl = self._format_tier_labels(sl)
        if tr is not None:
            tr = self._format_tier_labels(tr)

        # Fall back to preferences when ReferenceData has no rows
        if pt is None or sl is None or tr is None:
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
            if tr is None:
                # Trailing has no preferences key; use the generic fallback.
                tr  = self._format_tier_labels(
                    self._enrich_pref_tiers(self._TIER_FALLBACK, mult, tick))

        return pt, sl, tr

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

        Preference tiers store the *price-point* offset in ``value`` (matching
        the StrategyEditor's "val = offset × multiplier" semantics — the
        stored number is points, not ticks).  Dollar per contract is:

            dollar = offset_points × multiplier

        Examples
        --------
        NVDA  (mult=1):   20 pts → 20 × 1  = $20  / share (notional distance)
        NQ    (mult=20):  20 pts → 20 × 20 = $400 / contract
        MNQ   (mult=2):   20 pts → 20 × 2  = $40  / contract
        """
        result = []
        for t in tiers:
            raw = t.get("offset") or t.get("value", 0.0)
            try:
                offset = float(raw)
            except (TypeError, ValueError):
                offset = 0.0
            dollar = round(offset * multiplier, 4)
            result.append({**t, "offset": offset, "dollar": dollar})
        return result

    def _reload_tier_sections(self) -> None:
        """Reload PT/SL/Trailing tier chips when the active symbol or
        preferences change."""
        if not hasattr(self, "_pt_section"):
            return
        pt_tiers, sl_tiers, tr_tiers = self._load_tiers_for_active()
        self._pt_section.reload_tiers(pt_tiers)
        self._sl_section.reload_tiers(sl_tiers)
        self._tr_section.reload_tiers(tr_tiers)
        # Sync current qty so the dollar display is correct on next chip click
        self._pt_section.set_qty(self._active_qty)
        self._sl_section.set_qty(self._active_qty)
        self._tr_section.set_qty(self._active_qty)
        # Push contract multiplier for custom-distance dollar math
        from src.core.reference_data import get_instance as _rd
        mult = _rd().get_multiplier(self._active_symbol or "") or 1.0
        self._pt_section.set_multiplier(mult)
        self._sl_section.set_multiplier(mult)
        self._tr_section.set_multiplier(mult)
        # Sync live price + tick size into the TickPriceInput (and its Dist
        # ladder popup).  Tick size must be pushed even before the first price
        # tick arrives so the popup shows multiples of the *selected* symbol's
        # tick size instead of the 0.25 default.
        ts = self._tick_size_for_symbol(self._active_symbol or "")
        self._pt_section.set_market_price(self._last_price, ts)
        self._sl_section.set_market_price(self._last_price, ts)
        self._tr_section.set_market_price(self._last_price, ts)
        # ``reload_tiers`` rebuilds chips + Dist input from scratch so
        # nothing is armed; refresh the Buy/Sell secondary line so the
        # previous symbol's TP/SL/TS amounts don't linger on the buttons.
        self._refresh_action_labels()

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
            "trailing":    self._tr_section.get_value() if hasattr(self, "_tr_section") else None,
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
            log.warning(
                "OrderEntry: order blocked — live %s price unavailable for %s",
                label, self._active_symbol)
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
            # Same bracket payload the MKT/BID/ASK/MID path emits — without
            # these the LMT parent goes out naked and FlashTradeView has
            # nothing to forward as children.
            "profit_take": self._pt_section.get_value() if hasattr(self, "_pt_section") else None,
            "stop_loss":   self._sl_section.get_value() if hasattr(self, "_sl_section") else None,
            "trailing":    self._tr_section.get_value() if hasattr(self, "_tr_section") else None,
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

    def _on_bg_price_update(self, sym: str, last: float, bid: float,
                            ask: float, close: float = 0.0) -> None:
        """Receive a background price tick — update cache and display if active.

        Bid/ask are preserved from the last valid tick rather than being
        overwritten with 0 when a tick carries only a last-price update.
        IB delayed data (type 3) for futures often streams ``last`` ticks
        without bid/ask, which previously cleared the label on every tick.

        ``close`` is the prior-session close used by the ticker strip's
        day-change display (0 when the tick didn't carry one — we keep
        whatever was previously cached).
        """
        # Retrieve the last-known cache entry so we can fall back to its
        # bid/ask when the incoming values are absent (0 or never received).
        cached = self._price_cache.get(sym.upper(), (0.0, 0.0, 0.0))

        # Only replace cached values when the incoming values are actually valid
        effective_last = last if last > 0 else cached[0]
        effective_bid  = bid  if bid  > 0 else cached[1]
        effective_ask  = ask  if ask  > 0 else cached[2]

        self._price_cache[sym.upper()] = (effective_last, effective_bid, effective_ask)
        if close and close > 0:
            self._close_cache[sym.upper()] = float(close)

        if sym.upper() == (self._active_symbol or "").upper():
            if last > 0:
                self._last_price = last
                self._lbl_price.setText(f"{last:,.2f}")
                if hasattr(self, "_pt_section"):
                    ts = self._tick_size_for_symbol(sym)
                    self._pt_section.set_market_price(last, ts)
                    self._sl_section.set_market_price(last, ts)
                    self._tr_section.set_market_price(last, ts)
            # Preserve last known bid/ask — only replace when newly valid
            if bid > 0:
                self._last_bid = bid
            if ask > 0:
                self._last_ask = ask
            self._update_bid_ask_display(self._last_bid, self._last_ask)
            self._update_bid_ask_buttons(self._last_bid, self._last_ask)
            self._refresh_price_change_display()

    def _update_bid_ask_display(self, bid: float, ask: float) -> None:
        """Refresh the bid and ask labels on the compact ticker strip."""
        self._lbl_bid.setText(f"{bid:,.2f}" if bid > 0 else "")
        self._lbl_ask.setText(f"{ask:,.2f}" if ask > 0 else "")

    # ── Ticker-strip day-change display ──────────────────────────────────────

    def _refresh_price_change_display(self) -> None:
        """Repaint the change / %change labels against the prior-session
        close for the active symbol.  Blank when no close is known yet
        (cold start before first bg tick); otherwise signed text with
        green / red / dim colour keyed to the sign of the change.
        """
        sym = (self._active_symbol or "").upper()
        close = self._close_cache.get(sym, 0.0) if sym else 0.0
        last = float(self._last_price or 0.0)
        # Gate the change badges on the price label being populated so
        # the user never sees a change number hovering above a blank
        # price/bid/ask row (would happen if a stale ``_last_price``
        # lingered after a symbol switch while real ticks haven't
        # arrived yet).
        if (not sym or close <= 0 or last <= 0
                or not self._lbl_price.text()):
            self._lbl_change.setText("")
            self._lbl_change_pct.setText("")
            return
        change = last - close
        pct = (change / close) * 100.0 if close else 0.0
        sign = "+" if change > 0 else ""
        self._lbl_change.setText(f"{sign}{change:,.2f}")
        # Per product request: hide %change for the CME index futures
        # (NQ / MNQ / ES / MES) where % moves are tiny and add noise;
        # everything else still shows it.
        if sym in _NO_PCT_CHANGE_SYMBOLS:
            self._lbl_change_pct.setText("")
        else:
            self._lbl_change_pct.setText(f"{sign}{pct:,.2f}%")
        if change > 0:
            colour = Theme.CLR_BUY
        elif change < 0:
            colour = Theme.CLR_SELL
        else:
            colour = Theme.TEXT_DIM
        ss = f"color: {colour}; border: none;"
        self._lbl_change.setStyleSheet(ss)
        self._lbl_change_pct.setStyleSheet(ss)

    # ── Add-Symbol dropdown (type-to-filter popup triggered by "+") ──────────

    def _show_add_symbol_menu(self) -> None:
        """Pop up a searchable list of every saved contract that isn't
        already on the symbol-chip bar.  The search field at the top
        filters the list case-insensitively as the user types (matching
        against symbol, local symbol, and sec_type); Up / Down navigates
        the list, Enter / double-click adds the symbol via the existing
        ``_add_symbol`` path, Esc closes the popup.
        """
        from src.core.reference_data import get_instance as _rd

        # Collect available contracts (skip ones already on the bar).
        added_upper = {s.upper() for s in self._symbols}
        seen_keys: set[str] = set()
        entries: list[tuple[str, str]] = []  # (label, symbol)
        for c in sorted(_rd().contracts, key=lambda x: x.get("symbol", "")):
            sym = c.get("symbol", "")
            if not sym or sym.upper() in added_upper:
                continue
            sec = c.get("sec_type", "")
            local = c.get("local_symbol", "")
            exchange = c.get("exchange", "")
            key = f"{sym}_{local}_{sec}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            detail = local if local and local != sym else exchange
            label = (f"{sym}  –  {detail}  ({sec})"
                     if detail else f"{sym}  ({sec})")
            entries.append((label, sym))

        # Build the popup.  QDialog with Popup window flag so clicking
        # outside closes it automatically (same affordance as QMenu).
        from PySide6.QtWidgets import QDialog
        dlg = QDialog(self)
        dlg.setWindowFlags(Qt.WindowType.Popup)
        dlg.setFixedWidth(260)

        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        search = QLineEdit()
        search.setPlaceholderText("Type to search…")
        search.setStyleSheet(
            f"QLineEdit{{background:{Theme.BG_INPUT};color:{Theme.TEXT};"
            f"border:1px solid {Theme.BORDER};border-radius:3px;"
            f"padding:4px 6px;font-size:12px}}"
            f"QLineEdit:focus{{border-color:{Theme.ACCENT}}}")
        lay.addWidget(search)

        lst = QListWidget()
        lst.setStyleSheet(
            f"QListWidget{{background:{Theme.BG_INPUT};color:{Theme.TEXT};"
            f"border:1px solid {Theme.BORDER};border-radius:3px;"
            f"padding:2px;outline:none}}"
            f"QListWidget::item{{padding:4px 6px;border-radius:2px}}"
            f"QListWidget::item:selected{{background:#3a3f50}}")
        lst.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay.addWidget(lst)

        if not entries:
            item = QListWidgetItem(
                "(no more symbols — add via Contracts tab)")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            lst.addItem(item)
        else:
            for label, sym in entries:
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, sym)
                lst.addItem(item)
            lst.setCurrentRow(0)

        def _apply_filter(text: str) -> None:
            query = text.strip().lower()
            first_visible = -1
            for i in range(lst.count()):
                it = lst.item(i)
                if it.data(Qt.ItemDataRole.UserRole) is None:
                    continue
                visible = (not query) or (query in it.text().lower())
                it.setHidden(not visible)
                if visible and first_visible < 0:
                    first_visible = i
            if first_visible >= 0:
                lst.setCurrentRow(first_visible)

        def _commit_current() -> None:
            it = lst.currentItem()
            if it is None or it.isHidden():
                return
            sym = it.data(Qt.ItemDataRole.UserRole)
            if not sym:
                return
            dlg.close()
            self._add_symbol(sym)

        def _step(delta: int) -> None:
            n = lst.count()
            if n == 0:
                return
            row = lst.currentRow()
            for _ in range(n):
                row = (row + delta) % n
                it = lst.item(row)
                if it is not None and not it.isHidden() \
                        and it.data(Qt.ItemDataRole.UserRole):
                    lst.setCurrentRow(row)
                    return

        # Wire search → filter, Enter → commit, Esc → close (via Popup),
        # double-click on list → commit, Up/Down → navigate.
        search.textChanged.connect(_apply_filter)
        search.returnPressed.connect(_commit_current)
        lst.itemDoubleClicked.connect(lambda _it: _commit_current())

        # Capture Up/Down in the search field so the user can navigate
        # the list without leaving the text input.
        def _search_key_event(event) -> None:
            if event.key() == Qt.Key.Key_Down:
                _step(+1)
                event.accept()
                return
            if event.key() == Qt.Key.Key_Up:
                _step(-1)
                event.accept()
                return
            QLineEdit.keyPressEvent(search, event)
        search.keyPressEvent = _search_key_event  # type: ignore[method-assign]

        # Anchor beneath the + button.
        pos = self._btn_add_symbol.mapToGlobal(
            self._btn_add_symbol.rect().bottomLeft())
        dlg.move(pos)
        dlg.show()
        search.setFocus()

    def _update_bid_ask_buttons(self, bid: float, ask: float) -> None:
        """Keep @Bid / @Ask button labels in sync with the current quantity.

        Prices are shown in the bid/ask label beneath the main price display,
        not on the buttons themselves, so the buttons stay concise.
        """
        self._refresh_action_labels()
