"""
Top navigation tab bar – horizontal tabs with icons.

Style matches the IBKR-style dark tab strip:
dark background, icon + label per tab, accent underline on the active tab.
"""
from __future__ import annotations

from PySide6.QtWidgets import QWidget, QHBoxLayout, QSizePolicy, QLabel, QPushButton
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPainter, QColor, QFont, QPen

BAR_HEIGHT = 36
BAR_BG = "#161B22"
HOVER_BG = "#1C2128"
ACTIVE_BG = "#1F2937"
ACCENT = "#3FB950"
TEXT_PRIMARY = "#C9D1D9"
TEXT_SECONDARY = "#8B949E"
BORDER = "#30363D"

# Pre-cached QColor instances (avoid allocating on every paint call)
_C_BAR_BG = QColor(BAR_BG)
_C_HOVER = QColor(HOVER_BG)
_C_ACTIVE = QColor(ACTIVE_BG)
_C_ACCENT = QColor(ACCENT)
_C_TEXT_PRI = QColor(TEXT_PRIMARY)
_C_TEXT_SEC = QColor(TEXT_SECONDARY)
_C_BORDER = QColor(BORDER)

# Pre-cached fonts
_FONT_ACTIVE = QFont("Helvetica Neue, Arial", 11)
_FONT_ACTIVE.setWeight(QFont.Weight.Medium)
_FONT_NORMAL = QFont("Helvetica Neue, Arial", 11)
_FONT_NORMAL.setWeight(QFont.Weight.Normal)

# Unicode icons (lightweight, no image files needed)
_ICONS: dict[str, str] = {
    "Connect":        "⚡",
    "Contracts":      "🔍",
    "Nano":           "⚡",
    "StrategyEditor": "📐",
    "Executions":     "📊",
    "Settings":       "⚙",
}


class TabButton(QWidget):
    """A single tab in the top navigation bar."""

    clicked = Signal(str)

    def __init__(self, label: str, icon_char: str = "", parent=None):
        super().__init__(parent)
        self._label = label
        self._icon_char = icon_char
        self._display = f"{icon_char}  {label}" if icon_char else label
        self._active = False
        self._hovered = False

        self.setFixedHeight(BAR_HEIGHT)
        self.setMinimumWidth(90)
        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    # ── State ────────────────────────────────────────────────────────────────

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self.update()

    # ── Events ───────────────────────────────────────────────────────────────

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._label)

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()

        # Background
        if self._active:
            p.fillRect(0, 0, w, h, _C_ACTIVE)
            p.fillRect(0, h - 2, w, 2, _C_ACCENT)
        elif self._hovered:
            p.fillRect(0, 0, w, h, _C_HOVER)
        else:
            p.fillRect(0, 0, w, h, _C_BAR_BG)

        # Text
        p.setPen(_C_TEXT_PRI if self._active else _C_TEXT_SEC)
        p.setFont(_FONT_ACTIVE if self._active else _FONT_NORMAL)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._display)
        p.end()

    def sizeHint(self) -> QSize:
        return QSize(120, BAR_HEIGHT)


class TopTabBar(QWidget):
    """Horizontal top navigation bar."""

    nav_changed = Signal(str)
    # Emits tab name for ALL tabs (including Settings)
    tabBarClicked = Signal(str)
    # User clicked the "Account" pill — host should toggle the
    # Account Management panel (and switch to the Nano tab when the
    # current tab is something else).
    accountsToggleRequested = Signal()

    _TABS = ["Connect", "Contracts", "Nano", "StrategyEditor", "Executions", "Settings"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(BAR_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._buttons: dict[str, TabButton] = {}
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        for name in self._TABS:
            btn = TabButton(name, _ICONS.get(name, ""), self)
            btn.clicked.connect(self._on_tab_click)
            self._buttons[name] = btn
            layout.addWidget(btn)

        layout.addStretch(1)

        # Active trading-group chip — sits left of the Conn/Accts/
        # Selected status text.  Same blue + bold styling as the
        # selected chips in the AccountListPanel filter strip so the
        # two read as the same affordance.  Hidden when no group is
        # active.
        self._group_label = QLabel(self)
        self._group_label.setFont(_FONT_NORMAL)
        self._group_label.setStyleSheet(
            "QLabel {"
            " background: #0071e3; color: white;"
            " border-radius: 4px;"
            " padding: 2px 10px; margin-right: 12px;"
            " font-weight: bold;"
            "}")
        self._group_label.hide()
        layout.addWidget(self._group_label)

        # "Account" pill — always visible.  Click toggles the
        # AccountListPanel inside the Nano tab.  When clicked from a
        # different tab, the host switches to Nano first.  Same blue
        # chip styling as the active-group chip so they read as part
        # of the same affordance family.
        self._accounts_pill = QPushButton("Account", self)
        self._accounts_pill.setFont(_FONT_NORMAL)
        self._accounts_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self._accounts_pill.setStyleSheet(
            "QPushButton {"
            " background: #0071e3; color: white;"
            " border: none; border-radius: 4px;"
            " padding: 2px 12px; margin-right: 12px;"
            " font-weight: bold;"
            "}"
            "QPushButton:hover { background: #0077ed; }")
        self._accounts_pill.clicked.connect(
            self.accountsToggleRequested.emit)
        layout.addWidget(self._accounts_pill)

        self._status_label = QLabel(self)
        self._status_label.setFont(_FONT_NORMAL)
        self._status_label.setStyleSheet(f"QLabel {{ color: {TEXT_PRIMARY}; padding-right: 20px; }}")
        self._status_label.setText("")
        layout.addWidget(self._status_label)

        # Activate first tab
        first = next(iter(self._buttons.values()))
        first.set_active(True)

    def set_status(self, conn_count: int, acct_count: int,
                   selected_count: int = 0) -> None:
        """Update the top-right stats label.

        Format: ``Conn: 2 | Accts: 5 | Selected: 3``
        When everything is zero the label is hidden.
        """
        if conn_count == 0 and acct_count == 0 and selected_count == 0:
            self._status_label.setText("")
            return
        text = (f"Conn: {conn_count}"
                f"  |  Accts: {acct_count}"
                f"  |  Selected: {selected_count}")
        self._status_label.setText(text)

    def set_active_group(self, name: str | None) -> None:
        """Show / hide the blue active-group chip just left of the
        Conn / Accts / Selected status text.  Hidden when *name* is
        ``None`` or empty."""
        if not name:
            self._group_label.hide()
            return
        self._group_label.setText(name)
        self._group_label.show()


    def _on_tab_click(self, name: str) -> None:
        for label, btn in self._buttons.items():
            btn.set_active(label == name)
        self.nav_changed.emit(name)
        self.tabBarClicked.emit(name)  # Emit signal for all tabs

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), _C_BAR_BG)
        p.setPen(QPen(_C_BORDER, 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()
