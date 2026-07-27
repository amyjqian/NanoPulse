from __future__ import annotations
from src.core.enums import OrderAction, OrderType
from src.ui.theme import Theme

import logging
import math
from typing import Any

from PySide6.QtCore import (
    QEvent,
    QMimeData,
    QPoint,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDoubleValidator,
    QDrag,
    QFont,
    QFontMetrics,
    QIntValidator,
    QPainter,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.core import config_manager
from src.ui.widgets.order_panel_parts import DraggableChip, attach_exchange_badge

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

    _PRIMARY_PT = 12
    _SECONDARY_PT = 9

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

    # Stylesheets — split into "normal" (idle) and "armed" (positive
    # value typed) so the whole control can flip to the same blue
    # background a selected tier chip uses.  Triggered by the host
    # ``_TierSection`` via :meth:`set_armed`.
    _EDIT_SS_NORMAL = (
        "QLineEdit{background:#3a3a4a;color:#ccc;border:none;"
        "border-radius:6px 0 0 6px;padding:2px 6px;font-size:11px}"
        "QLineEdit:focus{background:#4a4a5a;color:white}"
        "QLineEdit:disabled{background:#252535;color:#444;border:none}"
    )
    _EDIT_SS_ARMED = (
        "QLineEdit{background:#0071e3;color:white;border:none;"
        "border-radius:6px 0 0 6px;padding:2px 6px;font-size:11px;"
        "font-weight:bold}"
        "QLineEdit:focus{background:#0077ed;color:white}"
        "QLineEdit:disabled{background:#252535;color:#444;border:none}"
    )
    _ARROW_SS_NORMAL = (
        "QToolButton{background:#3a3a4a;color:#aaa;border:none;"
        "font-size:7px;padding:0;margin:0}"
        "QToolButton:hover{background:#4a4a5a;color:white}"
        "QToolButton:pressed{background:#0071e3;color:white}"
    )
    _ARROW_SS_ARMED = (
        "QToolButton{background:#0071e3;color:white;border:none;"
        "font-size:7px;padding:0;margin:0}"
        "QToolButton:hover{background:#0077ed;color:white}"
        "QToolButton:pressed{background:#0077ed;color:white}"
    )
    _ARROW_W_SS_NORMAL = (
        "QWidget{background:#3a3a4a;border:none;border-radius:0 6px 6px 0}"
    )
    _ARROW_W_SS_ARMED = (
        "QWidget{background:#0071e3;border:none;border-radius:0 6px 6px 0}"
    )

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
        self._edit.setStyleSheet(self._EDIT_SS_NORMAL)
        # Reject non-numeric keystrokes; positive floats only
        _dv = QDoubleValidator(0.0, 1e12, 10, self._edit)
        _dv.setNotation(QDoubleValidator.Notation.StandardNotation)
        self._edit.setValidator(_dv)
        self._edit.textChanged.connect(self._relay_text)
        self._edit.returnPressed.connect(self._close_popup)
        self._edit.editingFinished.connect(self._snap_to_tick)
        self._edit.installEventFilter(self)

        self._btn_up = QToolButton()
        self._btn_up.setText("▲")
        self._btn_up.setFixedSize(self._BTN_W, self._CHIP_H // 2)
        self._btn_up.setStyleSheet(self._ARROW_SS_NORMAL)
        self._btn_up.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_up.setAutoRepeat(True)
        self._btn_up.setAutoRepeatDelay(400)    # ms before repeat starts
        self._btn_up.setAutoRepeatInterval(80)  # ms between each repeat tick
        self._btn_up.clicked.connect(self._on_up)

        self._btn_dn = QToolButton()
        self._btn_dn.setText("▼")
        self._btn_dn.setFixedSize(self._BTN_W, self._CHIP_H // 2)
        self._btn_dn.setStyleSheet(self._ARROW_SS_NORMAL)
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
        self._arrow_w.setStyleSheet(self._ARROW_W_SS_NORMAL)
        self._arrow_w.setLayout(arrow_col)
        # Track the current armed state so set_armed() can early-return
        # when nothing changed (avoids needless QSS unpolish/polish).
        self._armed: bool = False

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

    def set_armed(self, armed: bool) -> None:
        """Toggle the "armed" visual: when *armed* is True the QLineEdit
        and arrow column flip to the same blue background a selected
        tier chip uses.  Called by :class:`_TierSection` whenever the
        Dist input holds a positive value (or the user explicitly arms
        a chip via Dist) so the user can see at a glance which tier
        section is contributing to the next order."""
        armed = bool(armed)
        if armed == self._armed:
            return
        self._armed = armed
        edit_ss = self._EDIT_SS_ARMED if armed else self._EDIT_SS_NORMAL
        arrow_ss = self._ARROW_SS_ARMED if armed else self._ARROW_SS_NORMAL
        arrow_w_ss = (self._ARROW_W_SS_ARMED if armed
                      else self._ARROW_W_SS_NORMAL)
        self._edit.setStyleSheet(edit_ss)
        self._btn_up.setStyleSheet(arrow_ss)
        self._btn_dn.setStyleSheet(arrow_ss)
        self._arrow_w.setStyleSheet(arrow_w_ss)

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
        # Picking a chip clears the Dist value, so the input drops back
        # to its idle visual.  textChanged is signal-blocked above, so
        # this side-effect needs an explicit call.
        if self._input is not None:
            self._input.set_armed(False)
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
        # Flip the Dist field to the blue "armed" style whenever it
        # holds a positive value, matching the selected-chip colour.
        if self._input is not None:
            self._input.set_armed(val > 0)
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
            self._input.set_armed(False)
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
            try:
                _val = float(text) if text else 0.0
            except ValueError:
                _val = 0.0
            self._input.set_armed(_val > 0)
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


# ═══════════════════════════════════════════════════════════════════════════════
#  Hot-button placeholders + configuration dialog
# ═══════════════════════════════════════════════════════════════════════════════
#
# A "hot button" is a one-click order shortcut the user composes themselves
# (e.g. "Buy 1 NQ @MKT" attached to TP / SL).  The OrderEntry panel hosts
# a small grid of slots beneath the Trailing Stop section: an unconfigured
# slot renders as a dashed-border ``+`` placeholder, and clicking it opens
# :class:`_HotButtonDialog` to capture the label + side.  Slot → real button
# transformation is intentionally out of scope for this commit; we only
# wire the placeholder + dialog UI here.


# MIME type for the drag-and-drop reorder: payload is the source
# index encoded as ASCII text.  Defined module-level so both the
# saved-cell + placeholder drop targets agree.
_HOT_BUTTON_MIME = "application/x-nano-hotbutton-index"


class _HotButtonPlaceholder(QPushButton):
    """Dashed-border ``+`` cell that, when clicked, asks the host to add
    a new hot button.  Doubles as a drop target — dragging a saved
    cell here moves it to the end of the list.  Fixed minimum height
    matches the action-button grid above so the layout reads as a
    single column of touch targets.
    """

    # Emitted when a saved cell is dropped on the placeholder.  The
    # source index is the cell's position in ``OrderEntryPanel._hot_buttons``;
    # the host treats the placeholder as "move to end".
    dropFromCell = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__("+", parent)
        # Match the Flatten button's height (30 px min, no max) so
        # the hot-button row sits flush with the position-management
        # grid above.  ``+`` glyph at 16 px keeps it visually
        # balanced inside the slightly taller cell.
        self.setMinimumHeight(30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "QPushButton{"
            " background:transparent;"
            " color:#5a6478;"
            " border:1px dashed #3a4051;"
            " border-radius:6px;"
            " font-size:16px;"
            " font-weight:bold;"
            " padding:0;"
            "}"
            "QPushButton:hover{"
            " color:#8a94a8;"
            " border-color:#4a5168;"
            "}"
        )
        self.setAcceptDrops(True)

    # ── Drop target ──────────────────────────────────────────────────────────

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(_HOT_BUTTON_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(_HOT_BUTTON_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        data = event.mimeData().data(_HOT_BUTTON_MIME)
        try:
            src = int(bytes(data).decode())
        except (ValueError, UnicodeDecodeError):
            event.ignore()
            return
        self.dropFromCell.emit(src)
        event.acceptProposedAction()


class _HotButtonCell(QPushButton):
    """A saved hot button rendered in the OrderEntry hot-buttons grid.

    Adds three behaviours on top of a plain ``QPushButton``:

    * **Right-click menu** — Edit / Delete the cell's underlying entry.
    * **Drag source** — left-press + move past the start-drag distance
      kicks off a ``QDrag`` carrying the cell's index in the host's
      ``_hot_buttons`` list.
    * **Drop target** — accepts drops of the same MIME type from
      sibling cells; emits ``reorderRequested(src_index, dst_index)``
      so the host can reshuffle the list and rebuild the grid.

    Click semantics on a *normal* (non-drag) press are inherited from
    ``QPushButton`` — the host wires the ``clicked`` signal to whatever
    submission flow is appropriate (currently a no-op pending the
    follow-up that wires order routing).
    """

    editRequested = Signal(int)
    deleteRequested = Signal(int)
    reorderRequested = Signal(int, int)  # (src_index, dst_index)

    def __init__(self, label: str, colour: str, parent=None) -> None:
        super().__init__(label, parent)
        self._index: int = 0
        self._press_pos: QPoint | None = None
        # Match the Flatten button's size: 30 px min height (no
        # max), 11 px text, 4×8 px padding, 4 px border-radius.
        # Width comes from the 2-column hot-button grid, which
        # spans the full panel just like ``pm_grid``.
        self.setMinimumHeight(30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"""
            QPushButton {{
                background: {colour};
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 4px 8px;
                font-weight: bold;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: {colour};
                border: 1px solid #ffffff30;
            }}
            QPushButton:pressed {{
                background: {colour};
                border: 1px solid #ffffff60;
            }}
            """
        )
        self.setAcceptDrops(True)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_index(self, i: int) -> None:
        self._index = int(i)

    def index(self) -> int:
        return self._index

    # ── Drag source ──────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (self._press_pos is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            dist = (event.position().toPoint()
                    - self._press_pos).manhattanLength()
            if dist >= QApplication.startDragDistance():
                # Drag took over — clear the press anchor so a second
                # tiny mouseMove doesn't restart the drag and reset
                # parent state.
                self._press_pos = None
                self._start_drag()
                return
        super().mouseMoveEvent(event)

    def _start_drag(self) -> None:
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_HOT_BUTTON_MIME, str(self._index).encode("ascii"))
        drag.setMimeData(mime)
        # Use the rendered button as the drag pixmap so the user sees
        # exactly what's moving.
        pm = self.grab()
        drag.setPixmap(pm)
        drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        drag.exec(Qt.DropAction.MoveAction)

    # ── Drop target ──────────────────────────────────────────────────────────

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(_HOT_BUTTON_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(_HOT_BUTTON_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        data = event.mimeData().data(_HOT_BUTTON_MIME)
        try:
            src = int(bytes(data).decode())
        except (ValueError, UnicodeDecodeError):
            event.ignore()
            return
        if src != self._index:
            self.reorderRequested.emit(src, self._index)
        event.acceptProposedAction()

    # ── Right-click menu ─────────────────────────────────────────────────────

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        edit_a = menu.addAction("Edit")
        delete_a = menu.addAction("Delete")
        chosen = menu.exec(event.globalPos())
        if chosen is edit_a:
            self.editRequested.emit(self._index)
        elif chosen is delete_a:
            self.deleteRequested.emit(self._index)


class _Segmented(QWidget):
    """Generic segmented selector — N options, one selected at a time.

    Each option is rendered as a flat checkable button.  Pass an
    ``active_colors`` map to override the default blue active fill on a
    per-option basis (used by the SIDE row to colour BUY green / SELL
    red).
    """

    selectionChanged = Signal(str)

    _ACTIVE_BG_DEFAULT = "#0071e3"
    _INACTIVE_BG = "#262a36"
    _ACTIVE_FG = "#ffffff"
    _INACTIVE_FG = "#aab1c0"

    def __init__(
        self,
        options: list[tuple[str, str]],
        *,
        active_colors: dict[str, str] | None = None,
        height: int = 30,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._options = options
        self._active_colors = active_colors or {}
        self._height = height
        self._selected: str = options[0][0]
        self._buttons: dict[str, QPushButton] = {}

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        for value, label in options:
            btn = QPushButton(label, self)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(self._height)
            btn.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(
                lambda _chk, v=value: self._on_clicked(v))
            self._buttons[value] = btn
            lay.addWidget(btn)
        self._restyle()

    def _on_clicked(self, value: str) -> None:
        if value == self._selected:
            # keep the segment latched so users can't deselect everything
            self._buttons[value].setChecked(True)
            return
        self._selected = value
        self._restyle()
        self.selectionChanged.emit(value)

    def _restyle(self) -> None:
        for value, btn in self._buttons.items():
            active = value == self._selected
            btn.setChecked(active)
            if active:
                bg = self._active_colors.get(value, self._ACTIVE_BG_DEFAULT)
                fg = self._ACTIVE_FG
            else:
                bg = self._INACTIVE_BG
                fg = self._INACTIVE_FG
            btn.setStyleSheet(
                "QPushButton{"
                f" background:{bg}; color:{fg};"
                " border:none; border-radius:5px;"
                " padding:6px 12px;"
                " font-weight:bold; font-size:11px;"
                "}"
            )

    def selected(self) -> str:
        return self._selected

    def set_selected(self, value: str) -> None:
        if value not in self._buttons or value == self._selected:
            return
        self._selected = value
        self._restyle()

    def clear_selection(self) -> None:
        """Render every button as inactive — used when the segment is
        contextually irrelevant (e.g. the LIMIT ANCHOR row when the
        order type is MKT).  The internal ``_selected`` is cleared so
        ``selected()`` returns ``""`` until ``set_selected`` is called
        again with a real option, which is how callers restore the
        prior choice when the row becomes relevant again."""
        if self._selected == "":
            return
        self._selected = ""
        self._restyle()


def _hot_button_default_draft() -> dict:
    """Pristine HotButton draft used to seed the modal in Add mode.

    LIMIT ANCHOR defaults to ``MID`` so a freshly-created hot button
    that the user later switches to LMT lands on @Mid (the most common
    starting point for a marketable limit) rather than @Bid.  Existing
    saved hot buttons keep their persisted anchor on Edit.
    """
    return {
        "side": "BUY",
        "group": "__current__",
        "symbol": "",
        "qty": "",
        "ord_type": "LMT",
        "anchor": "MID",
        "offset": 0,
        "label": "",
        "tp": {"on": False, "v": 80.0, "u": "abs"},
        "sl": {"on": False, "v": 80.0, "u": "abs"},
        "disabled": False,
    }


def _auto_hot_button_label(side: str, qty: str, sym: str,
                           ord_type: str, anchor: str, offset: int,
                           tp: dict | None = None,
                           sl: dict | None = None) -> str:
    """Generate the auto-label shown when the user leaves the label
    field blank.

    Format for BUY / SELL:
        ``{qty} {SYM} @{venue} [{tp_value} - {sl_value}]``

    Where:
      * Side is encoded by the button's background colour, not the label
        text — so we omit the ``Buy``/``Sell`` verb to keep the label
        compact for narrow grid cells.
      * ``qty`` is hidden when it equals ``1`` (the common single-lot
        case reads cleanly without a leading number).
      * ``@{venue}`` is ``@MKT`` for market orders, otherwise ``@Bid`` /
        ``@Ask`` / ``@Mid`` (with ``+/-N`` offset suffix when set) for
        limit orders.
      * Take-profit / stop-loss values are appended only when their
        respective leg toggle is on.  When both are on they're joined
        with ``" - "`` so the label reads e.g. ``NQ @MKT 80 - 50``.
    Falls back to ``QTY`` / ``SYM`` placeholders so the preview still
    reads naturally before the user picks values.
    """
    sym_part = sym.strip().upper() if sym.strip() else "SYM"
    # FLATTEN / CANCEL ignore qty / venue entirely.
    if side == "FLATTEN":
        return f"Flatten {sym_part}"
    if side == "CANCEL":
        return f"Cancel {sym_part}"

    qty_clean = qty.strip()
    if qty_clean == "1":
        qty_part = ""
    elif qty_clean:
        qty_part = qty_clean
    else:
        qty_part = "QTY"

    if ord_type == "MKT":
        venue = "@MKT"
    else:
        anchor_label = {"BID": "@Bid", "ASK": "@Ask", "MID": "@Mid"}.get(
            anchor, f"@{anchor}")
        if offset:
            sign = "+" if offset > 0 else ""
            venue = f"{anchor_label} {sign}{offset}"
        else:
            venue = anchor_label

    def _leg_active(leg: dict | None) -> bool:
        return bool(leg and leg.get("on"))

    def _fmt_leg_value(leg: dict | None) -> str:
        # When the bracket pair is shown, an inactive leg renders as
        # "0" so the user can see at a glance which side is armed —
        # but the pair itself is omitted entirely when both legs are
        # off (no brackets attached at all).
        if not _leg_active(leg):
            return "0"
        v = leg.get("v", 0)
        try:
            f = float(v)
        except (TypeError, ValueError):
            return "0"
        return f"{int(f)}" if f.is_integer() else f"{f:g}"

    if _leg_active(tp) or _leg_active(sl):
        bracket = f"{_fmt_leg_value(tp)} - {_fmt_leg_value(sl)}"
    else:
        bracket = ""

    chunks = [c for c in (qty_part, sym_part, venue, bracket) if c]
    return " ".join(chunks)


# Saved-button accent colour for each side.  Mirrors the existing
# FlashTrade action-button palette so a hot button blends with the
# Buy/Sell/Flatten/Cancel buttons in the position-management grid.
_HOT_BUTTON_COLOURS: dict[str, str] = {
    "BUY": Theme.CLR_BUY,
    "SELL": Theme.CLR_SELL,
    "FLATTEN": Theme.CLR_FLATTEN,
    "CANCEL": Theme.CLR_CANCEL,
}


class _HotButtonModal(QDialog):
    """Add/Edit dialog for a single Hot Button — implementation of
    ``new-hot-buttons.md`` §4.

    Collects every field defined by :func:`_hot_button_default_draft`
    plus optional Take Profit / Stop Loss bracket legs.  A live
    preview line at the bottom mirrors the current draft state so the
    user sees what their button will say before saving.
    """

    # Attached-order legs only support absolute dollar offsets — the
    # ticks / % unit options were removed at the user's request, so
    # there's no longer a unit dropdown in the bracket rows; the
    # legend reads "$ price" inline as a static label.
    UNIT_OPTIONS = [("abs", "$ price")]

    def __init__(
        self,
        parent=None,
        *,
        draft: dict | None = None,
        groups: list[str] | None = None,
        edit_mode: bool = False,
        default_specific: bool = False,
    ) -> None:
        super().__init__(parent)
        self._draft = dict(draft) if draft else _hot_button_default_draft()
        # Bracket legs are nested dicts — copy them too so cancelling
        # the dialog doesn't mutate the caller's data.
        self._draft["tp"] = dict(self._draft.get(
            "tp", {"on": False, "v": 80.0, "u": "abs"}))
        self._draft["sl"] = dict(self._draft.get(
            "sl", {"on": False, "v": 80.0, "u": "abs"}))
        self._edit_mode = edit_mode
        # Force the SYMBOL / QTY mode segments to "specific" with empty
        # inputs even when the draft has no value — used by Add mode so
        # the user always lands on the explicit-input variant rather
        # than the "use selected from panel" default.
        self._default_specific = bool(default_specific)
        # Tracks whether the user has manually edited the LABEL input.
        # While False, every field-change slot rewrites the label with
        # the auto-generated text so the user sees a live preview as
        # they fill in symbol / qty / order type / brackets.  Flips to
        # True the moment the user types into the label field, then
        # back to False if they clear it again so auto-fill resumes.
        self._label_user_edited = False
        # Same idea for the QTY input — when the user types a symbol
        # that resolves to a known sec_type, we auto-fill QTY from the
        # trading_prefs default size for that instrument class (FUT vs
        # STK).  Once the user manually edits qty, this flag freezes
        # the value; clearing the field re-engages auto-fill.
        self._qty_user_edited = False
        self._delete_requested = False
        # When the caller doesn't pass a groups list, pull the
        # user-defined trading groups from app_config.json.  Built-in
        # "All" / "None" header pills aren't in that list, so no
        # extra filtering is needed.
        if groups is None:
            try:
                groups = [g["name"] for g
                          in config_manager.load_account_groups()
                          if g.get("name")]
            except Exception:
                log.exception(
                    "HotButtonModal: failed to load account groups")
                groups = []
        self._groups = list(groups)

        self.setWindowTitle("Edit Hot Button" if edit_mode else "Add Hot Button")
        self.setModal(True)
        self.setMinimumWidth(540)
        self.setStyleSheet(
            f"QDialog{{background:{Theme.BG_PANEL};color:{Theme.TEXT};}}")

        self._build_ui()
        self._apply_draft()
        self._update_preview()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(14)

        # Header — side badge + title + subtitle
        header = QHBoxLayout()
        header.setSpacing(10)
        self._lbl_side_badge = QLabel(self._draft["side"], self)
        self._lbl_side_badge.setFixedHeight(22)
        self._lbl_side_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_side_badge.setMinimumWidth(40)
        title = QLabel("Edit Hot Button" if self._edit_mode else "Add Hot Button")
        title.setStyleSheet(
            f"color:{Theme.TEXT}; font-size:15px; font-weight:bold;")
        subtitle = QLabel("— rapid buy/sell shortcut")
        subtitle.setStyleSheet(
            f"color:{Theme.TEXT_DIM}; font-size:11px;")
        header.addWidget(self._lbl_side_badge)
        header.addWidget(title)
        header.addWidget(subtitle)
        header.addStretch(1)
        outer.addLayout(header)

        # ── TRADING GROUP + SIDE row ────────────────────────────────
        row1 = QHBoxLayout()
        row1.setSpacing(12)
        # Group dropdown
        col_g = QVBoxLayout()
        col_g.setSpacing(4)
        col_g.addWidget(self._caption("TRADING GROUP"))
        self._cmb_group = QComboBox(self)
        # Special "Current Selection" entry maps to the __current__ sentinel.
        self._cmb_group.addItem("— Current Selection —", "__current__")
        for g in self._groups:
            self._cmb_group.addItem(g, g)
        self._cmb_group.setStyleSheet(self._INPUT_QSS)
        self._cmb_group.currentIndexChanged.connect(self._on_group_changed)
        col_g.addWidget(self._cmb_group)
        # Side segmented — four options.  FLATTEN closes positions,
        # CANCEL cancels open orders; both render in their respective
        # action-button accent colours so the saved hot button's style
        # is consistent with the existing FlashTrade buttons.
        col_s = QVBoxLayout()
        col_s.setSpacing(4)
        col_s.addWidget(self._caption("SIDE"))
        self._seg_side = _Segmented(
            [("BUY", "BUY"), ("SELL", "SELL"),
             ("FLATTEN", "FLATTEN"), ("CANCEL", "CANCEL")],
            active_colors={
                "BUY": Theme.CLR_BUY,
                "SELL": Theme.CLR_SELL,
                "FLATTEN": Theme.CLR_FLATTEN,
                "CANCEL": Theme.CLR_CANCEL,
            },
        )
        self._seg_side.selectionChanged.connect(self._on_side_changed)
        col_s.addWidget(self._seg_side)
        row1.addLayout(col_g, stretch=1)
        row1.addLayout(col_s, stretch=1)
        outer.addLayout(row1)

        # ── SYMBOL ───────────────────────────────────────────────────
        outer.addLayout(self._symbol_row())
        # ── SHARES / QTY ─────────────────────────────────────────────
        outer.addLayout(self._qty_row())
        # ── ORDER TYPE ───────────────────────────────────────────────
        col_ot = QVBoxLayout()
        col_ot.setSpacing(4)
        col_ot.addWidget(self._caption("ORDER TYPE"))
        self._seg_ord_type = _Segmented(
            [("LMT", "Limit"), ("MKT", "Market")], height=34)
        self._seg_ord_type.selectionChanged.connect(self._on_ord_type_changed)
        col_ot.addWidget(self._seg_ord_type)
        outer.addLayout(col_ot)

        # ── LIMIT ANCHOR + OFFSET (faded when MKT) ───────────────────
        self._limit_row_w = QWidget(self)
        lim_row = QHBoxLayout(self._limit_row_w)
        lim_row.setContentsMargins(0, 0, 0, 0)
        lim_row.setSpacing(12)
        col_a = QVBoxLayout()
        col_a.setSpacing(4)
        col_a.addWidget(self._caption("LIMIT ANCHOR"))
        self._seg_anchor = _Segmented(
            [("BID", "@Bid"), ("ASK", "@Ask"), ("MID", "@Mid")])
        self._seg_anchor.selectionChanged.connect(self._on_anchor_changed)
        col_a.addWidget(self._seg_anchor)
        col_o = QVBoxLayout()
        col_o.setSpacing(4)
        col_o.addLayout(self._caption_row(
            "OFFSET", "ticks · ± from anchor"))
        self._inp_offset = QLineEdit("0", self)
        self._inp_offset.setValidator(QIntValidator(-9999, 9999, self))
        self._inp_offset.setStyleSheet(self._INPUT_QSS)
        self._inp_offset.setFixedHeight(30)
        self._inp_offset.textChanged.connect(self._on_offset_changed)
        col_o.addWidget(self._inp_offset)
        lim_row.addLayout(col_a, stretch=2)
        lim_row.addLayout(col_o, stretch=1)
        outer.addWidget(self._limit_row_w)

        # ── BUTTON LABEL ─────────────────────────────────────────────
        col_l = QVBoxLayout()
        col_l.setSpacing(4)
        col_l.addLayout(self._caption_row("BUTTON LABEL", "auto"))
        self._inp_label = QLineEdit(self)
        self._inp_label.setPlaceholderText("e.g. Quick Buy 100 @Bid")
        self._inp_label.setMaxLength(28)
        self._inp_label.setStyleSheet(self._INPUT_QSS)
        self._inp_label.setFixedHeight(30)
        self._inp_label.textChanged.connect(self._on_label_changed)
        col_l.addWidget(self._inp_label)
        outer.addLayout(col_l)

        # ── ATTACHED ORDERS ──────────────────────────────────────────
        self._attached_card_w = self._attached_card()
        outer.addWidget(self._attached_card_w)

        # ── PREVIEW ──────────────────────────────────────────────────
        outer.addWidget(self._preview_row())

        # ── Footer ───────────────────────────────────────────────────
        foot = QHBoxLayout()
        foot.setSpacing(8)
        if self._edit_mode:
            self._btn_delete = QPushButton("Delete", self)
            self._btn_delete.setStyleSheet(
                "QPushButton{"
                f" background:transparent; color:{Theme.CLR_SELL};"
                f" border:1px solid {Theme.CLR_SELL}; border-radius:5px;"
                " padding:6px 14px; font-weight:bold; font-size:11px;"
                "}"
                "QPushButton:hover{ background:#3a1a1a; }"
            )
            self._btn_delete.clicked.connect(self._on_delete_clicked)
            foot.addWidget(self._btn_delete)
        foot.addStretch(1)
        btn_cancel = QPushButton("Cancel", self)
        btn_cancel.setStyleSheet(self._BTN_NEUTRAL_QSS)
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton(
            "Save Changes" if self._edit_mode else "Save", self)
        btn_save.setStyleSheet(self._BTN_PRIMARY_QSS)
        btn_save.clicked.connect(self.accept)
        btn_save.setDefault(True)
        foot.addWidget(btn_cancel)
        foot.addWidget(btn_save)
        outer.addLayout(foot)

    # ── Sub-builders ──────────────────────────────────────────────────────────

    def _symbol_row(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(4)
        cap_row = QHBoxLayout()
        cap_row.setContentsMargins(0, 0, 0, 0)
        cap_row.addWidget(self._caption("SYMBOL"))
        cap_row.addStretch(1)
        self._seg_sym_mode = _Segmented(
            [("default", "DEFAULT · USE SELECTED"), ("specific", "SPECIFIC")],
            height=22,
        )
        self._seg_sym_mode.selectionChanged.connect(self._on_sym_mode_changed)
        # Right-align the toggle, give it a max width so it doesn't
        # eat the row.
        wrap = QWidget(self)
        wlay = QHBoxLayout(wrap)
        wlay.setContentsMargins(0, 0, 0, 0)
        wlay.addWidget(self._seg_sym_mode)
        wrap.setMaximumWidth(280)
        cap_row.addWidget(wrap)
        col.addLayout(cap_row)
        self._inp_symbol = QLineEdit(self)
        self._inp_symbol.setPlaceholderText(
            "e.g. NQM6, AAPL — leave empty to use selected")
        self._inp_symbol.setStyleSheet(self._INPUT_QSS)
        self._inp_symbol.setFixedHeight(30)
        self._inp_symbol.textChanged.connect(self._on_symbol_changed)
        self._install_symbol_completer(self._inp_symbol)
        col.addWidget(self._inp_symbol)
        # Match-status hint — populated by _update_symbol_status() based
        # on a lookup against the saved-contracts cache.
        self._lbl_sym_status = QLabel("", self)
        self._lbl_sym_status.setStyleSheet(
            f"color:{Theme.TEXT_DIM}; font-size:9px;")
        self._lbl_sym_status.setMinimumHeight(14)
        col.addWidget(self._lbl_sym_status)
        return col

    def _qty_row(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(4)
        cap_row = QHBoxLayout()
        cap_row.setContentsMargins(0, 0, 0, 0)
        cap_row.addWidget(self._caption("SHARES / QTY"))
        cap_row.addStretch(1)
        self._seg_qty_mode = _Segmented(
            [("default", "DEFAULT · USE SELECTED"), ("specific", "SPECIFIC")],
            height=22,
        )
        self._seg_qty_mode.selectionChanged.connect(self._on_qty_mode_changed)
        wrap = QWidget(self)
        wlay = QHBoxLayout(wrap)
        wlay.setContentsMargins(0, 0, 0, 0)
        wlay.addWidget(self._seg_qty_mode)
        wrap.setMaximumWidth(260)
        cap_row.addWidget(wrap)
        col.addLayout(cap_row)
        self._inp_qty = QLineEdit(self)
        self._inp_qty.setPlaceholderText(
            "leave empty to use selected qty chip")
        self._inp_qty.setStyleSheet(self._INPUT_QSS)
        self._inp_qty.setFixedHeight(30)
        self._inp_qty.textChanged.connect(self._on_qty_changed)
        col.addWidget(self._inp_qty)
        return col

    def _attached_card(self) -> QFrame:
        card = QFrame(self)
        card.setObjectName("attachedCard")
        card.setStyleSheet(
            "QFrame#attachedCard{"
            f" background:{Theme.BG_INPUT};"
            f" border:1px solid {Theme.BORDER};"
            " border-radius:6px;"
            "}"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(6)
        h_dot = QLabel("●", card)
        h_dot.setStyleSheet(f"color:{Theme.ACCENT}; font-size:10px;")
        h_lbl = QLabel("ATTACHED ORDERS", card)
        h_lbl.setStyleSheet(
            f"color:{Theme.TEXT}; font-size:10px; font-weight:bold;"
            " letter-spacing:1px;")
        h_sub = QLabel("— sent as bracket on fill", card)
        h_sub.setStyleSheet(f"color:{Theme.TEXT_DIM}; font-size:10px;")
        head.addWidget(h_dot)
        head.addWidget(h_lbl)
        head.addWidget(h_sub)
        head.addStretch(1)
        lay.addLayout(head)

        # TP row + SL row
        self._tp_widgets = self._build_bracket_row(
            "Take Profit", "limit · opposite side · same qty",
            Theme.CLR_BUY, "tp", card)
        self._sl_widgets = self._build_bracket_row(
            "Stop Loss", "stop · opposite side · same qty",
            Theme.CLR_SELL, "sl", card)
        lay.addLayout(self._tp_widgets["row"])
        lay.addLayout(self._sl_widgets["row"])
        return card

    def _build_bracket_row(self, name: str, sublabel: str,
                           accent_colour: str, key: str,
                           parent: QWidget) -> dict:
        row = QHBoxLayout()
        row.setSpacing(8)
        chk = QCheckBox(parent)
        chk.setStyleSheet(self._SWITCH_QSS)
        chk.toggled.connect(
            lambda on, k=key: self._on_bracket_toggled(k, on))
        lbl_box = QVBoxLayout()
        lbl_box.setSpacing(0)
        nm = QLabel(name, parent)
        nm.setStyleSheet(f"color:{accent_colour}; font-size:11px;"
                         " font-weight:bold;")
        sub = QLabel(sublabel, parent)
        sub.setStyleSheet(f"color:{Theme.TEXT_DIM}; font-size:9px;")
        lbl_box.addWidget(nm)
        lbl_box.addWidget(sub)
        val = QLineEdit("80", parent)
        val.setValidator(QDoubleValidator(0.0, 1e9, 4, parent))
        val.setStyleSheet(self._INPUT_QSS)
        val.setFixedHeight(28)
        val.setFixedWidth(80)
        val.textChanged.connect(
            lambda t, k=key: self._on_bracket_value_changed(k, t))
        # Static "$ price" label — replaces the previous unit dropdown
        # since the only supported unit is absolute dollar offset.
        unit_lbl = QLabel("$ price", parent)
        unit_lbl.setStyleSheet(
            f"color:{Theme.TEXT_DIM}; font-size:11px;"
            " padding:0 6px;")
        unit_lbl.setFixedWidth(70)
        unit_lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter
                              | Qt.AlignmentFlag.AlignLeft)
        row.addWidget(chk)
        row.addLayout(lbl_box, stretch=1)
        row.addWidget(val)
        row.addWidget(unit_lbl)
        return {"row": row, "chk": chk, "val": val, "unit_lbl": unit_lbl,
                "name": nm, "sub": sub}

    def _preview_row(self) -> QFrame:
        f = QFrame(self)
        f.setObjectName("previewCard")
        f.setStyleSheet(
            "QFrame#previewCard{"
            f" background:{Theme.BG_INPUT};"
            f" border:1px solid {Theme.BORDER};"
            " border-radius:6px;"
            "}"
        )
        lay = QHBoxLayout(f)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)
        cap = QLabel("PREVIEW", f)
        cap.setStyleSheet(
            f"color:{Theme.TEXT_DIM}; font-size:10px; font-weight:bold;"
            " letter-spacing:1px;")
        # Mini styled tile that mirrors the eventual hot-button rendering.
        self._preview_btn = QPushButton(f)
        self._preview_btn.setEnabled(False)
        self._preview_btn.setMinimumHeight(46)
        self._preview_btn.setMinimumWidth(170)
        # Summary line
        self._preview_line = QLabel("", f)
        self._preview_line.setStyleSheet(
            f"color:{Theme.TEXT}; font-size:11px;")
        self._preview_line.setWordWrap(True)
        lay.addWidget(cap)
        lay.addWidget(self._preview_btn)
        lay.addWidget(self._preview_line, stretch=1)
        return f

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _caption(self, text: str) -> QLabel:
        lbl = QLabel(text, self)
        lbl.setStyleSheet(
            f"color:{Theme.TEXT_DIM}; font-size:10px; font-weight:bold;"
            " letter-spacing:1px;")
        return lbl

    def _caption_row(self, primary: str, secondary: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._caption(primary))
        row.addStretch(1)
        s = QLabel(secondary, self)
        s.setStyleSheet(f"color:{Theme.TEXT_DIM}; font-size:9px;")
        row.addWidget(s)
        return row

    _INPUT_QSS = (
        "QLineEdit, QComboBox{"
        " background:#262a36; color:#e6e9ef;"
        " border:1px solid #2c313a; border-radius:5px;"
        " padding:4px 8px; font-size:11px;"
        "}"
        "QLineEdit:focus, QComboBox:focus{ border-color:#0071e3; }"
        "QLineEdit:disabled, QComboBox:disabled{ color:#5a6478; }"
        "QComboBox::drop-down{ border:none; width:20px; }"
        # Style the popup list — without this Qt falls back to the
        # platform default which renders light-on-light on dark themes.
        "QComboBox QAbstractItemView{"
        " background:#262a36; color:#e6e9ef;"
        " selection-background-color:#0071e3;"
        " selection-color:#ffffff;"
        " border:1px solid #2c313a;"
        " outline:none; padding:2px;"
        "}"
        "QComboBox QAbstractItemView::item{"
        " min-height:22px; padding:2px 8px;"
        "}"
    )
    _BTN_NEUTRAL_QSS = (
        "QPushButton{"
        " background:transparent; color:#aab1c0;"
        " border:1px solid #3a4051; border-radius:5px;"
        " padding:6px 14px; font-weight:bold; font-size:11px;"
        "}"
        "QPushButton:hover{ color:#e6e9ef; border-color:#4a5168; }"
    )
    _BTN_PRIMARY_QSS = (
        "QPushButton{"
        " background:#0071e3; color:#ffffff;"
        " border:none; border-radius:5px;"
        " padding:6px 14px; font-weight:bold; font-size:11px;"
        "}"
        "QPushButton:hover{ background:#0077ed; }"
    )
    _SWITCH_QSS = (
        "QCheckBox::indicator{"
        " width:34px; height:18px; border-radius:9px;"
        " background:#3a4051; border:none;"
        "}"
        "QCheckBox::indicator:checked{ background:#3FB950; }"
    )

    # ── Draft load / save ─────────────────────────────────────────────────────

    def _apply_draft(self) -> None:
        d = self._draft
        # Side
        self._seg_side.set_selected(d.get("side", "BUY"))
        self._lbl_side_badge.setText(d["side"])
        self._restyle_side_badge()
        # Group
        gv = d.get("group", "__current__")
        idx = self._cmb_group.findData(gv)
        self._cmb_group.setCurrentIndex(idx if idx >= 0 else 0)
        # Symbol — segment forced to "specific" when default_specific is
        # set OR the draft already carries a value.  When forced with
        # an empty value the input is enabled but blank so the user can
        # type directly without first toggling the segment.
        sym = d.get("symbol", "")
        sym_specific = bool(sym) or self._default_specific
        self._seg_sym_mode.set_selected("specific" if sym_specific else "default")
        self._inp_symbol.blockSignals(True)
        self._inp_symbol.setText(sym)
        self._inp_symbol.blockSignals(False)
        self._inp_symbol.setEnabled(sym_specific)
        # Qty — same rule as Symbol.
        qty = d.get("qty", "")
        qty_specific = bool(qty) or self._default_specific
        self._seg_qty_mode.set_selected("specific" if qty_specific else "default")
        self._inp_qty.blockSignals(True)
        self._inp_qty.setText(qty)
        self._inp_qty.blockSignals(False)
        self._inp_qty.setEnabled(qty_specific)
        # An existing non-empty qty in the draft (Edit mode) is the
        # user's prior choice — freeze it so auto-fill on symbol typing
        # doesn't clobber it.  Empty qty (Add mode) stays in auto.
        self._qty_user_edited = bool(qty)
        # Order type
        self._seg_ord_type.set_selected(d.get("ord_type", "LMT"))
        # Anchor + offset
        self._seg_anchor.set_selected(d.get("anchor", "BID"))
        self._inp_offset.blockSignals(True)
        self._inp_offset.setText(str(int(d.get("offset", 0))))
        self._inp_offset.blockSignals(False)
        # Label.  Decide once at load time whether the saved label is a
        # custom override (user-edited) or an auto-derived snapshot:
        # * Empty / matches what the auto rule produces from the loaded
        #   fields → treat as auto so future field edits regenerate it.
        # * Anything else → treat as user-edited and freeze.
        saved_label = d.get("label", "")
        self._inp_label.blockSignals(True)
        self._inp_label.setText(saved_label)
        self._inp_label.blockSignals(False)
        auto_now = self._compute_auto_label()
        self._label_user_edited = (
            bool(saved_label) and saved_label != auto_now)
        # Brackets — only the toggle + dollar value are user-editable
        # now (unit is fixed at "abs").
        for key, w in (("tp", self._tp_widgets), ("sl", self._sl_widgets)):
            leg = d.get(key, {})
            # Force the unit to the only supported option so any old
            # ticks / % drafts upgrade cleanly.
            leg["u"] = "abs"
            self._draft[key]["u"] = "abs"
            w["chk"].blockSignals(True)
            w["chk"].setChecked(bool(leg.get("on", False)))
            w["chk"].blockSignals(False)
            w["val"].blockSignals(True)
            w["val"].setText(str(leg.get("v", 80)))
            w["val"].blockSignals(False)
            self._restyle_bracket_row(key)
        # Limit-anchor row faded when MKT.  Also covers the CANCEL
        # case (which hides everything order-type-related).
        self._update_side_dependent_state()
        # Initialise the symbol-search hint for the loaded draft.
        self._update_symbol_status()
        # Final preview pass — also fills in the auto-label field when
        # not user-edited, so the user always sees a live label even
        # before they touch any input.
        self._update_preview()

    def _compute_auto_label(self) -> str:
        """Generate the auto-label text from the current draft state.
        Used both to populate ``_inp_label`` live and to detect whether
        a saved label is a custom override or an auto-derived snapshot.
        """
        d = self._draft
        return _auto_hot_button_label(
            side=d.get("side", "BUY"),
            qty=d.get("qty", ""),
            sym=d.get("symbol", ""),
            ord_type=d.get("ord_type", "LMT"),
            anchor=d.get("anchor", "BID"),
            offset=int(d.get("offset", 0) or 0),
            tp=d.get("tp"),
            sl=d.get("sl"),
        )

    def _refresh_auto_label_field(self) -> None:
        """Push the auto-generated label into ``_inp_label`` whenever
        the user hasn't manually overridden it.  Skips when the field
        text already matches so the cursor doesn't jump while typing
        in adjacent inputs.  ``blockSignals`` prevents this programmatic
        update from triggering ``_on_label_changed``."""
        if self._label_user_edited:
            return
        auto = self._compute_auto_label()
        if self._inp_label.text() == auto:
            self._draft["label"] = auto
            return
        self._inp_label.blockSignals(True)
        self._inp_label.setText(auto)
        self._inp_label.blockSignals(False)
        self._draft["label"] = auto

    def values(self) -> dict:
        """Return the current draft as a fully-populated HotButton dict."""
        return dict(self._draft)

    def delete_requested(self) -> bool:
        return self._delete_requested

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_side_changed(self, side: str) -> None:
        self._draft["side"] = side
        self._lbl_side_badge.setText(side)
        self._restyle_side_badge()
        self._update_side_dependent_state()
        self._update_preview()

    def _update_side_dependent_state(self) -> None:
        """When the SIDE is set to CANCEL, gray out everything that
        doesn't apply to a cancel-orders shortcut: the order-type
        segment, the limit-anchor + offset row, and the attached-
        orders card.  BUY / SELL / FLATTEN restore the normal state
        (limit row still gated by the MKT toggle below)."""
        is_cancel = self._draft.get("side") == "CANCEL"
        self._seg_ord_type.setEnabled(not is_cancel)
        self._attached_card_w.setEnabled(not is_cancel)
        # Limit-row state combines: side != CANCEL AND ord_type == LMT
        self._update_limit_row_state()

    def _on_group_changed(self, _i: int) -> None:
        v = self._cmb_group.currentData()
        self._draft["group"] = v if v is not None else "__current__"
        self._update_preview()

    def _on_sym_mode_changed(self, mode: str) -> None:
        self._inp_symbol.setEnabled(mode == "specific")
        if mode == "default":
            self._inp_symbol.blockSignals(True)
            self._inp_symbol.clear()
            self._inp_symbol.blockSignals(False)
            self._draft["symbol"] = ""
        self._update_symbol_status()
        self._update_preview()

    def _on_symbol_changed(self, text: str) -> None:
        self._draft["symbol"] = text.strip()
        if self._draft["symbol"]:
            self._seg_sym_mode.set_selected("specific")
        # Auto-fill QTY from trading prefs when the symbol resolves to
        # a known sec_type and the user hasn't already typed a custom
        # qty.  Futures pull from prefs['FUT']['quantity']; stocks /
        # options share prefs['STK']['quantity'].
        self._auto_fill_qty_from_prefs()
        self._update_symbol_status()
        self._update_preview()

    def _auto_fill_qty_from_prefs(self) -> None:
        """Populate ``_inp_qty`` with the trading-prefs default size for
        the current symbol's instrument class — Futures or Stock.  No-op
        when the user has manually edited qty (``_qty_user_edited``) or
        the symbol can't be resolved to a known sec_type.

        Lookup uses :class:`ReferenceData.get_sec_type` and falls back
        to the FUT preference bucket for FUT, STK for STK / OPT.  An
        unknown symbol (``"?"``) leaves qty alone so we don't guess.
        """
        if self._qty_user_edited:
            return
        sym = (self._draft.get("symbol") or "").strip().upper()
        if not sym:
            return
        try:
            from src.core.reference_data import get_instance as _rd
            from src.core.trading_prefs import trading_prefs
            sec = _rd().get_sec_type(sym)
        except Exception:
            log.debug("HotButton: prefs auto-fill lookup failed",
                      exc_info=True)
            return
        if sec == "FUT":
            pref_key = "FUT"
        elif sec in ("STK", "OPT"):
            pref_key = "STK"
        else:
            return
        try:
            qty_pref = (trading_prefs.get(pref_key) or {}).get("quantity")
        except Exception:
            return
        if not qty_pref:
            return
        try:
            qty_str = str(int(qty_pref))
        except (TypeError, ValueError):
            return
        if self._inp_qty.text() == qty_str:
            self._draft["qty"] = qty_str
            return
        self._inp_qty.blockSignals(True)
        self._inp_qty.setText(qty_str)
        self._inp_qty.blockSignals(False)
        self._draft["qty"] = qty_str
        # Also force the qty mode segment to "specific" since we just
        # populated a concrete value.
        self._seg_qty_mode.set_selected("specific")
        self._inp_qty.setEnabled(True)

    def _install_symbol_completer(self, line: QLineEdit) -> None:
        """Wire a case-insensitive ``contains``-match QCompleter against
        :attr:`ReferenceData.contracts_by_symbol`.  The completer's
        popup picks up the modal's dark QSS so it doesn't render
        light-on-light, and selecting a row fires
        :meth:`_on_symbol_changed` via the line-edit's ``textChanged``.
        """
        try:
            from src.core.reference_data import get_instance as _rd
            cache = _rd().contracts_by_symbol
        except Exception:
            log.exception(
                "HotButtonModal: contract cache unavailable for completer")
            cache = {}
        # Build an ordered, deduped list of symbols + local_symbols
        # (e.g. ``"NQ"`` and ``"NQM6"`` both navigate to the same contract).
        seen: set[str] = set()
        items: list[str] = []
        for sym, rows in sorted(cache.items()):
            for label in (sym,) + tuple(
                    str(r.get("local_symbol") or "") for r in rows):
                if not label:
                    continue
                key = label.upper()
                if key in seen:
                    continue
                seen.add(key)
                items.append(label)
        completer = QCompleter(items, line)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(
            QCompleter.CompletionMode.PopupCompletion)
        # Style the completer popup to match the modal's dark theme.
        popup = completer.popup()
        popup.setStyleSheet(
            "QListView{"
            " background:#262a36; color:#e6e9ef;"
            " selection-background-color:#0071e3;"
            " selection-color:#ffffff;"
            " border:1px solid #2c313a; outline:none;"
            "}"
            "QListView::item{ padding:4px 10px; min-height:22px; }"
        )
        line.setCompleter(completer)

    def _update_symbol_status(self) -> None:
        """Validate the SPECIFIC-mode symbol against the saved-contracts
        cache and display a small status hint below the input.
        Empty / DEFAULT mode → blank hint.  Match → ``✓ <local_symbol> ·
        <sec_type>``.  No match → ``⚠ symbol not found``.
        """
        sym = self._draft.get("symbol", "")
        if not sym:
            self._lbl_sym_status.setText("")
            return
        try:
            from src.core.reference_data import get_instance as _rd
            contracts = _rd().contracts_by_symbol.get(sym.upper(), [])
        except Exception:
            log.exception("HotButtonModal: contract lookup failed")
            contracts = []
        if not contracts:
            self._lbl_sym_status.setText("⚠ symbol not found")
            self._lbl_sym_status.setStyleSheet(
                f"color:{Theme.CLR_FLATTEN}; font-size:9px;")
            return
        c = contracts[0]
        pieces: list[str] = []
        local = c.get("local_symbol") or c.get("symbol") or sym
        pieces.append(str(local))
        sec_type = c.get("sec_type") or ""
        if sec_type:
            pieces.append(str(sec_type))
        exch = c.get("exchange") or ""
        if exch:
            pieces.append(str(exch))
        extra = ""
        if len(contracts) > 1:
            extra = f"  (+{len(contracts) - 1} more)"
        self._lbl_sym_status.setText(
            "✓ " + " · ".join(pieces) + extra)
        self._lbl_sym_status.setStyleSheet(
            f"color:{Theme.CLR_BUY}; font-size:9px;")

    def _on_qty_mode_changed(self, mode: str) -> None:
        self._inp_qty.setEnabled(mode == "specific")
        if mode == "default":
            self._inp_qty.blockSignals(True)
            self._inp_qty.clear()
            self._inp_qty.blockSignals(False)
            self._draft["qty"] = ""
            # Switching back to "use selected chip" cancels the user's
            # manual qty so the next symbol typing can re-auto-fill
            # from prefs (kept frozen otherwise).
            self._qty_user_edited = False
        self._update_preview()

    def _on_qty_changed(self, text: str) -> None:
        clean = text.strip()
        self._draft["qty"] = clean
        if clean:
            self._seg_qty_mode.set_selected("specific")
        # User typing flips qty out of auto mode so a later symbol
        # change won't replace their value.  Clearing the field
        # re-engages auto so the next symbol fires the prefs default.
        self._qty_user_edited = bool(clean)
        self._update_preview()

    def _on_ord_type_changed(self, t: str) -> None:
        self._draft["ord_type"] = t
        self._update_limit_row_state()
        self._update_preview()

    def _on_anchor_changed(self, a: str) -> None:
        self._draft["anchor"] = a
        self._update_preview()

    def _on_offset_changed(self, t: str) -> None:
        try:
            self._draft["offset"] = int(t) if t.lstrip("-").isdigit() else 0
        except ValueError:
            self._draft["offset"] = 0
        self._update_preview()

    def _on_label_changed(self, t: str) -> None:
        text = t.strip()
        self._draft["label"] = text
        # The user manually typing into the label field flips us out of
        # auto mode so subsequent symbol/qty/order-type/bracket changes
        # don't clobber their custom text.  An empty field re-engages
        # auto mode — typing back to blank is the natural way to ask
        # for the live-generated label again.  Typing the exact auto
        # text also stays in auto mode (e.g. user retyped what was
        # already there).
        if not text:
            self._label_user_edited = False
        else:
            self._label_user_edited = text != self._compute_auto_label()
        self._update_preview()

    def _on_bracket_toggled(self, key: str, on: bool) -> None:
        self._draft[key]["on"] = bool(on)
        self._restyle_bracket_row(key)
        self._update_preview()

    def _on_bracket_value_changed(self, key: str, t: str) -> None:
        try:
            self._draft[key]["v"] = float(t) if t else 0.0
        except ValueError:
            self._draft[key]["v"] = 0.0
        self._update_preview()

    def _on_delete_clicked(self) -> None:
        self._delete_requested = True
        self.accept()

    # ── Visual updates ────────────────────────────────────────────────────────

    def _restyle_side_badge(self) -> None:
        side = self._draft.get("side", "BUY")
        bg = _HOT_BUTTON_COLOURS.get(side, Theme.CLR_BUY)
        self._lbl_side_badge.setStyleSheet(
            f"QLabel{{ background:{bg}; color:#ffffff;"
            " border-radius:4px; padding:2px 8px;"
            " font-weight:bold; font-size:10px; letter-spacing:1px; }}")

    def _restyle_bracket_row(self, key: str) -> None:
        w = self._tp_widgets if key == "tp" else self._sl_widgets
        on = self._draft[key]["on"]
        # QSS opacity isn't supported per-rule; use enable/disable +
        # palette swap instead.
        w["val"].setEnabled(on)
        # Dim the labels (and the static "$ price" tail) when off.
        if on:
            colour = (Theme.CLR_BUY if key == "tp" else Theme.CLR_SELL)
            w["name"].setStyleSheet(
                f"color:{colour}; font-size:11px; font-weight:bold;")
            w["sub"].setStyleSheet(
                f"color:{Theme.TEXT_DIM}; font-size:9px;")
            w["unit_lbl"].setStyleSheet(
                f"color:{Theme.TEXT_DIM}; font-size:11px; padding:0 6px;")
        else:
            w["name"].setStyleSheet(
                "color:#5a6478; font-size:11px; font-weight:bold;")
            w["sub"].setStyleSheet("color:#3a4051; font-size:9px;")
            w["unit_lbl"].setStyleSheet(
                "color:#3a4051; font-size:11px; padding:0 6px;")

    def _update_limit_row_state(self) -> None:
        is_lmt = self._draft.get("ord_type") == "LMT"
        is_cancel = self._draft.get("side") == "CANCEL"
        active = is_lmt and not is_cancel
        self._limit_row_w.setEnabled(active)
        # When the row is inactive (MKT / CANCEL) clear the anchor
        # segment so no @Bid/@Ask/@Mid pill appears highlighted —
        # there's no anchor decision to make at MKT.  Switching back to
        # LMT restores the draft's saved anchor.
        if active:
            self._seg_anchor.set_selected(
                self._draft.get("anchor", "MID"))
        else:
            self._seg_anchor.clear_selection()

    def _update_preview(self) -> None:
        # Refresh the auto-fill label in the input field FIRST so the
        # preview button below picks up the same text the user sees
        # typed into the label box.  No-op when the user has manually
        # edited the label.
        self._refresh_auto_label_field()
        d = self._draft
        side = d["side"]
        qty = d["qty"]
        sym = d["symbol"]
        anchor = d["anchor"]
        offset = int(d["offset"] or 0)
        ord_type = d["ord_type"]
        label = d["label"]
        # Auto label when blank.
        text = label or _auto_hot_button_label(
            side, qty, sym, ord_type, anchor, offset,
            tp=d.get("tp"), sl=d.get("sl"))
        colour = _HOT_BUTTON_COLOURS.get(side, Theme.CLR_BUY)
        self._preview_btn.setText(text)
        self._preview_btn.setStyleSheet(
            f"QPushButton{{ background:{colour}; color:#ffffff;"
            " border:none; border-radius:6px;"
            " padding:6px 10px; font-weight:bold; font-size:11px; }}"
            f"QPushButton:disabled{{ background:{colour}; color:#ffffff; }}")
        # Summary line — mirrors the spec's preview text format.  For
        # CANCEL / FLATTEN the order-type / anchor / brackets aren't
        # meaningful so we keep the line short.
        sym_part = sym or "[selected sym]"
        qty_part = qty or "[selected qty]"
        group_v = d.get("group", "__current__")
        group_part = "[current group]" if group_v == "__current__" else group_v
        if side == "CANCEL":
            summary = f"CANCEL {sym_part} · group {group_part}"
            self._preview_line.setText(summary)
            return
        if side == "FLATTEN":
            summary = f"FLATTEN {sym_part} · group {group_part}"
        elif ord_type == "MKT":
            summary = (f"{side} {qty_part} {sym_part} · MKT · "
                       f"group {group_part}")
        else:
            sign = "+" if offset >= 0 else "-"
            summary = (
                f"{side} {qty_part} {sym_part} · LMT @ {anchor} "
                f"{sign}{abs(offset)}t · group {group_part}")
        attached = []
        if d["tp"]["on"]:
            attached.append(self._fmt_leg("TP", d["tp"]))
        if d["sl"]["on"]:
            attached.append(self._fmt_leg("SL", d["sl"]))
        if attached:
            summary += "\n↳ attached: " + " · ".join(attached)
        self._preview_line.setText(summary)

    @staticmethod
    def _fmt_leg(name: str, leg: dict) -> str:
        # Only "abs" (dollar offset) is supported now; legacy ticks /
        # pct entries are migrated to "abs" at draft load.
        v = leg.get("v", 0)
        sign = "+" if v >= 0 else "-"
        return f"{name} {sign}${abs(v):g}"


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

    # A saved futures contract was swapped for a different expiration via
    # the search popup — FlashTradeView re-points both panels' market
    # data at the new contract.
    contract_replaced = Signal(str)

    # Emitted when user clicks the Take Profit / Stop Loss header button.
    # Payload:
    #   {"side": "tp"|"sl", "symbol": str, "qty": int,
    #    "offset_pts": float, "last_price": float}
    # The consumer (FlashTradeView) queries each checked account for the
    # current position, sizes the order to min(|pos|, qty), computes the
    # target price from last_price ± offset, and submits a LMT (tp) or
    # STP/STP LMT (sl) exit order.  No-op for zero-position accounts.
    close_tier_requested = Signal(dict)

    # Hot-button group-targeted variants — payload includes the
    # explicit account list so FlashTradeView fans out to those
    # accounts regardless of the AccountListPanel selection.  Used
    # only when a hot button is configured with a named trading group
    # (BUY/SELL go through ``order_requested`` with a
    # ``_target_accounts`` payload field instead).
    hot_button_flatten = Signal(str, list)   # (symbol, accounts)
    hot_button_cancel = Signal(str, list)    # (symbol, accounts)

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

        # Symbol-chip grid → qty-chip row gap.  Tightened from 12 px
        # to 7 px (3/5) so the two strips read as a more connected
        # cluster without losing the visual separation.
        root.addSpacing(7)

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
            "Trailing", tr_tiers, label_color=Theme.CLR_CLOSE,
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
        # Wrap the three tier sections in their own VBox with tighter
        # spacing (root layout uses 8 px between siblings; here we
        # halve it to 4 px so TP / SL / Trail read as a connected
        # cluster) without affecting the surrounding gaps.
        tier_box = QVBoxLayout()
        tier_box.setContentsMargins(0, 0, 0, 0)
        tier_box.setSpacing(4)
        tier_box.addWidget(self._pt_section)
        tier_box.addWidget(self._sl_section)
        tier_box.addWidget(self._tr_section)
        root.addLayout(tier_box)

        # ── Hot buttons ───────────────────────────────────────────────────────
        # User-defined one-click order shortcuts.  Layout grows as the
        # user adds entries: configured slots render as coloured
        # buttons (green BUY / red SELL); a single trailing dashed-
        # border "+" placeholder always sits at the next free slot so
        # the user can keep adding.  Two columns per row.
        root.addSpacing(8)
        self._hot_grid = QGridLayout()
        # Match the Buy/Sell action-button grid spacing so each hot
        # button ends up the exact same width as a Buy/Sell button.
        self._hot_grid.setSpacing(4)
        self._hot_grid.setContentsMargins(0, 0, 0, 0)
        # Always reserve two equal-width columns so a lone "+" cell
        # in column 0 still occupies only half the panel width
        # (column 1 holds blank space until the user adds a second
        # hot button).  Without this Qt would collapse the empty
        # column and let the placeholder stretch full-width.
        self._hot_grid.setColumnStretch(0, 1)
        self._hot_grid.setColumnStretch(1, 1)
        # Restore the user's saved hot buttons; first launch returns
        # an empty list so the panel shows just the "+" placeholder.
        try:
            self._hot_buttons: list[dict] = config_manager.load_hot_buttons()
        except Exception:
            log.exception("HotButton: load failed; starting with empty list")
            self._hot_buttons = []
        hot_w = QWidget()
        hot_w.setStyleSheet("border: none;")
        hot_w.setContentsMargins(0, 0, 0, 0)
        hot_w.setLayout(self._hot_grid)
        # Hot buttons fill the full panel width (same as the Buy/Sell
        # action grid above) so each cell ends up at the same width
        # as a Buy/Sell button.
        root.addWidget(hot_w)
        self._rebuild_hot_buttons()

        root.addStretch(1)

    def _rebuild_hot_buttons(self) -> None:
        """Re-lay out the hot-button grid: one styled cell per saved
        entry plus a single trailing ``+`` placeholder.  Two columns
        per row.  Cheap to call (a handful of widgets, recreated)."""
        # Clear current children.
        while self._hot_grid.count():
            item = self._hot_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        # Render saved entries.
        for i, entry in enumerate(self._hot_buttons):
            row, col = divmod(i, 2)
            cell = self._make_hot_button(entry)
            cell.set_index(i)
            cell.editRequested.connect(self._on_hot_button_edit)
            cell.deleteRequested.connect(self._on_hot_button_delete)
            cell.reorderRequested.connect(self._on_hot_button_reorder)
            # Bind the index via a default-arg lambda so each cell
            # captures its own ``i`` instead of the loop's last value.
            cell.clicked.connect(
                lambda _chk=False, idx=i: self._on_hot_button_clicked(idx))
            self._hot_grid.addWidget(cell, row, col)
        # Trailing "+" placeholder always at the next free slot.  Also
        # accepts drops so the user can drag a saved cell here to move
        # it to the end of the list.
        n = len(self._hot_buttons)
        row, col = divmod(n, 2)
        slot = _HotButtonPlaceholder(self)
        slot.clicked.connect(self._on_add_hot_button)
        slot.dropFromCell.connect(self._on_hot_button_drop_to_end)
        self._hot_grid.addWidget(slot, row, col)

    def _make_hot_button(self, entry: dict) -> "_HotButtonCell":
        """Render a saved hot-button entry as a :class:`_HotButtonCell`.
        Side → colour mapping uses :data:`_HOT_BUTTON_COLOURS` so the
        styling matches the FlashTrade Buy/Sell/Flatten/Cancel action
        buttons.  Auto-generates the label from the other entry fields
        when the user left it blank.  The cell handles right-click
        Edit/Delete + drag-and-drop reorder; left-click submission is
        intentionally a no-op for now — order routing is deferred."""
        side = entry.get("side", "BUY")
        label = entry.get("label", "") or _auto_hot_button_label(
            side=side,
            qty=entry.get("qty", ""),
            sym=entry.get("symbol", ""),
            ord_type=entry.get("ord_type", "LMT"),
            anchor=entry.get("anchor", "BID"),
            offset=int(entry.get("offset", 0) or 0),
            tp=entry.get("tp"),
            sl=entry.get("sl"),
        )
        is_disabled = bool(entry.get("disabled", False))
        # Disabled buttons render in a neutral grey so the user sees the
        # underlying contract is gone.  We do NOT call setEnabled(False)
        # because that would also block right-click — the user must still
        # be able to Edit / Delete the cell from the context menu.  The
        # left-click submission is short-circuited in
        # ``_on_hot_button_clicked`` when ``disabled`` is True.
        colour = "#484F58" if is_disabled else _HOT_BUTTON_COLOURS.get(
            side, Theme.CLR_BUY)
        cell = _HotButtonCell(label, colour, parent=self)
        if is_disabled:
            cell.setCursor(Qt.CursorShape.ArrowCursor)
            cell.setToolTip(
                f"Disabled — saved contract for '{entry.get('symbol', '')}' "
                f"was deleted.  Right-click to Edit or Delete, or re-add "
                f"the contract.")
            return cell
        # Tooltip surfaces the resolved localSymbol so the user can see
        # which actual contract a root-symbol button (e.g. "NQ") will
        # trade right now (e.g. "NQM6").  Updates on every rebuild, so
        # rolling the contract row in Contracts View immediately reflects
        # here without re-defining the button.
        tip = self._hot_button_tooltip(entry)
        if tip:
            cell.setToolTip(tip)
        return cell

    def reload_hot_buttons(self) -> None:
        """Reload the hot-button list from disk and rebuild the grid.
        Used by ContractsView after a contract deletion that updated
        related hot-button symbols (auto-rolled or disabled)."""
        try:
            self._hot_buttons = config_manager.load_hot_buttons()
        except Exception:
            log.exception("HotButton: reload failed; keeping in-memory list")
            return
        self._rebuild_hot_buttons()

    def _hot_button_tooltip(self, entry: dict) -> str:
        """Build the hover tooltip describing the contract a hot button
        currently resolves to — including which front-month is active
        when the button targets a futures root with multiple saved
        expirations.  Empty when there's no symbol or no extra info to
        surface (e.g. stocks where local_symbol matches the ticker)."""
        sym = (entry.get("symbol", "") or "").strip().upper()
        if not sym:
            return ""
        try:
            from src.core.reference_data import get_instance as _rd
            c = _rd().pick_active_contract(sym)
        except Exception:
            return ""
        if not c:
            return f"{sym}: no saved contract — add one in Contracts"
        local = (c.get("local_symbol") or "").strip()
        expiry = (c.get("expiry") or "").strip()
        if not local or local.upper() == sym:
            return ""
        if expiry:
            return f"{sym} → {local}  (expiry {expiry})"
        return f"{sym} → {local}"

    # ── Hot-button host actions ──────────────────────────────────────────────

    def _persist_hot_buttons(self) -> None:
        """Atomically write the current ``_hot_buttons`` list to
        ``app_config.json``.  Called after every mutation (add / edit /
        delete / reorder) so the user's hot-button layout survives a
        restart.  Failures are logged but not propagated — a write
        problem must never break order entry."""
        try:
            config_manager.save_hot_buttons(self._hot_buttons)
        except Exception:
            log.exception("HotButton: failed to persist to app_config.json")

    def _on_add_hot_button(self) -> None:
        """Open :class:`_HotButtonModal` in Add mode, append the
        resulting HotButton dict to the in-memory list, and rebuild
        the grid so the next ``+`` placeholder appears in the next
        free slot.

        Add-mode UX:
          * Symbol / Qty mode segments default to ``SPECIFIC`` with
            *empty* inputs (force-set via ``default_specific=True``) so
            the user types directly into the inputs without first
            flipping a segment toggle.
          * Order type defaults to ``MKT``.
          * The label field auto-fills as the user types — see
            ``_HotButtonModal._refresh_auto_label_field``.
        """
        draft = _hot_button_default_draft()
        draft["ord_type"] = "MKT"
        dlg = _HotButtonModal(self, draft=draft, default_specific=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.delete_requested():
            return  # Delete in Add mode is a no-op
        values = dlg.values()
        self._hot_buttons.append(values)
        log.info("HotButton: added side=%s label=%r ord_type=%s",
                 values.get("side"), values.get("label"),
                 values.get("ord_type"))
        self._rebuild_hot_buttons()
        self._persist_hot_buttons()

    def _on_hot_button_edit(self, index: int) -> None:
        """Right-click → Edit → reopen the modal in Edit mode for the
        cell at *index*.  On Save, replace the entry in place; on
        Delete (modal footer), drop the entry."""
        if not (0 <= index < len(self._hot_buttons)):
            return
        dlg = _HotButtonModal(
            self, draft=self._hot_buttons[index], edit_mode=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.delete_requested():
            log.info("HotButton: deleted index=%d via modal", index)
            del self._hot_buttons[index]
        else:
            new_entry = dlg.values()
            # Editing a disabled cell implies the user is fixing the
            # broken reference (typically by changing the symbol to one
            # that has a saved contract).  Clear the disabled flag so
            # the cell goes back to its normal coloured state — if the
            # new symbol is still bad, the user will see no orders fire
            # and can re-edit.
            new_entry["disabled"] = False
            self._hot_buttons[index] = new_entry
            log.info("HotButton: edited index=%d", index)
        self._rebuild_hot_buttons()
        self._persist_hot_buttons()

    def _on_hot_button_delete(self, index: int) -> None:
        """Right-click → Delete → remove the cell at *index*."""
        if not (0 <= index < len(self._hot_buttons)):
            return
        log.info("HotButton: deleted index=%d", index)
        del self._hot_buttons[index]
        self._rebuild_hot_buttons()
        self._persist_hot_buttons()

    def _on_hot_button_reorder(self, src: int, dst: int) -> None:
        """Drag-and-drop reorder: move the entry at *src* to the slot
        currently occupied by *dst*, sliding the rest down.  No-op
        when src == dst or when either index is out of range."""
        n = len(self._hot_buttons)
        if src == dst or not (0 <= src < n) or not (0 <= dst < n):
            return
        item = self._hot_buttons.pop(src)
        # ``dst`` was computed before the pop, so adjust if the source
        # sat earlier in the list.
        if dst > src:
            dst -= 1
        self._hot_buttons.insert(dst, item)
        log.info("HotButton: reorder src=%d → dst=%d", src, dst)
        self._rebuild_hot_buttons()
        self._persist_hot_buttons()

    def _on_hot_button_drop_to_end(self, src: int) -> None:
        """Drag-and-drop onto the trailing ``+`` placeholder → move
        the entry at *src* to the end of the list."""
        n = len(self._hot_buttons)
        if not (0 <= src < n) or src == n - 1:
            return
        item = self._hot_buttons.pop(src)
        self._hot_buttons.append(item)
        log.info("HotButton: moved src=%d to end", src)
        self._rebuild_hot_buttons()
        self._persist_hot_buttons()

    def _resolve_hot_button_target_accounts(
            self, group: str) -> list[str] | None:
        """Resolve the target-account list for a hot button's trading
        group.  Returns:

        * ``None`` for the ``__current__`` sentinel — caller falls
          through to whatever the AccountListPanel currently has
          selected (preserves the existing OrderEntry semantics).
        * The group's currently-checked subset (``selected`` field)
          when *group* names a known user-defined group from
          ``app_config.json``.  An empty list means the user has
          nothing checked in that group right now; an empty list is
          also returned when the group can't be found at all (the
          caller treats both as "abort with warning").
        """
        if not group or group == "__current__":
            return None
        try:
            groups = config_manager.load_account_groups()
        except Exception:
            log.exception(
                "HotButton: failed to load account_groups from config")
            return []
        for g in groups:
            if g.get("name") == group:
                # Prefer the currently-checked subset so a hot button
                # that targets "Beta" matches what the user sees
                # checked while Beta is active in the AccountListPanel.
                # Fall back to the full membership only when
                # ``selected`` is missing entirely (legacy configs
                # that haven't been migrated yet).
                sel = g.get("selected")
                if isinstance(sel, list):
                    return [a for a in sel if a]
                return [a for a in g.get("accounts", []) if a]
        log.warning(
            "HotButton: group %r not found in account_groups config", group)
        return []

    def _on_hot_button_clicked(self, index: int) -> None:
        """Hot-button click → build and submit an order from the saved
        entry.  Mirrors the ``_emit_*`` helpers used by the explicit
        Buy/Sell/Flatten/Cancel buttons so the routing in
        :class:`FlashTradeView` is the same regardless of source.

        Resolution rules:
          * ``symbol`` empty → fall back to ``self._active_symbol``.
          * ``qty`` empty / non-integer → fall back to ``self._active_qty``.
          * ``ord_type == "MKT"`` → emit ``type="MKT"``.
          * ``ord_type == "LMT"`` → compute
            ``limit_price = anchor_base + offset * tick`` (anchor base
            from the live bid/ask/mid cache, falls back to last) and
            emit ``type="BID_LMT"``/``"ASK_LMT"`` so FlashTradeView
            routes a plain LMT at the explicit price.
          * ``side == "FLATTEN"`` → emit
            :pyattr:`flatten_requested(symbol)` for ``__current__``,
            or :pyattr:`hot_button_flatten(symbol, accounts)` when a
            named group resolves to a non-empty account list.
          * ``side == "CANCEL"`` → :pyattr:`cancel_all_requested` /
            :pyattr:`hot_button_cancel` mirror the same rule.
          * Attached ``tp`` / ``sl`` legs (``u == "abs"``) → translate
            to the existing ``{value, unit}`` payload using ``"price"``
            (dollar offset) so the worker's bracket builder reuses the
            same path the OrderEntry tier sections use.

        Trading group routing: ``__current__`` sentinel uses the
        AccountListPanel selection (existing behaviour).  Named groups
        bypass that selection and fan out to every member of the
        group, so a hot button still fires the right accounts even if
        the user has switched the AccountListPanel to ``None`` /
        another group.
        """
        if not (0 <= index < len(self._hot_buttons)):
            return
        # Disabled buttons are greyed out via setEnabled(False), but the
        # guard here is a belt-and-braces backstop: if a future change
        # paths a click around the QPushButton enabled state, we still
        # refuse to route an order against a deleted contract.
        if self._hot_buttons[index].get("disabled"):
            log.info("HotButton[%d]: click ignored — entry is disabled",
                     index)
            return
        entry = self._hot_buttons[index]
        side = str(entry.get("side", "BUY"))

        # Symbol resolution — fall back to the panel's focused symbol.
        sym_raw = str(entry.get("symbol", "") or "").strip()
        sym = (sym_raw or (self._active_symbol or "")).upper()
        if not sym:
            log.warning(
                "HotButton[%d]: no symbol (entry empty + no active symbol)",
                index)
            return

        # Group routing — resolve target accounts for named groups.
        group = str(entry.get("group", "__current__") or "__current__")
        target_accounts = self._resolve_hot_button_target_accounts(group)
        if group != "__current__":
            if not target_accounts:
                log.warning(
                    "HotButton[%d]: group %r resolves to no accounts — "
                    "aborting submission so we don't silently fall back "
                    "to the AccountListPanel selection.",
                    index, group)
                return
            log.info("HotButton[%d]: group %r → %d target account(s): %s",
                     index, group, len(target_accounts),
                     sorted(target_accounts))

        # ── CANCEL / FLATTEN — position-management actions ────────────────
        if side == "CANCEL":
            if target_accounts:
                log.info("HotButton[%d]: CANCEL %s on group %r",
                         index, sym, group)
                self.hot_button_cancel.emit(sym, list(target_accounts))
            else:
                log.info("HotButton[%d]: CANCEL %s", index, sym)
                self.cancel_all_requested.emit(sym)
            return
        if side == "FLATTEN":
            if target_accounts:
                log.info("HotButton[%d]: FLATTEN %s on group %r",
                         index, sym, group)
                self.hot_button_flatten.emit(sym, list(target_accounts))
            else:
                log.info("HotButton[%d]: FLATTEN %s", index, sym)
                self.flatten_requested.emit(sym)
            return

        # ── BUY / SELL — qty resolution ───────────────────────────────────
        raw_qty = str(entry.get("qty", "") or "").strip()
        qty: int | None = None
        if raw_qty:
            # "100" supported now; "25%" is in the spec but resolution
            # against position size lives in a follow-up — fall back to
            # the active chip qty so the click still does *something*.
            try:
                qty = int(raw_qty)
            except ValueError:
                log.info(
                    "HotButton[%d]: qty=%r not integer; falling back "
                    "to active chip qty=%d",
                    index, raw_qty, self._active_qty)
        if qty is None or qty <= 0:
            qty = self._active_qty
        if qty <= 0:
            log.warning("HotButton[%d]: qty resolves to 0 — aborting", index)
            return

        ord_type = str(entry.get("ord_type", "LMT"))
        sec_type = _sec_type_for(sym)
        # Per-symbol price cache lookup — critical when the hot
        # button targets a *different* symbol than the OrderEntry's
        # active one.  ``self._last_*`` only reflect the focused
        # symbol; without this lookup an "ES" hot button clicked
        # while NQ is focused would compute an NQ-priced LMT against
        # the ES contract.
        cached = self._price_cache.get(sym.upper())
        if cached:
            sym_last, sym_bid, sym_ask = cached
        else:
            sym_last, sym_bid, sym_ask = 0.0, 0.0, 0.0
        payload: dict = {
            "action": side,
            "symbol": sym,
            "qty": qty,
            "sec_type": sec_type,
            "last_price": float(sym_last),
        }
        # Group-targeted hot buttons override the AccountListPanel
        # selection so the order fires on the configured group's
        # accounts even when the user has the panel set to None /
        # another group.  Read by FlashTradeView._on_order_requested.
        if target_accounts:
            payload["_target_accounts"] = list(target_accounts)

        if ord_type == "MKT":
            payload["type"] = "MKT"
        else:
            anchor = str(entry.get("anchor", "BID"))
            try:
                offset_ticks = int(entry.get("offset", 0) or 0)
            except (TypeError, ValueError):
                offset_ticks = 0
            base = self._anchor_base_price_for(
                anchor, sym_bid, sym_ask, sym_last)
            if base <= 0:
                log.warning(
                    "HotButton[%d]: live %s base price unavailable for "
                    "%s — skipping LMT submission",
                    index, anchor, sym)
                return
            tick = self._tick_size_for_symbol(sym)
            limit_price = base + offset_ticks * tick
            if tick > 0:
                # Snap to a tick boundary so the broker doesn't reject.
                limit_price = round(limit_price / tick) * tick
            payload["type"] = "BID_LMT" if side == "BUY" else "ASK_LMT"
            payload["limit_price"] = float(limit_price)

        # ── Attached brackets ─────────────────────────────────────────────
        payload["profit_take"] = self._bracket_payload(entry.get("tp"))
        payload["stop_loss"] = self._bracket_payload(entry.get("sl"))
        payload["trailing"] = None  # hot buttons don't expose a trail leg

        log.info(
            "HotButton[%d]: emit order side=%s qty=%d sym=%s type=%s "
            "limit=%s tp=%s sl=%s",
            index, side, qty, sym, payload.get("type"),
            payload.get("limit_price"),
            payload.get("profit_take"), payload.get("stop_loss"))
        self.order_requested.emit(payload)

    @staticmethod
    def _anchor_base_price_for(
            anchor: str, bid: float, ask: float, last: float) -> float:
        """Resolve the anchor (BID/ASK/MID) to a usable base price from
        an explicit ``(bid, ask, last)`` triple.  The caller picks the
        triple — for hot buttons that's the per-symbol price cache,
        not the panel's focused-symbol values, so a click on an "ES"
        hot button uses ES quotes even while the panel is showing NQ.
        Falls back to ``last`` when the anchor side isn't streaming
        (e.g. overnight sessions)."""
        if anchor == "BID":
            base = bid
        elif anchor == "ASK":
            base = ask
        elif anchor == "MID" and bid > 0 and ask > 0:
            base = (bid + ask) / 2.0
        else:
            base = 0.0
        if math.isnan(base) or base <= 0:
            base = last
        return float(base) if not math.isnan(base) else 0.0

    @staticmethod
    def _bracket_payload(leg: dict | None) -> dict | None:
        """Translate a hot-button bracket leg (``{on, v, u}``) into the
        ``{value, unit}`` shape the worker's bracket builder expects.
        ``u == "abs"`` is mapped to ``"price"`` (dollar offset) since
        the existing bracket-children path treats those as equivalent.
        Returns ``None`` when the leg is off / missing."""
        if not isinstance(leg, dict) or not leg.get("on"):
            return None
        u = leg.get("u", "abs")
        unit = "price" if u == "abs" else u
        return {
            "value": float(leg.get("v", 0) or 0),
            "unit": unit,
            "label": "HotButton",
        }

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
        attach_exchange_badge(btn, sym)
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

    def refresh_symbol_contract(self, sym: str) -> None:
        """Re-point *sym*'s market-data stream at its (possibly replaced)
        saved contract.  The bg-subscription skip-list is keyed by
        symbol, so an unsubscribe → subscribe cycle is required for the
        worker to pick up the new conId."""
        sym = (sym or "").upper()
        if sym not in self._symbols:
            return
        self._price_cache.pop(sym, None)
        if (self._bg_worker is not None
                and self._bg_worker.isRunning()
                and self._bg_worker.is_ib_connected):
            self._bg_worker.unsubscribe_background_symbol(sym)
            params = self._build_bg_params_for_symbol(sym)
            if params:
                self._bg_worker.subscribe_background_symbols([params])
        if self._active_symbol == sym:
            self._update_symbol_display()
            self._refresh_action_labels()
            self._reload_tier_sections()

    def _remove_symbol(self, sym: str) -> None:
        """Right-click removes a symbol button."""
        if sym in self._symbols:
            self._symbols.remove(sym)
            config_manager.save_order_entry_symbols(self._symbols)
            # Drop the STK contract from the universe too, unless the
            # Book Trader chip bar or a hot button still needs it.
            config_manager.remove_stock_contract_if_unused(sym)
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
        """Build market data params for a single symbol from the reference cache.

        Routes through ``pick_active_contract`` so a root like ``NQ`` with
        multiple saved expirations resolves to the front-month
        automatically — keeping the panel's market-data subscription on
        the same contract that order entry will trade against.
        """
        from src.core.reference_data import get_instance as _rd
        c = _rd().pick_active_contract(sym)
        if not c:
            return None
        conid = c.get("conid")
        if not conid:
            return None
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

    def open_symbol_search(self) -> None:
        """Public entry point for the global Shift+/ shortcut (MainWindow)."""
        self._show_add_symbol_menu()

    def _show_add_symbol_menu(self) -> None:
        """Searchable add-symbol popup: saved contracts appear instantly
        and, as the user types, live IBKR stock matches stream in via
        reqMatchingSymbols — no need to add the stock on the Contracts
        tab first.  Picking an IBKR row auto-saves the contract, then
        the symbol is added via the existing ``_add_symbol`` path."""
        from src.ui.widgets.symbol_search_popup import (
            SymbolSearchPopup,
            apply_future_pick,
            connected_workers_by_priority,
            ensure_contract_saved,
        )
        dlg = SymbolSearchPopup(
            self,
            workers=connected_workers_by_priority(self._active_threads),
            exclude={s.upper() for s in self._symbols})

        def _picked(entry: dict) -> None:
            if (entry.get("source") == "ibkr"
                    and entry.get("sec_type") == "FUT"):
                sym, replaced = apply_future_pick(self, entry)
                if not sym:
                    return  # user declined the replacement
                self._add_symbol(sym)
                if replaced:
                    self.contract_replaced.emit(sym)
                return
            sym = ensure_contract_saved(entry)
            if sym:
                self._add_symbol(sym)

        dlg.symbol_selected.connect(_picked)
        dlg.open_below(self._btn_add_symbol)

    def _update_bid_ask_buttons(self, bid: float, ask: float) -> None:
        """Keep @Bid / @Ask button labels in sync with the current quantity.

        Prices are shown in the bid/ask label beneath the main price display,
        not on the buttons themselves, so the buttons stay concise.
        """
        self._refresh_action_labels()
