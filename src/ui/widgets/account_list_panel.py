from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, QMimeData, QModelIndex, QRect, QRectF, QTimer
from PySide6.QtGui import QDrag, QFont, QPainter, QPen, QColor, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .accounts_model import (
    AccountsTableModel,
    COL_ACCOUNT,
    COL_ALIAS,
    COL_NET_LIQ,
    COL_TOTAL_CASH,
    COL_TYPE,
)
from src.core import config_manager
from src.ui.theme import hover_shades

log = logging.getLogger(__name__)

# ── Colours (matching order_entry_panel) ──────────────────────────────────────
BG_PANEL = "#151922"
BG_ITEM_ACTIVE = "#1a2040"
BG_ITEM_INACTIVE = "#242424"
BORDER_ACTIVE = "#2a3ab0"
BORDER_INACTIVE = "#333333"
BORDER = "#262E3F"
TEXT = "#E6EBF2"
TEXT_DIM = "#A3AFC2"

CLR_FLATTEN = "#E08A3C"
CLR_CLOSE = "#6E757E"
CLR_REVERSE = "#A371F7"
CLR_CANCEL = "#3A414C"

# Orange accent matches the Flatten button + trades control bar.
ACCENT = "#E08A3C"
ACCENT_DIM = "#B8722F"

# Filter-strip chip palette — same blue-selected / grey-unselected
# pattern as the symbol chips in OrderEntryPanel so the two strips
# read as the same affordance.
_CHIP_SELECTED_BG    = "#539BF5"
_CHIP_SELECTED_HOVER = "#6BA8F6"
_CHIP_IDLE_BG        = "#232A3B"
_CHIP_IDLE_HOVER     = "#334058"

# ── Account-card design tokens (ACCOUNT_CARD_SPEC.md, dark theme) ──────────────
_CARD_SURFACE      = "#1B2130"   # --surface  (card background)
_CARD_SURFACE3     = "#232A3B"   # --surface-3 (close hover)
_CARD_SEL_BG       = "#1C2740"   # accent-soft (0.12) pre-blended over --surface
_CARD_TEXT         = "#E6EBF2"   # --text   (name, total amount)
_CARD_TEXT2        = "#A3AFC2"   # --text-2 (balance values)
_CARD_TEXT3        = "#6A7690"   # --text-3 (currency codes, caps, close)
_CARD_BORDER2      = "#334058"   # --border-2 (idle frame, empty checkbox)
_CARD_ACCENT       = "#539BF5"   # --accent  (selected frame + checkbox fill)
_CARD_LOSS         = "#fb6f84"   # --loss    (negative figures)
_CARD_GAIN         = "#2EA043"   # --gain    (positive USD balance, matches CLR_PNL_POS)
# Offline (disconnected) dim variants.
_CARD_OFF_PRIMARY  = "#6A7690"
_CARD_OFF_SECOND   = "#334058"

# Real minus sign (U+2212) for negative figures, per spec.
_MINUS = "−"


def _apply_filter_chip_style(btn: QPushButton, selected: bool) -> None:
    """Apply chip styling to a filter-strip button — blue when selected,
    grey otherwise.  Mirrors ``_apply_chip_style`` in order_entry_panel
    so the Accounts filter strip and the symbol chips look identical."""
    if selected:
        btn.setStyleSheet(
            "QPushButton{background:" + _CHIP_SELECTED_BG +
            ";color:white;border:none;border-radius:6px;"
            "padding:4px 12px;font-weight:bold;font-size:12px}"
            "QPushButton:hover{background:" + _CHIP_SELECTED_HOVER + "}")
    else:
        btn.setStyleSheet(
            "QPushButton{background:" + _CHIP_IDLE_BG +
            ";color:#A3AFC2;border:none;border-radius:6px;"
            "padding:4px 12px;font-weight:600;font-size:12px}"
            "QPushButton:hover{background:" + _CHIP_IDLE_HOVER + "}")

_TITLE_FONT = QFont("Inter, Helvetica Neue, Arial", 11)

# Group-pill grid layout: 3 rows × 4 columns.  The first two slots
# (row 0, cols 0 and 1) are reserved for the built-in All / None
# quick-select pills, so user-defined groups get the remaining 10.
_GROUP_COLS = 4
_GROUP_ROWS = 3
_GROUP_DEFAULT_SLOTS = 2
_GROUP_MAX = _GROUP_COLS * _GROUP_ROWS - _GROUP_DEFAULT_SLOTS

# MIME type for drag-to-reorder of trading-group pills.
_GROUP_MIME = "application/x-nanopulse-group"

# Built-in quick-select groups rendered in the panel header.  Their
# names are reserved — user-defined groups may not shadow them.
# "All"  → every model account, every row checked.
# "None" → every model account, every row unchecked.
_DEFAULT_GROUPS: tuple[str, ...] = ("All", "None")
_TITLE_FONT.setWeight(QFont.Weight.Bold)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _btn_qss(colour: str, fg: str = "#ffffff") -> str:
    hover, pressed = hover_shades(colour)
    return f"""
        QPushButton {{
            background: {colour};
            color: {fg};
            border: none;
            border-radius: 4px;
            padding: 4px 2px;
            font-weight: bold;
            font-size: 11px;
        }}
        QPushButton:hover {{ background: {hover}; }}
        QPushButton:pressed {{ background: {pressed}; }}
    """


def _make_btn(text: str, colour: str, min_h: int = 30,
              fg: str = "#ffffff") -> QPushButton:
    """Flat action button – similar to order_entry_panel but tuned
    tighter so the 2×3 action grid barely constrains the panel's
    minimum width.  Horizontal padding and font size are both pulled
    in step-by-step (orig 11/8, then 10/3, now 8/2) to let the
    AccountListPanel splitter drag further left without touching the
    button labels."""
    btn = QPushButton(text)
    btn.setMinimumHeight(min_h)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    btn.setStyleSheet(_btn_qss(colour, fg))
    return btn


def _h_divider() -> QWidget:
    """Thin horizontal grey divider."""
    line = QWidget()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {BORDER};")
    return line


# ── Custom Checkbox (painted checkmark) ───────────────────────────────────────

class _CheckmarkBox(QWidget):
    """A custom-painted checkbox (ACCOUNT_CARD_SPEC.md):
    unchecked = 26×26 rounded square, 2px ``--border-2`` outline, empty;
    checked   = filled ``--accent`` square with a white checkmark."""

    toggled = Signal(bool)

    def __init__(self, checked: bool = True, parent=None):
        super().__init__(parent)
        self._checked = checked
        # Dim the fill/outline when the row's worker is offline.
        self._enabled_look = True
        self.setFixedSize(20, 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, v: bool) -> None:
        if self._checked != v:
            self._checked = v
            self.update()

    def set_enabled_look(self, on: bool) -> None:
        if self._enabled_look != bool(on):
            self._enabled_look = bool(on)
            self.update()

    def mousePressEvent(self, event) -> None:
        self._checked = not self._checked
        self.update()
        self.toggled.emit(self._checked)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        radius = 6.0
        rect = QRectF(1.5, 1.5, self.width() - 3.0, self.height() - 3.0)
        w = self.width()

        if self._checked:
            # Filled accent square …
            fill = _CARD_ACCENT if self._enabled_look else _CARD_OFF_SECOND
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(fill))
            p.drawRoundedRect(rect, radius, radius)

            # … with a white checkmark (polyline 20 6 · 9 17 · 4 12).
            pen = QPen(QColor("#ffffff" if self._enabled_look else "#cdd1e6"))
            pen.setWidthF(2.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath()
            path.moveTo(w * 0.30, w * 0.52)
            path.lineTo(w * 0.43, w * 0.66)
            path.lineTo(w * 0.72, w * 0.34)
            p.drawPath(path)
        else:
            # Empty square with a 2px outline.
            outline = _CARD_BORDER2 if self._enabled_look else _CARD_OFF_SECOND
            pen = QPen(QColor(outline))
            pen.setWidthF(2.0)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect, radius, radius)

        p.end()


# ── Account Row (Card) ────────────────────────────────────────────────────────

class _SweepMenu(QMenu):
    """Cash-sweep context menu that reports a right-click landing outside
    it.  A popup QMenu grabs the mouse, so that click would otherwise only
    dismiss the menu and the user would have to right-click the other
    account row a second time — the panel listens to this signal and
    reopens the menu on the row under the cursor in one step."""

    right_pressed_outside = Signal(object)   # global QPoint

    def mousePressEvent(self, ev):
        if (ev.button() == Qt.MouseButton.RightButton
                and not self.rect().contains(ev.position().toPoint())):
            global_pos = ev.globalPosition().toPoint()
            self.close()
            self.right_pressed_outside.emit(global_pos)
            return
        super().mousePressEvent(ev)


class _AccountRow(QFrame):
    """A single account card: checkbox | name | balance | × button."""

    toggled = Signal(str, bool)
    removed = Signal(str)
    context_menu_requested = Signal(str, object)   # account, global QPoint

    def __init__(self, account: str, alias: str, balance: str,
                 acct_type: str, checked: bool = True, parent=None):
        super().__init__(parent)
        self._account = account
        self._checked = checked
        # Live TWS connection state for this row's account.  Defaults to
        # True so a brand-new row isn't dimmed before the first
        # ``set_connected()`` call.  Updated by AccountListPanel on
        # ``dataChanged`` from AccountsTableModel.
        self._connected: bool = True
        # Plain display name without the "WARN " offline prefix; kept so
        # ``set_connected(True)`` can restore the original text.
        self._base_name: str = alias if alias else account
        # USD cash balance (shown under the name) + base-currency total sign.
        self._usd: float | None = None
        self._balance_neg: bool = self._parse_neg(balance)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Right-click → cash-sweep context menu (built by AccountListPanel,
        # which has the balances + sweep-symbol quote).
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda p: self.context_menu_requested.emit(
                self._account, self.mapToGlobal(p)))

        # Faithful 2-row grid (ACCOUNT_CARD_SPEC.md):
        #   col0 checkbox (spans both rows) | col1 left column (name / USD bal,
        #   1fr) | col2 total (top-right) | col3 close (spans both rows).
        grid = QGridLayout(self)
        grid.setContentsMargins(10, 5, 10, 5)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(0)
        grid.setColumnStretch(1, 1)   # the 1fr middle column

        self._cb = _CheckmarkBox(checked)
        self._cb.toggled.connect(self._on_toggle)
        grid.addWidget(self._cb, 0, 0, 2, 1, Qt.AlignmentFlag.AlignVCenter)

        # Name — row 0, bottom-left so it hugs the USD line below it.
        self._name_lbl = QLabel(self._base_name)
        self._name_lbl.setFont(self._ui_font(11))
        grid.addWidget(
            self._name_lbl, 0, 1,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)

        # USD balance sub-line — row 1, top-left.  "USD" code + mono value.
        self._sub_container = QWidget()
        self._sub_container.setStyleSheet("background: transparent;")
        sub = QHBoxLayout(self._sub_container)
        sub.setContentsMargins(0, 0, 0, 0)
        sub.setSpacing(5)
        self._sub_code = QLabel("USD")
        self._sub_code.setFont(self._ui_font(10))
        sub.addWidget(self._sub_code, 0, Qt.AlignmentFlag.AlignBottom)
        self._sub_val = QLabel("")
        self._sub_val.setFont(self._mono_font(10, demibold=True))
        sub.addWidget(self._sub_val, 0, Qt.AlignmentFlag.AlignBottom)
        self._sub_container.hide()
        grid.addWidget(
            self._sub_container, 1, 1,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        # Total — row 0, bottom-right (baseline-aligned with the name).
        total = QWidget()
        total.setStyleSheet("background: transparent;")
        total_row = QHBoxLayout(total)
        total_row.setContentsMargins(0, 0, 0, 0)
        total_row.setSpacing(4)
        self._balance_lbl = QLabel(self._format_balance(balance))
        self._balance_lbl.setFont(self._mono_font(11))
        total_row.addWidget(self._balance_lbl, 0, Qt.AlignmentFlag.AlignBottom)
        self._cap_lbl = QLabel("USD")
        self._cap_lbl.setFont(self._ui_font(10))
        total_row.addWidget(self._cap_lbl, 0, Qt.AlignmentFlag.AlignBottom)
        grid.addWidget(
            total, 0, 2,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)

        # Close button — col 3, spanning both rows, vertically centered.
        self._x_btn = QPushButton("×", self)
        self._x_btn.setFixedSize(22, 22)
        self._x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._x_btn.setStyleSheet(
            "QPushButton {"
            f"  background: transparent; color: {_CARD_TEXT3};"
            "  border: none; border-radius: 6px;"
            "  font-size: 15px; font-weight: bold;"
            "}"
            "QPushButton:hover {"
            f"  background: {_CARD_SURFACE3}; color: {_CARD_TEXT};"
            "}")
        self._x_btn.clicked.connect(lambda: self.removed.emit(self._account))
        grid.addWidget(self._x_btn, 0, 3, 2, 1, Qt.AlignmentFlag.AlignVCenter)

        self._apply_card_style()

    # -- Properties ------------------------------------------------------------

    @property
    def account(self) -> str:
        return self._account

    @property
    def is_checked(self) -> bool:
        return self._checked

    def set_balance(self, balance: str) -> None:
        """Update the big balance label.  Skips the setText and — more
        importantly — the setStyleSheet (a full QSS re-polish) when
        nothing changed: the 250 ms summary batches re-send every tag,
        so most calls are no-ops."""
        neg = self._parse_neg(balance)
        text = self._format_balance(balance)
        if text == self._balance_lbl.text() and neg == self._balance_neg:
            return
        self._balance_neg = neg
        self._balance_lbl.setText(text)
        self._balance_lbl.setStyleSheet(
            f"color: {self._big_color()}; background: transparent;")

    def set_cash(self, cash: dict[str, float]) -> None:
        """Pick out the USD cash balance and render it under the name.  No
        USD entry hides the sub-line.  No-op when the value is unchanged."""
        usd = cash.get("USD")
        new = float(usd) if usd is not None else None
        if new == self._usd:
            return
        self._usd = new
        self._refresh_usd()

    def set_name(self, alias: str) -> bool:
        """Set the display name; returns True when it actually changed
        (the caller reorders rows only then — sort order is name-based)."""
        base = alias if alias else self._account
        if base == self._base_name:
            return False
        self._base_name = base
        self._refresh_name_label()
        return True

    def set_checked(self, checked: bool) -> None:
        self._checked = checked
        self._cb.setChecked(checked)
        self._apply_card_style()

    def set_connected(self, connected: bool) -> None:
        """Toggle the row's offline appearance.  When ``connected`` is
        False the name label is prefixed with a warning glyph, every label
        is dimmed, and a tooltip explains that orders will be skipped on
        this account.  When True, restores the live styling."""
        if self._connected == bool(connected):
            return
        self._connected = bool(connected)
        self._refresh_name_label()
        self._apply_card_style()
        if self._connected:
            self.setToolTip("")
        else:
            self.setToolTip(
                "Connection disconnected — orders will be skipped on this "
                "account until the connection is restored.")

    # -- Internal --------------------------------------------------------------

    def _refresh_name_label(self) -> None:
        if self._connected:
            self._name_lbl.setText(self._base_name)
        else:
            self._name_lbl.setText(f"⚠ {self._base_name}")

    def _on_toggle(self, checked: bool) -> None:
        self._checked = checked
        self._apply_card_style()
        self.toggled.emit(self._account, checked)

    @staticmethod
    def _ui_font(pt: int) -> QFont:
        # Same family/point-sizing as the positions view title font.
        f = QFont("Inter, Helvetica Neue, Arial", pt)
        f.setWeight(QFont.Weight.Bold)
        return f

    @staticmethod
    def _mono_font(pt: int, demibold: bool = False) -> QFont:
        # Same family/point-sizing as the positions view table/value font.
        f = QFont("JetBrains Mono, Menlo, Consolas, monospace", pt)
        f.setWeight(QFont.Weight.DemiBold if demibold else QFont.Weight.Bold)
        return f

    def _big_color(self) -> str:
        if not self._connected:
            return _CARD_OFF_PRIMARY
        return _CARD_LOSS if self._balance_neg else _CARD_TEXT

    def _apply_card_style(self) -> None:
        if not self._connected:
            # Offline -- dashed frame so the row reads as inert regardless of
            # the checked state.
            border = f"2px dashed {_CARD_BORDER2}"
            bg = _CARD_SURFACE
        elif self._checked:
            # Selected — keep the idle border colour (no accent ring); the
            # filled checkbox + tinted background carry the selected state.
            border = f"2px solid {_CARD_BORDER2}"
            bg = _CARD_SEL_BG
        else:
            border = f"2px solid {_CARD_BORDER2}"
            bg = _CARD_SURFACE
        self.setStyleSheet(
            "_AccountRow {"
            f"  background-color: {bg};"
            f"  border: {border};"
            "  border-radius: 10px;"
            "}")
        self._refresh_colors()

    def _refresh_colors(self) -> None:
        """Recolour the name, total, cap, checkbox, and USD sub-line for the
        current online/offline state."""
        online = self._connected
        name_c = _CARD_TEXT if online else _CARD_OFF_PRIMARY
        cap_c = _CARD_TEXT3 if online else _CARD_OFF_SECOND
        self._name_lbl.setStyleSheet(
            f"color: {name_c}; background: transparent;")
        self._cap_lbl.setStyleSheet(
            f"color: {cap_c}; background: transparent;")
        self._balance_lbl.setStyleSheet(
            f"color: {self._big_color()}; background: transparent;")
        self._cb.set_enabled_look(online)
        self._refresh_usd()

    def _refresh_usd(self) -> None:
        """Render (or hide) the USD balance sub-line under the name."""
        if self._usd is None:
            self._sub_container.hide()
            return
        online = self._connected
        code_c = _CARD_TEXT3 if online else _CARD_OFF_SECOND
        if not online:
            val_c = _CARD_OFF_SECOND
        elif self._usd < 0:
            val_c = _CARD_LOSS
        elif self._usd > 0:
            val_c = _CARD_GAIN
        else:
            val_c = _CARD_TEXT2
        self._sub_code.setStyleSheet(
            f"color: {code_c}; background: transparent;")
        self._sub_val.setText(self._fmt_amount(self._usd))
        self._sub_val.setStyleSheet(
            f"color: {val_c}; background: transparent;")
        self._sub_container.show()

    @staticmethod
    def _fmt_amount(amount: float) -> str:
        """Grouped integer with a real minus on negatives, e.g. ``-8,917``."""
        s = f"{abs(amount):,.0f}"
        return f"{_MINUS}{s}" if amount < 0 else s

    @staticmethod
    def _parse_neg(raw: str) -> bool:
        try:
            return float(raw) < 0
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _format_balance(raw: str) -> str:
        """Compact base-currency total: ``219k``, ``1.2M``, ``-12k``."""
        if not raw:
            return ""
        try:
            val = float(raw)
        except (ValueError, TypeError):
            return raw
        if abs(val) > 1e100:
            return "-"
        a = abs(val)
        if a >= 1_000_000:
            s = f"{a / 1_000_000:.1f}M"
        elif a >= 1_000:
            s = f"{a / 1_000:.0f}k"
        else:
            s = f"{a:,.0f}"
        return f"{_MINUS}{s}" if val < 0 else s

# ── Group pill ────────────────────────────────────────────────────────────────

class _GroupPill(QPushButton):
    """Single named-group button in the filter strip.

    Left-click = activate group (emits ``activated``).
    Right-click = open a small context menu with Rename / Delete.
    Left-click + drag = reorder pills (invokes host-supplied ``on_drop``,
    mirroring the ``DraggableChip`` pattern in ``order_panel_parts.py``).

    Group membership auto-updates from individual checkbox toggles
    while the pill is active; there is no explicit "Update" action.
    """

    activated        = Signal(str)
    rename_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, name: str, parent=None, *, on_drop=None):
        super().__init__(name, parent)
        self._name = name
        self._active = False
        self._on_drop = on_drop
        self._press_pos = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.clicked.connect(lambda: self.activated.emit(self._name))
        self.setAcceptDrops(on_drop is not None)
        # Expand horizontally with the grid column so the pill scales
        # with the panel width rather than hugging its text.
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _apply_filter_chip_style(self, False)


    @property
    def group_name(self) -> str:
        return self._name

    def set_active(self, on: bool) -> None:
        if self._active == bool(on):
            return
        self._active = bool(on)
        _apply_filter_chip_style(self, self._active)

    def is_active(self) -> bool:
        return self._active

    def _show_menu(self, pos) -> None:
        menu = QMenu(self)
        rename_act = menu.addAction("Rename…")
        menu.addSeparator()
        delete_act = menu.addAction("Delete")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is rename_act:
            self.rename_requested.emit(self._name)
        elif chosen is delete_act:
            self.delete_requested.emit(self._name)

    # ── Drag-to-reorder ──────────────────────────────────────────────────────

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if (self._on_drop is not None
                and self._press_pos is not None
                and (event.buttons() & Qt.MouseButton.LeftButton)
                and (event.pos() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            self._press_pos = None
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_GROUP_MIME, self._name.encode("utf-8"))
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.MoveAction)
            return
        super().mouseMoveEvent(event)

    def dragEnterEvent(self, event):  # noqa: N802
        if (event.mimeData().hasFormat(_GROUP_MIME)
                and event.source() is not self):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802
        if (event.mimeData().hasFormat(_GROUP_MIME)
                and event.source() is not self):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        mime = event.mimeData()
        if not (mime.hasFormat(_GROUP_MIME) and event.source() is not self):
            super().dropEvent(event)
            return
        try:
            src = bytes(mime.data(_GROUP_MIME)).decode("utf-8")
        except UnicodeDecodeError:
            event.ignore()
            return
        event.acceptProposedAction()
        if self._on_drop is not None and src and src != self._name:
            self._on_drop(src, self._name)


# ── Main Panel ────────────────────────────────────────────────────────────────

def _floor_to_5(shares: float) -> int:
    """Round a share count DOWN to the nearest multiple of 5 (e.g. 911→910,
    919→915).  Used for Sweep in so we never buy beyond the positive cash."""
    return int(max(0.0, shares) // 5) * 5


def _ceil_to_5(shares: float) -> int:
    """Round a share count UP to the nearest multiple of 5 (e.g. 911→915,
    919→920).  Used for Sweep out so we sell enough to cover the negative cash."""
    import math
    return int(math.ceil(max(0.0, shares) / 5.0)) * 5


class AccountListPanel(QWidget):
    """Live account list with action buttons – wired to AccountsTableModel."""

    # Emitted when an action button is clicked: (action_name, [account_ids])
    action_requested = Signal(str, list)
    # Emitted whenever the checked/selected set of accounts changes.
    # Payload: frozenset of currently-selected account ID strings.
    selection_changed = Signal(object)   # set[str]
    # Emitted when the user activates a named group (or None when the
    # current selection no longer matches any saved group).  Payload
    # is the group name string or None.
    active_group_changed = Signal(object)
    # Emitted when the user picks a cash-sweep action from an account's
    # right-click menu: (account, direction "in"|"out", qty, symbol,
    # limit_price — the sweep-in buy limit; 0.0 for MKT sweep-out sells).
    sweep_requested = Signal(str, str, int, str, float)

    def __init__(self, accounts_model: AccountsTableModel, parent=None):
        super().__init__(parent)
        self._model = accounts_model
        self._account_rows: dict[str, _AccountRow] = {}
        # Callable(account, symbol) -> (price, held_shares) supplied by the
        # host (FlashTradeView) so the sweep menu can size the order.
        self._sweep_quote_provider = None
        self._unchecked: set[str] = set(
            config_manager.load_unchecked_accounts())
        self._hidden: set[str] = set(config_manager.load_hidden_accounts())
        # Named groups persisted to app_config.json.  List (not dict)
        # to preserve pill order in the filter strip.  Each group dict
        # is ``{name, accounts, selected}`` — ``accounts`` is the full
        # member set, ``selected`` is the subset currently checked
        # while the group is active.  Legacy configs lacking
        # ``selected`` are migrated to ``selected = accounts`` so the
        # prior "every member checked" behavior is preserved.
        self._groups: list[dict] = config_manager.load_account_groups()
        for _g in self._groups:
            if "selected" not in _g:
                _g["selected"] = list(_g.get("accounts", []))
        self._active_group: str | None = None
        self._group_pills: dict[str, _GroupPill] = {}
        # Built-in header pills.  Initialised during _setup_ui; the
        # ``is not None`` guards in _set_active_group cope with the
        # brief window before then.
        self._all_pill: QPushButton | None = None
        self._none_pill: QPushButton | None = None
        # One-shot flag: auto-activate the "Alph" trading group on
        # startup once the first batch of accounts has populated.
        # Not re-applied after the user switches away mid-session.
        self._default_group_applied: bool = False
        self._setup_ui()

        # React to model changes
        self._model.dataChanged.connect(self._on_model_data_changed)
        self._model.connection_state_changed.connect(
            self._refresh_connected_states)
        self._model.rowsInserted.connect(self._on_rows_changed)
        self._model.rowsRemoved.connect(self._on_rows_changed)
        # A deleted connection drops its accounts from ``known_accounts``;
        # without this the offline rows it left behind would never clear
        # (``remove_rows_by_label`` emits nothing when the rows are
        # already gone, which is the usual disconnect-then-delete order).
        self._model.known_accounts_changed.connect(self._on_rows_changed)

        self._sync_rows()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setObjectName("AccountListPanel")
        self.setStyleSheet(f"""
            QWidget#AccountListPanel {{
                background: {BG_PANEL};
                border: 1px solid {BORDER};
                border-radius: 4px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # ── Header: title (left) + "+" button (right) ────────────────────────
        # "+" creates a new user-defined group seeded with every
        # account, all unchecked.  The built-in All / None quick-select
        # pills live in the first two slots of the 2×5 grid below.
        header = QHBoxLayout()
        header.setSpacing(4)

        title = QLabel("Accounts")
        title.setFont(_TITLE_FONT)
        title.setStyleSheet("color: #ffffff; border: none;")
        header.addWidget(title)
        header.addStretch(1)

        self._add_group_btn = QPushButton("+")
        self._add_group_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_group_btn.setToolTip(
            "Create a new trading group seeded with every account "
            "(all unchecked).")
        _apply_filter_chip_style(self._add_group_btn, False)
        self._add_group_btn.clicked.connect(self._on_create_group)
        header.addWidget(self._add_group_btn)
        root.addLayout(header)

        # ── Trading-group chip grid: up to 2 rows × 5 chips ──────────────────
        self._filter_strip = self._build_filter_strip()
        root.addWidget(self._filter_strip)

        # ── Scrollable account rows ───────────────────────────────────────────
        self._rows_container = QWidget()
        # Object-name scope this rule.  A selector-less ``border: none``
        # cascades to every descendant and would strip each _AccountRow's
        # selected/idle border, so the card's frame colour never changes.
        self._rows_container.setObjectName("AccountRowsContainer")
        self._rows_container.setStyleSheet(
            "QWidget#AccountRowsContainer "
            "{ background: transparent; border: none; }")
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        self._rows_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(self._rows_container)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ background: transparent; width: 6px; }}
            QScrollBar::handle:vertical {{
                background: #232A3B; border-radius: 3px; min-height: 20px;
            }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        """)
        root.addWidget(scroll, stretch=1)

        # ── Divider ───────────────────────────────────────────────────────────
        root.addWidget(_h_divider())

        # ── Action button grid ────────────────────────────────────────────────
        ag = QGridLayout()
        ag.setSpacing(4)
        ag.setContentsMargins(0, 4, 0, 0)

        for col, (txt, clr, fg, act) in enumerate([
            ("25%",     CLR_CLOSE,   "#ffffff", "close_25"),
            ("50%",     CLR_CLOSE,   "#ffffff", "close_50"),
            ("Flatten", CLR_FLATTEN, "#ffffff", "flatten"),
        ]):
            b = _make_btn(txt, clr, fg=fg)
            b.clicked.connect(lambda _, a=act: self._emit_action(a))
            if act == "flatten":
                self._flatten_btn = b
            ag.addWidget(b, 0, col)

        # Misclick guard for Flatten (inline two-step, same as the
        # positions view): the first click only ARMS the button
        # ("Flatten" → red "Confirm?"); a second click within the
        # window actually flattens, else it quietly reverts.
        self._flatten_armed = False
        self._flatten_confirm_timer = QTimer(self)
        self._flatten_confirm_timer.setSingleShot(True)
        self._flatten_confirm_timer.setInterval(3000)
        self._flatten_confirm_timer.timeout.connect(self._disarm_flatten)

        for col, (txt, clr, fg, act) in enumerate([
            ("Reverse", CLR_REVERSE, "#ffffff", "reverse"),
            ("75%",     CLR_CLOSE,   "#ffffff", "close_75"),
            ("Cancel",  CLR_CANCEL,  "#E6EBF2", "cancel_all"),
        ]):
            b = _make_btn(txt, clr, fg=fg)
            b.clicked.connect(lambda _, a=act: self._emit_action(a))
            ag.addWidget(b, 1, col)

        aw = QWidget()
        aw.setStyleSheet("border: none;")
        aw.setContentsMargins(0, 0, 0, 0)
        aw.setLayout(ag)
        root.addWidget(aw)

        # ── Divider ───────────────────────────────────────────────────────────
        root.addWidget(_h_divider())

        # ── Footer: Add Account dropdown ──────────────────────────────────────
        self._add_combo = QComboBox()
        self._add_combo.setPlaceholderText("+ Add Account")
        self._add_combo.setStyleSheet(f"""
            QComboBox {{
                background: {BG_ITEM_INACTIVE}; color: {TEXT};
                border: 1px solid {BORDER}; border-radius: 6px;
                padding: 2px 8px; font-size: 11px; min-height: 20px;
            }}
            QComboBox:hover {{ border-color: {TEXT_DIM}; }}
            QComboBox::drop-down {{
                subcontrol-origin: padding; subcontrol-position: center right;
                width: 24px; border: none;
            }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {TEXT_DIM};
                margin-right: 8px;
            }}
            QComboBox QAbstractItemView {{
                background: {BG_PANEL}; color: {TEXT};
                border: 1px solid {BORDER};
                selection-background-color: #1C2740;
                selection-color: {TEXT}; padding: 4px;
            }}
        """)
        self._add_combo.activated.connect(self._on_add_account)
        root.addWidget(self._add_combo)

    # ── Filter strip + group pills ────────────────────────────────────────────

    def _build_filter_strip(self) -> QWidget:
        """Grid container holding the trading-group chips.

        Layout (2 rows × 5 columns):
          * (0, 0) — built-in ``All``  pill
          * (0, 1) — built-in ``None`` pill
          * remaining 8 slots (row 0 cols 2–4, row 1 cols 0–4) — user-
            defined group pills.  User pills drag-to-reorder among
            themselves; the defaults always stay in their reserved
            corner and are not draggable.
        """
        strip = QWidget()
        strip.setObjectName("filterStrip")
        strip.setStyleSheet(
            "QWidget#filterStrip { background: transparent; border: none; }")

        grid = QGridLayout(strip)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        # Equal stretch on every column so the pills divide the
        # available width evenly and grow / shrink with the panel
        # instead of hugging their content size.
        for c in range(_GROUP_COLS):
            grid.setColumnStretch(c, 1)
        self._pills_layout = grid
        self._pills_container = strip

        # Built-in All / None pills occupy the first two slots of row 0.
        self._all_pill = QPushButton("All")
        self._all_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self._all_pill.setToolTip("Select every account.")
        self._all_pill.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _apply_filter_chip_style(self._all_pill, False)
        self._all_pill.clicked.connect(
            lambda: self._select_default_group("All"))
        grid.addWidget(self._all_pill, 0, 0)

        self._none_pill = QPushButton("None")
        self._none_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self._none_pill.setToolTip("Deselect every account.")
        self._none_pill.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _apply_filter_chip_style(self._none_pill, False)
        self._none_pill.clicked.connect(
            lambda: self._select_default_group("None"))
        grid.addWidget(self._none_pill, 0, 1)

        self._rebuild_pills()
        return strip

    def _rebuild_pills(self) -> None:
        """Clear and repopulate the user-group slots in the pill grid.
        The first two slots (row 0 cols 0 and 1) host the built-in
        All / None pills and are never touched by this method.  Only
        the first ``_GROUP_MAX`` user groups are rendered — creation is
        capped at that count so overflow is never visible here."""
        # Detach & delete existing user pills.  Defaults are untouched.
        for pill in self._group_pills.values():
            self._pills_layout.removeWidget(pill)
            pill.deleteLater()
        self._group_pills.clear()

        for i, group in enumerate(self._groups[:_GROUP_MAX]):
            slot = i + _GROUP_DEFAULT_SLOTS
            row = slot // _GROUP_COLS
            col = slot % _GROUP_COLS
            name = group["name"]
            pill = _GroupPill(
                name,
                self._pills_container,
                on_drop=self._on_group_drop,
            )
            pill.activated.connect(self._select_group)
            pill.rename_requested.connect(self._rename_group)
            pill.delete_requested.connect(self._delete_group)
            pill.set_active(name == self._active_group)
            self._pills_layout.addWidget(pill, row, col)
            self._group_pills[name] = pill

    def _on_group_drop(self, src_name: str, dst_name: str) -> None:
        """Pill ``src_name`` was dropped onto ``dst_name`` — the dragged
        pill takes the drop target's position, so a drag works in every
        direction (the pill grid is row-major, so left/right/up/down are
        all just list indices): dragging forward lands *after* the
        target, dragging backward lands *before* it.  Persist + rebuild.
        """
        names = [g["name"] for g in self._groups]
        if src_name not in names or dst_name not in names:
            return
        src_idx = names.index(src_name)
        # The target's index BEFORE the pop: inserting there after
        # removing src puts the pill exactly where the target was —
        # after it when dragging forward (everything shifted left by
        # one), before it when dragging backward (nothing shifted).
        dst_idx = names.index(dst_name)
        moved = self._groups.pop(src_idx)
        self._groups.insert(dst_idx, moved)
        config_manager.save_account_groups(self._groups)
        self._rebuild_pills()
        log.info("AccountList: moved group %r to %r's position",
                 src_name, dst_name)

    def _set_active_group(self, name: str | None) -> None:
        """Update `_active_group`, refresh pill visual state, persist
        the new value so the next launch can restore it, and emit the
        signal.  Covers both user-defined pills and the built-in All /
        None pills."""
        if name == self._active_group:
            return
        self._active_group = name
        for pill_name, pill in self._group_pills.items():
            pill.set_active(pill_name == name)
        if self._all_pill is not None:
            _apply_filter_chip_style(self._all_pill, name == "All")
        if self._none_pill is not None:
            _apply_filter_chip_style(self._none_pill, name == "None")
        config_manager.save_last_account_group(name)
        self.active_group_changed.emit(name)

    def _find_group(self, name: str) -> dict | None:
        for g in self._groups:
            if g["name"] == name:
                return g
        return None

    # ── Group CRUD ────────────────────────────────────────────────────────────

    def _on_create_group(self) -> None:
        if len(self._groups) >= _GROUP_MAX:
            QMessageBox.information(
                self, "Group limit reached",
                f"The trading-group grid holds at most {_GROUP_MAX} "
                f"user-defined groups (the first two slots are reserved "
                "for the built-in All and None pills).  "
                "Delete an existing group before creating a new one.")
            return
        # Seed the new group with every account currently known to the
        # model.  The user then checks the ones they actually want
        # selected (or clicks × to drop an account from the group
        # entirely) — see ``_on_row_toggled`` and the × button for the
        # two kinds of mutation.
        all_accounts = sorted(
            rd[COL_ACCOUNT] for rd in self._model._table_data
            if rd[COL_ACCOUNT]
        )
        name = self._ask_group_name(
            "New account group",
            f"Name this group ({len(all_accounts)} account(s)):",
            "")
        if not name:
            return
        if name in _DEFAULT_GROUPS:
            QMessageBox.warning(
                self, "Reserved name",
                f"‘{name}’ is a built-in quick-select group — "
                "pick a different name.")
            return
        if self._find_group(name) is not None:
            QMessageBox.warning(self, "Name in use",
                                f"A group named ‘{name}’ already exists.")
            return
        # New group: every account is a member, none are selected.
        # The user then checks the ones they want (updates ``selected``)
        # and optionally clicks × to trim membership (``accounts``).
        self._groups.append(
            {"name": name, "accounts": all_accounts, "selected": []})
        config_manager.save_account_groups(self._groups)
        self._rebuild_pills()
        self._set_active_group(name)
        self._sync_rows()
        # _sync_rows only updates alias/balance on rows that already
        # exist — it doesn't touch their check state.  Apply the fresh
        # (all-unchecked) state explicitly so every visible row flips
        # in a single pass.
        for acct, r in self._account_rows.items():
            r.set_checked(self._is_account_checked(acct))
        self._emit_selection_changed()
        log.info("AccountList: created group %r seeded with %d account(s) "
                 "(all unchecked)", name, len(all_accounts))

    def _ask_group_name(self, title: str, label: str,
                        initial: str) -> str:
        """Prompt the user for a group name with a text-input dialog
        that is 80% of the Accounts panel width and centered over it.
        Returns the stripped name, or ``""`` on cancel / empty."""
        dlg = QInputDialog(self)
        dlg.setInputMode(QInputDialog.InputMode.TextInput)
        dlg.setWindowTitle(title)
        dlg.setLabelText(label)
        dlg.setTextValue(initial)
        dlg.setTextEchoMode(QLineEdit.EchoMode.Normal)

        width = max(220, int(self.width() * 0.8))

        # Defer the resize+center to the next event-loop tick so the
        # dialog's frame geometry (title bar, padding) is accurate.
        # Doing it before exec() puts the wrong number into move() on
        # macOS — the title bar offsets the visible center.
        def _center() -> None:
            dlg.resize(width, dlg.height())
            panel_center = self.mapToGlobal(self.rect().center())
            frame = dlg.frameGeometry()
            dlg.move(panel_center.x() - frame.width() // 2,
                     panel_center.y() - frame.height() // 2)
        QTimer.singleShot(0, _center)

        if dlg.exec() != QInputDialog.DialogCode.Accepted:
            return ""
        return dlg.textValue().strip()

    def _rename_group(self, old_name: str) -> None:
        if self._find_group(old_name) is None:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename group", "New name:",
            QLineEdit.EchoMode.Normal, old_name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_name:
            return
        if new_name in _DEFAULT_GROUPS:
            QMessageBox.warning(
                self, "Reserved name",
                f"‘{new_name}’ is a built-in quick-select group — "
                "pick a different name.")
            return
        if self._find_group(new_name) is not None:
            QMessageBox.warning(self, "Name in use",
                                f"A group named ‘{new_name}’ already exists.")
            return
        for g in self._groups:
            if g["name"] == old_name:
                g["name"] = new_name
                break
        if self._active_group == old_name:
            self._active_group = new_name
        config_manager.save_account_groups(self._groups)
        self._rebuild_pills()
        log.info("AccountList: renamed group %r -> %r", old_name, new_name)

    def _delete_group(self, name: str) -> None:
        if self._find_group(name) is None:
            return
        ans = QMessageBox.question(
            self, "Delete group",
            f"Delete the ‘{name}’ group? Account selections "
            "aren't changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if ans != QMessageBox.StandardButton.Yes:
            return
        was_active = self._active_group == name
        self._groups = [g for g in self._groups if g["name"] != name]
        config_manager.save_account_groups(self._groups)
        if was_active:
            # Selection itself is preserved; only the active-group
            # indicator clears.  Re-sync rows so the list falls back to
            # the all-accounts view (it was filtered to this group).
            self._set_active_group(None)
            self._sync_rows()
        self._rebuild_pills()
        log.info("AccountList: deleted group %r", name)

    def get_checked_accounts(self) -> set[str]:
        """Internal helper — a set of currently checked account IDs.
        Kept separate from the legacy ``get_selected_accounts`` which
        returns a list."""
        return {a for a, r in self._account_rows.items() if r.is_checked}

    # ── Model synchronisation ─────────────────────────────────────────────────

    def _sync_rows(self) -> None:
        """Rebuild / update account rows from the model.

        Visibility rules:
          * always hide accounts in ``self._hidden`` (global hide list)
          * when a trading group is active, show only that group's
            members — the list IS the group.  Scoped-delete via the ×
            button drops an account from the active group; the row
            disappears here because the account is no longer a member.
          * when no group is active, show every non-hidden account.
        """
        model_accounts: dict[str, dict] = {}
        for row_data in self._model._table_data:
            acct = row_data[COL_ACCOUNT]
            if acct:
                model_accounts[acct] = {
                    "alias": row_data[COL_ALIAS],
                    "balance": row_data[COL_NET_LIQ],
                    "type": row_data[COL_TYPE],
                }

        # Compute the set of accounts that should have a visible row.
        # Built-in All / None groups and the no-group view honor the
        # ``_hidden`` set (× hides the account from the default views).
        # User-defined groups ignore ``_hidden`` — an account that's
        # been hidden from All is still a member of any group that
        # includes it, and becomes visible again when the user
        # activates that group.
        # An account whose connection is down has no model row, but it has
        # not ceased to exist.  Every view here keeps it on screen with a
        # row of its own — dimmed, ⚠-prefixed, figures blanked — because a
        # list that silently shrinks to its online members hides exactly
        # who an order fan-out is about to skip.  ``known_accounts``
        # remembers them across a disconnect; deleting the connection
        # (``forget_label``) is what removes them for good.
        offline_members: set[str] = set()
        if self._active_group is not None \
                and self._active_group not in _DEFAULT_GROUPS:
            g = self._find_group(self._active_group)
            group_members = set(g.get("accounts", [])) if g else set()
            visible = {a for a in model_accounts if a in group_members}
            # Scoped to the group's own membership list, which survives a
            # disconnect on its own — no need to consult known_accounts.
            offline_members = group_members - set(model_accounts)
        else:
            # All / None / no-group: honour the ``_hidden`` set (× hides
            # an account from the default views) on both halves, so a
            # hidden account does not reappear the moment it goes offline.
            visible = {a for a in model_accounts if a not in self._hidden}
            offline_members = {
                a for a in self._model.known_accounts()
                if a not in model_accounts and a not in self._hidden
            }

        # Add new / update existing.
        for acct in visible:
            info = model_accounts[acct]
            if acct not in self._account_rows:
                self._add_account_row(
                    acct, info["alias"], info["balance"], info["type"])
            r = self._account_rows[acct]
            r.set_name(info["alias"] if info["alias"] else acct)
            r.set_balance(info["balance"])
            r.set_cash(self._model.get_cash_by_currency(acct))
            r.set_connected(self._is_account_connected(acct))

        for acct in offline_members:
            if acct not in self._account_rows:
                self._add_account_row(
                    acct, self._model._aliases.get(acct, ""), "–", "")
            r = self._account_rows[acct]
            r.set_name(self._model._aliases.get(acct, "") or acct)
            # Blank the money too, not just dim it.  A row that keeps its
            # last balance is showing a number nothing is refreshing —
            # indistinguishable from a live one, and the account may have
            # been traded elsewhere since the connection dropped.
            r.set_balance("–")
            r.set_cash({})
            r.set_connected(False)

        # Remove rows whose account should no longer be visible (either
        # missing from the model entirely or filtered out by the active
        # group).
        for acct in [a for a in self._account_rows
                     if a not in visible and a not in offline_members]:
            self._remove_account_row(acct)

        self._refresh_add_combo(model_accounts)
        self._reorder_rows()
        self._maybe_apply_default_group()

    def _maybe_apply_default_group(self) -> None:
        """One-shot: restore the last-active group on startup (or the
        built-in ``None`` pill on first launch) once the first batch
        of accounts populates the panel.  Subsequent sync calls are
        no-ops so the user is free to switch groups mid-session."""
        if self._default_group_applied:
            return
        if not self._account_rows:
            # Wait for the first non-empty population — accounts may
            # arrive a tick or two after the panel is constructed.
            return
        self._default_group_applied = True

        last = config_manager.load_last_account_group()
        if last in _DEFAULT_GROUPS:
            log.info("AccountList: restoring last-active default group %r",
                     last)
            self._select_default_group(last)
            return
        if last and self._find_group(last) is not None:
            log.info("AccountList: restoring last-active user group %r", last)
            self._select_group(last)
            return
        # First launch (or the saved name vanished): default to "None"
        # — every account visible, none selected.
        log.info("AccountList: first launch, applying default 'None' group")
        self._select_default_group("None")

    def _add_account_row(self, account: str, alias: str,
                         balance: str, acct_type: str) -> None:
        # Check state is resolved through _is_account_checked so each
        # context uses the right source of truth: user group's
        # ``selected`` list, built-in All / None semantics, or the
        # global ``_unchecked`` set when no group is active.
        checked = self._is_account_checked(account)
        row = _AccountRow(account, alias, balance, acct_type, checked=checked)
        row.toggled.connect(self._on_row_toggled)
        row.removed.connect(self._on_row_removed)
        row.context_menu_requested.connect(self._show_sweep_menu)
        # Apply the worker's current connection state so freshly added
        # rows for an offline label render dimmed from the start.
        row.set_connected(self._is_account_connected(account))
        idx = self._rows_layout.count() - 1   # insert before the stretch
        self._rows_layout.insertWidget(idx, row)
        self._account_rows[account] = row
        log.debug("AccountList: added row for %s (checked=%s)", account, checked)

    def _is_account_connected(self, account: str) -> bool:
        """Resolve the live TWS connection state for ``account`` via the
        shared model.  An account with no connection label at all was
        never detected this session (its worker never connected) — that
        is offline, not "state unknown", so it must NOT inherit
        ``is_label_connected``'s default-True for fresh labels."""
        label = self._model.connection_for_account(account)
        if label is None:
            return False
        return self._model.is_label_connected(label)

    def _refresh_connected_states(self) -> None:
        """Push the current connection state of each visible row's worker
        back onto the row widget.  Called after ``dataChanged`` so flips
        from ``set_connection_status`` reach the UI in real time."""
        for account, row in self._account_rows.items():
            row.set_connected(self._is_account_connected(account))

    def _remove_account_row(self, account: str) -> None:
        row = self._account_rows.pop(account, None)
        if row:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
            log.debug("AccountList: removed row for %s", account)

    def _refresh_add_combo(self, current_accounts: dict | None = None) -> None:
        """Populate the Add Account dropdown with accounts NOT yet visible."""
        if current_accounts is None:
            current_accounts = {
                r[COL_ACCOUNT]: True
                for r in self._model._table_data if r[COL_ACCOUNT]
            }
        self._add_combo.blockSignals(True)
        self._add_combo.clear()
        for acct in current_accounts:
            if acct not in self._account_rows:
                alias = self._model._aliases.get(acct, "")
                label = f"{alias} ({acct})" if alias else acct
                self._add_combo.addItem(label, acct)
        self._add_combo.setCurrentIndex(-1)
        self._add_combo.blockSignals(False)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_model_data_changed(self, topLeft: QModelIndex,
                               bottomRight: QModelIndex, roles=None) -> None:
        """Column-aware: the 250 ms summary batches emit one dataChanged
        per (account, tag) — ~hundreds per flush.  Only the columns this
        panel renders trigger row updates (the row widgets additionally
        skip unchanged values), and rows re-sort only when a display
        name actually changed — the sort key is name-based, so balance
        ticks never alter the order.  Connection flips arrive via the
        model's dedicated ``connection_state_changed`` signal."""
        c0, c1 = topLeft.column(), bottomRight.column()
        name_changed = False
        for row_idx in range(topLeft.row(), bottomRight.row() + 1):
            if row_idx >= len(self._model._table_data):
                continue
            rd = self._model._table_data[row_idx]
            r = self._account_rows.get(rd[COL_ACCOUNT])
            if not r:
                continue
            if c0 <= COL_NET_LIQ <= c1 or c0 <= COL_TOTAL_CASH <= c1:
                r.set_balance(rd[COL_NET_LIQ])
                r.set_cash(self._model.get_cash_by_currency(rd[COL_ACCOUNT]))
            if c0 <= COL_ALIAS <= c1:
                if r.set_name(rd[COL_ALIAS] if rd[COL_ALIAS]
                              else rd[COL_ACCOUNT]):
                    name_changed = True
        if name_changed:
            self._reorder_rows()

    def _on_rows_changed(self, *args) -> None:
        self._sync_rows()
        self._refresh_connected_states()
        self._emit_selection_changed()

    def _on_row_toggled(self, account: str, checked: bool) -> None:
        log.debug("AccountList: %s toggled → %s (active=%s)",
                  account, checked, self._active_group)
        if (self._active_group is not None
                and self._active_group not in _DEFAULT_GROUPS):
            # Toggling within a user group updates that group's
            # ``selected`` subset and persists — membership
            # (``accounts``) is untouched, so the row stays visible.
            g = self._find_group(self._active_group)
            if g is not None:
                selected = list(g.get("selected", g.get("accounts", [])))
                if checked:
                    if account not in selected:
                        selected.append(account)
                else:
                    selected = [a for a in selected if a != account]
                g["selected"] = selected
                config_manager.save_account_groups(self._groups)
        else:
            # No user group active (or one of All / None) — fall back
            # to the global ``_unchecked`` tracking which survives
            # restarts via ``_persist_selection``.
            if checked:
                self._unchecked.discard(account)
            else:
                self._unchecked.add(account)
            self._persist_selection()
        self._emit_selection_changed()
        self._reorder_rows()

    def _on_row_removed(self, account: str) -> None:
        # When a user-defined group is active, the × button scopes to
        # that group: the account is taken out of the active group
        # only, every other group is left alone.  Virtual All / None
        # groups and the no-group view fall through to the "hide from
        # the default view" path: ``_hidden`` keeps the row out of
        # those views, but user groups still show the account when
        # activated (see ``_sync_rows``), so other groups are not
        # affected.
        if (self._active_group is not None
                and self._active_group not in _DEFAULT_GROUPS):
            self._remove_account_from_active_group(account)
            return

        self._remove_account_row(account)
        self._hidden.add(account)
        self._unchecked.discard(account)
        self._persist_selection()
        self._persist_hidden()
        self._refresh_add_combo()
        self._emit_selection_changed()
        log.info("AccountList: %s hidden from default views (groups untouched)",
                 account)

    def _remove_account_from_active_group(self, account: str) -> None:
        """Scoped delete: drop *account* from the currently-active group
        only, leaving every other saved group untouched.  The row is
        removed from the list because the active-group view shows only
        group members.  An emptied group is KEPT — its pill stays and
        the view just shows no rows.  Deleting a group is an explicit
        act only: right-click the pill → Delete."""
        name = self._active_group
        g = self._find_group(name) if name else None
        if g is None:
            return
        if account not in g["accounts"]:
            return
        g["accounts"] = [a for a in g["accounts"] if a != account]
        # Keep ``selected`` consistent — a non-member can't be selected.
        if "selected" in g:
            g["selected"] = [a for a in g["selected"] if a != account]

        config_manager.save_account_groups(self._groups)

        # Re-sync rows: the deleted account is no longer a member so
        # its row is dropped (an emptied group shows an empty list).
        self._sync_rows()
        self._emit_selection_changed()
        log.info("AccountList: removed %s from group %r (scoped delete)",
                 account, name)

    def _on_add_account(self, index: int) -> None:
        acct = self._add_combo.itemData(index)
        if not acct:
            return
        self._hidden.discard(acct)
        self._persist_hidden()

        # When a user group is active, adding an account re-joins it
        # as a checked member of that group.  Persisted alongside the
        # other group mutations so the change survives a restart.
        if (self._active_group is not None
                and self._active_group not in _DEFAULT_GROUPS):
            g = self._find_group(self._active_group)
            if g is not None:
                accounts = list(g.get("accounts", []))
                if acct not in accounts:
                    accounts.append(acct)
                    g["accounts"] = accounts
                selected = list(g.get("selected", []))
                if acct not in selected:
                    selected.append(acct)
                    g["selected"] = selected
                config_manager.save_account_groups(self._groups)
        else:
            # No user group active — make sure the freshly-visible row
            # starts checked by removing it from the global _unchecked.
            if acct in self._unchecked:
                self._unchecked.discard(acct)
                self._persist_selection()

        for rd in self._model._table_data:
            if rd[COL_ACCOUNT] == acct:
                self._add_account_row(
                    acct, rd[COL_ALIAS], rd[COL_NET_LIQ], rd[COL_TYPE])
                break
        self._refresh_add_combo()
        self._emit_selection_changed()
        self._reorder_rows()
        log.info("AccountList: user re-added %s", acct)

    def _all_known_account_ids(self) -> set[str]:
        """Every account this session knows about, ONLINE OR NOT.

        ``_model._table_data`` holds only accounts whose connection is
        currently up — a disconnect removes the rows.  Any decision about
        the full account universe (what "None" unchecks, what "all
        accounts" means) has to span the offline ones too, or it silently
        skips exactly the rows the panel is still showing.
        """
        online = {rd[COL_ACCOUNT] for rd in self._model._table_data
                  if rd[COL_ACCOUNT]}
        return online | set(self._model.known_accounts())

    def _is_account_checked(self, account: str) -> bool:
        """Resolve an account's checked state based on the active
        group:

          * ``"All"``  → every account is checked.
          * ``"None"`` → every account is unchecked.
          * user group → account is checked iff it's in ``selected``.
          * no active group → check state driven by the global
            ``_unchecked`` set.
        """
        if self._active_group == "All":
            return True
        if self._active_group == "None":
            return False
        if self._active_group is not None:
            g = self._find_group(self._active_group)
            if g is not None:
                return account in g.get("selected", g.get("accounts", []))
        return account not in self._unchecked

    def get_selected_count(self) -> int:
        """Return the number of currently checked (selected) account rows."""
        return sum(1 for r in self._account_rows.values() if r.is_checked)

    def get_visible_accounts(self) -> set[str]:
        """The accounts currently rendered as rows — the active user
        group's members (online or not) when a group is active, every
        non-hidden account otherwise.  This is the panel's DISPLAY
        scope, independent of check state: the Book Trader ladder uses
        it to decide which accounts' resting orders it may badge and
        act on (an order outside the active group must be neither shown
        nor cancelled by a ladder click)."""
        return set(self._account_rows)

    def get_all_accounts(self) -> set[str]:
        """Every account known to the accounts model — regardless of the
        active group, the hidden list, or check state.  The status bar
        compares the checked set against THIS to decide whether the
        selection is a real subset: while a group is active the visible
        rows are only that group's members, so comparing against
        ``_account_rows`` would make any fully-checked group look like
        "everything selected"."""
        return self._all_known_account_ids()

    def _emit_selection_changed(self) -> None:
        """Broadcast the current selected-account set."""
        self.selection_changed.emit(self.get_selected_accounts())

    # ── Cash-sweep context menu ────────────────────────────────────────────────

    def set_sweep_quote_provider(self, provider) -> None:
        """Register ``provider(account, symbol) -> (last, ask, held_shares)``
        used to price + size the sweep order shown in the right-click menu."""
        self._sweep_quote_provider = provider

    def compute_sweeps(self, account: str) -> list[dict]:
        """One sweep entry per configured sweep symbol (max 4) for the
        right-click menu.  With nothing configured, a single disabled
        pointer-to-Settings entry."""
        from src.core.cash_management import cash_prefs

        symbols = cash_prefs.sweep_symbols
        if not symbols:
            return [{"enabled": False, "symbol": "", "direction": None,
                     "qty": 0, "limit_price": 0.0,
                     "label": "Set a sweep symbol in Settings → "
                              "Cash Management"}]
        return [self.compute_sweep(account, s) for s in symbols]

    def compute_sweep(self, account: str, symbol: str | None = None) -> dict:
        """Pure computation for one sweep-menu entry (kept separate so
        it's testable).  *symbol* defaults to the first configured sweep
        symbol.

        Returns ``{enabled, label, direction, qty, symbol, limit_price}``:

        * **Sweep in** when the account's USD balance is positive — buy
          ``floor(USD / limit)`` shares of the sweep symbol (rounded down to a
          multiple of 5 so we never exceed the cash).  The buy goes out as a
          **limit order** so IB's cash check uses the exact order value — a
          MKT buy carries a ~5% price cushion that rejects full-balance
          sweeps in no-margin (TFSA/RSP) accounts.  Sweep symbols are stable
          money-market ETFs, so the limit is the live ask when we have one,
          else last + 2¢ — marketable either way.
        * **Sweep out** when the balance is negative — sell
          ``ceil(|USD| / last)`` shares (rounded up to a multiple of 5 so we
          cover the whole deficit), capped at the shares actually held.
          Sells stay MKT (``limit_price`` 0) — there's no cash check to trip.

        ``enabled`` is False (with an explanatory ``label``) when there's no
        sweep symbol configured, no live price, a flat balance, or the quantity
        rounds to zero.  A sweep-out with no shares held additionally sets
        ``hidden`` True — the menu drops the entry instead of showing it
        disabled."""
        from src.core.cash_management import cash_prefs

        if symbol is None:
            symbol = cash_prefs.sweep_symbol
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return {"enabled": False, "symbol": "", "direction": None, "qty": 0,
                    "limit_price": 0.0,
                    "label": "Set a sweep symbol in Settings → Cash Management"}

        usd = float(self._model.get_cash_by_currency(account).get("USD", 0.0) or 0.0)
        last, ask, held = 0.0, 0.0, 0.0
        if callable(self._sweep_quote_provider):
            try:
                last, ask, held = self._sweep_quote_provider(account, symbol)
            except Exception:  # noqa: BLE001 — never let a bad provider break the menu
                last, ask, held = 0.0, 0.0, 0.0

        if ask > 0:
            limit_price = round(float(ask), 2)
        elif last > 0:
            limit_price = round(float(last) + 0.02, 2)
        else:
            limit_price = 0.0

        direction = "in" if usd > 0 else "out" if usd < 0 else None
        # Sizing anchor: the buy is capped at limit_price so size with it;
        # the sell is MKT so size with last (ask as a fallback).
        size_price = (limit_price if direction == "in"
                      else (last if last > 0 else ask))
        if direction is None or size_price <= 0:
            reason = ("USD balance is flat" if direction is None
                      else f"no live price for {symbol}")
            return {"enabled": False, "symbol": symbol, "direction": direction,
                    "qty": 0, "limit_price": 0.0,
                    "label": f"Sweep unavailable — {reason}"}

        if direction == "in":
            qty = _floor_to_5(usd / size_price)
        else:
            limit_price = 0.0
            # Sweep out only makes sense if the account actually holds the
            # sweep symbol — nothing to sell otherwise.  ``hidden`` keeps
            # the entry out of the menu entirely (not just disabled).
            if held <= 0:
                return {"enabled": False, "hidden": True, "symbol": symbol,
                        "direction": "out", "qty": 0, "limit_price": 0.0,
                        "label": f"No {symbol} shares to sweep out"}
            needed = _ceil_to_5(abs(usd) / size_price)
            # Cover the whole deficit if we hold enough (rounded up to a
            # multiple of 5); otherwise sell the entire position.
            qty = needed if held >= needed else int(held)

        if qty <= 0:
            return {"enabled": False, "symbol": symbol, "direction": direction,
                    "qty": 0, "limit_price": 0.0,
                    "label": f"Sweep {direction} — quantity rounds to 0"}

        verb = "Sweep in" if direction == "in" else "Sweep out"
        return {"enabled": True, "symbol": symbol, "direction": direction,
                "qty": int(qty), "limit_price": limit_price,
                "label": f"{verb} {qty:,} {symbol}"}

    def _show_sweep_menu(self, account: str, global_pos) -> None:
        """Build + show the account's cash-sweep right-click menu — one
        action per configured sweep symbol (max 4).  Entries flagged
        ``hidden`` (e.g. sweep-out with no shares held) are omitted;
        with nothing left to show, no menu opens at all."""
        menu = _SweepMenu(self)
        menu.right_pressed_outside.connect(self._reopen_sweep_menu_at)
        actions: list[tuple[object, dict]] = []
        for info in self.compute_sweeps(account):
            if info.get("hidden"):
                continue
            act = menu.addAction(info["label"])
            act.setEnabled(info["enabled"])
            actions.append((act, info))
        if not actions:
            return
        chosen = menu.exec(global_pos)
        for act, info in actions:
            if chosen is act and info["enabled"]:
                self.sweep_requested.emit(
                    account, info["direction"], info["qty"], info["symbol"],
                    float(info.get("limit_price", 0.0)))
                break

    def _reopen_sweep_menu_at(self, global_pos) -> None:
        """A right-click closed the open sweep menu — if it landed on an
        account row, open that row's menu without a second click.  Deferred
        with a 0-ms timer so the closing menu's ``exec`` unwinds first."""
        for acct, row in self._account_rows.items():
            if row.rect().contains(row.mapFromGlobal(global_pos)):
                QTimer.singleShot(
                    0, lambda a=acct, p=global_pos: self._show_sweep_menu(a, p))
                return

    def active_group(self) -> str | None:
        """Currently-active group name, or None when the selection
        doesn't match any saved group."""
        return self._active_group

    def active_filter_label(self) -> str | None:
        """Top-bar chip label for the current selection state.

        Priority (most specific first):
          * active group name
          * ``"All"`` when every visible account is checked
          * ``"None"`` when nothing is checked
          * ``None`` for a hand-picked subset (the chip hides)
        """
        if self._active_group is not None:
            return self._active_group
        visible = set(self._account_rows.keys())
        if not visible:
            return None
        checked = self.get_checked_accounts()
        if checked == visible:
            return "All"
        if not checked:
            return "None"
        return None

    def _select_group(self, name: str) -> None:
        """Activate a saved group — the account list is filtered to
        that group's members (see ``_sync_rows``), and each row's
        checked state is restored from the group's ``selected`` list.
        Membership (``accounts``) and selection (``selected``) are
        both persisted per-group, so switching groups correctly
        restores per-group state.
        """
        g = self._find_group(name)
        if g is None:
            return
        # Update the active-group indicator before re-syncing rows so
        # _sync_rows filters to this group's members.
        self._set_active_group(name)
        self._sync_rows()
        # Push the group's saved ``selected`` subset onto the visible
        # rows so the user sees its per-group check state.
        for acct, r in self._account_rows.items():
            r.set_checked(self._is_account_checked(acct))
        self._emit_selection_changed()
        self._reorder_rows()
        log.info("AccountList: activated group %r (%d visible / %d selected)",
                 name, len(self._account_rows),
                 len(g.get("selected", g.get("accounts", []))))

    def _select_default_group(self, name: str) -> None:
        """Activate one of the built-in All / None pills in the header.

        * ``"All"``  — show every model account and check all of them
          (empty ``_unchecked``).
        * ``"None"`` — show every model account and uncheck all of them
          (``_unchecked`` = every account).

        These are virtual groups: nothing is added to ``self._groups``
        and they don't count against the user's 2×5 grid cap.  × on a
        row while one of these is active falls through to the classic
        global-hide path in ``_on_row_removed``.
        """
        if name not in _DEFAULT_GROUPS:
            return
        # Spans offline accounts too.  Built from _table_data alone this
        # missed every account whose connection was down, so "None" left
        # them OUT of _unchecked — and the explicit re-check loop below,
        # which walks _account_rows (offline rows included), then read
        # "not in _unchecked" as "checked" and ticked every offline row
        # the user had just asked to deselect.
        all_accounts = self._all_known_account_ids()
        if name == "All":
            self._unchecked = set()
        else:  # "None"
            self._unchecked = set(all_accounts)
        self._persist_selection()
        self._set_active_group(name)
        self._sync_rows()
        # _sync_rows only touches alias/balance on existing rows — push
        # the new checked state explicitly so every row flips.
        for acct, r in self._account_rows.items():
            r.set_checked(acct not in self._unchecked)
        self._emit_selection_changed()
        self._reorder_rows()
        log.info("AccountList: activated default group %r (%d account(s))",
                 name, len(all_accounts))

    # ── Row ordering ──────────────────────────────────────────────────────────

    # Priority buckets for the first-3-char prefix.  Lower integer →
    # higher priority → floats further up the list.  ``mgn`` is rank 0
    # (top), ``tfs`` rank 1, ``rsp`` and ``fhs`` share rank 2, and every
    # other prefix falls through to rank 3 where the usual alphabetical
    # tiebreak takes over.
    _PREFIX_PRIORITY: dict[str, int] = {
        "mgn": 0,
        "tfs": 1,
        "rsp": 2,
        "fhs": 2,
    }

    def _name_sort_key(self, account: str) -> tuple:
        """Sort key: priority-prefix buckets first (MGN > TFS > RSP/FHS
        > everything else), then alphabetical by first 3 chars of the
        display name, then by last 2 chars.  Display name = alias when
        set, else account id.  Comparison is case-insensitive so
        aliases like ``B-mgn0`` and ``b-MGN1`` land together.  Account
        id is the final tiebreaker to keep the sort deterministic when
        two accounts share both prefix and suffix."""
        alias = self._model._aliases.get(account, "") or ""
        name = alias if alias else account
        lower = name.lower()
        first3 = lower[:3]
        last2 = lower[-2:]
        priority = self._PREFIX_PRIORITY.get(first3, 3)
        return (priority, first3, last2, account)

    def _reorder_rows(self) -> None:
        """Re-arrange the account rows by display-name prefix priority
        (MGN > TFS > RSP/FHS > rest), then alphabetical.  Keeps the
        trailing ``addStretch`` entry at the end of the layout."""
        if not self._account_rows:
            return
        ordered = sorted(
            self._account_rows.items(),
            key=lambda kv: self._name_sort_key(kv[0]),
        )
        # Detach every row (without deleting) then re-insert in new order
        # just before the trailing addStretch slot.
        for _acct, row in ordered:
            self._rows_layout.removeWidget(row)
        # After removal the only child is the stretch (count() == 1).
        for i, (_acct, row) in enumerate(ordered):
            self._rows_layout.insertWidget(i, row)

    def _emit_action(self, action: str) -> None:
        selected = [a for a, r in self._account_rows.items() if r.is_checked]
        if action == "flatten":
            if not self._flatten_armed:
                self._arm_flatten()
                return
            self._disarm_flatten()
        elif action == "reverse":
            if not self._confirm_reverse(selected):
                return
        log.info("AccountList: action '%s' on %d account(s): %s",
                 action, len(selected), selected)
        self.action_requested.emit(action, selected)

    def _arm_flatten(self) -> None:
        self._flatten_armed = True
        self._flatten_btn.setText("Confirm?")
        self._flatten_btn.setStyleSheet(_btn_qss("#F85149"))
        self._flatten_confirm_timer.start()

    def _disarm_flatten(self) -> None:
        self._flatten_confirm_timer.stop()
        if self._flatten_armed:
            self._flatten_armed = False
            self._flatten_btn.setText("Flatten")
            self._flatten_btn.setStyleSheet(
                _btn_qss(CLR_FLATTEN))

    def _confirm_reverse(self, accounts: list[str]) -> bool:
        """Show a styled confirmation dialog before reversing.

        Returns True if the user confirmed, False if they cancelled.
        """
        if not accounts:
            return True   # nothing to reverse; let the normal empty-accounts path handle it

        acct_list = "\n".join(f"  • {a}" for a in accounts)

        dlg = QMessageBox(self)
        dlg.setWindowTitle("Confirm Reverse")
        dlg.setIcon(QMessageBox.Icon.Warning)
        dlg.setText("<b>Reverse all positions?</b>")
        dlg.setInformativeText(
            f"This will cancel all open orders and flip every open position "
            f"(long → short, short → long) for the following account(s):\n\n"
            f"{acct_list}\n\n"
            f"This action cannot be undone."
        )
        dlg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        dlg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dlg.button(QMessageBox.StandardButton.Yes).setText("Reverse")

        dlg.setStyleSheet("""
            QMessageBox {
                background-color: #151922;
                color: #E6EBF2;
            }
            QMessageBox QLabel {
                color: #E6EBF2;
                font-size: 12px;
            }
            QPushButton {
                min-width: 80px;
                min-height: 28px;
                border-radius: 4px;
                font-weight: bold;
                font-size: 11px;
                padding: 4px 12px;
                border: none;
            }
            QPushButton[text="Reverse"] {
                background: #A371F7;
                color: #ffffff;
            }
            QPushButton[text="Reverse"]:hover  { background: #B58AF9; }
            QPushButton[text="Reverse"]:pressed { background: #8F5FE8; }
            QPushButton[text="Cancel"] {
                background: #3A414C;
                color: #E6EBF2;
            }
            QPushButton[text="Cancel"]:hover  { background: #232A3B; }
            QPushButton[text="Cancel"]:pressed { background: #232A3B; }
        """)

        return dlg.exec() == QMessageBox.StandardButton.Yes

    # ── Persistence helpers ───────────────────────────────────────────────────

    def _persist_selection(self) -> None:
        """Save unchecked accounts to config."""
        config_manager.save_unchecked_accounts(sorted(self._unchecked))

    def _persist_hidden(self) -> None:
        """Save hidden accounts to config."""
        config_manager.save_hidden_accounts(sorted(self._hidden))

    def get_selected_accounts(self) -> list[str]:
        """Return list of currently checked account IDs."""
        return [a for a, r in self._account_rows.items() if r.is_checked]

    def is_full_fanout_selection(self) -> bool:
        """True when the selection is "every account the user can see"
        in a default view — i.e. exactly what the built-in All pill
        produces.  FlashTradeView uses this for the *Orders on "All"
        accounts* guard.

        A user-defined group returns False: ``_sync_rows`` filters the
        visible rows to that group's members, so a fully-checked group
        would otherwise look like "everything selected" (the same trap
        ``get_all_accounts`` documents).  Fanning out to a group is a
        deliberate act, and the group hot buttons aren't guarded either.

        Single-account setups return False too — there is no "all vs
        some" distinction to protect.
        """
        if (self._active_group is not None
                and self._active_group not in _DEFAULT_GROUPS):
            return False
        visible = set(self._account_rows)
        return len(visible) > 1 and self.get_checked_accounts() == visible
