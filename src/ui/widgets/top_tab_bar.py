"""
Top navigation tab bar – horizontal tabs with icons.

Style matches the IBKR-style dark tab strip:
dark background, icon + label per tab, accent underline on the active tab.
"""
from __future__ import annotations

import time

from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QPushButton, QSizePolicy, QWidget,
)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPainter, QColor, QFont, QPen

BAR_HEIGHT = 36
BAR_BG = "#151922"
HOVER_BG = "#1B2130"
ACTIVE_BG = "#1B2130"
ACCENT = "#3FB950"
TEXT_PRIMARY = "#E6EBF2"
TEXT_SECONDARY = "#A3AFC2"
BORDER = "#262E3F"

# Pre-cached QColor instances (avoid allocating on every paint call)
_C_BAR_BG = QColor(BAR_BG)
_C_HOVER = QColor(HOVER_BG)
_C_ACTIVE = QColor(ACTIVE_BG)
_C_ACCENT = QColor(ACCENT)
_C_TEXT_PRI = QColor(TEXT_PRIMARY)
_C_TEXT_SEC = QColor(TEXT_SECONDARY)
_C_BORDER = QColor(BORDER)

# Pre-cached fonts
_FONT_ACTIVE = QFont("Inter, Helvetica Neue, Arial", 11)
_FONT_ACTIVE.setWeight(QFont.Weight.Medium)
_FONT_NORMAL = QFont("Inter, Helvetica Neue, Arial", 11)
_FONT_NORMAL.setWeight(QFont.Weight.Normal)

# Unicode icons (lightweight, no image files needed)
_ICONS: dict[str, str] = {
    "Connect":        "⚡",
    "Nano":           "⚡",
    "StrategyEditor": "📐",
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
    # User clicked the padlock — host should lock the session.
    lockRequested = Signal()

    _TABS = ["Connect", "Nano", "StrategyEditor", "Settings"]

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

        # Active trading-group chip — centered between the tab
        # buttons and the right-side Accounts pill / status text.
        # Same blue + bold styling as the selected chips in the
        # AccountListPanel filter strip so the two read as the same
        # affordance.  The chip text is "<group> <selected>" (e.g.
        # "Alpha 2") so the user can see both the active group and
        # how many accounts are currently checked inside it.  Hidden
        # when no group is active.
        # Active-group chip + Account pill sit side-by-side on the
        # right, with the chip immediately to the left of the pill
        # (both styled + sized to match the symbol chips in
        # order_panel_parts.py — padding 4×12, font-size 12, radius 6,
        # fixed height 24 so a QLabel and a QPushButton render at the
        # same pixel height regardless of platform metrics).
        _CHIP_HEIGHT = 24
        self._group_label = QPushButton("", self)
        self._group_label.setFont(_FONT_NORMAL)
        self._group_label.setEnabled(False)  # non-interactive — just a badge.
        self._group_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._group_label.setFixedHeight(_CHIP_HEIGHT)
        self._group_label.setStyleSheet(
            "QPushButton {"
            " background: #539BF5; color: white;"
            " border: none; border-radius: 6px;"
            " padding: 4px 12px; margin-right: 8px;"
            " font-weight: bold; font-size: 12px;"
            "}"
            "QPushButton:disabled { background: #539BF5; color: white; }")
        self._group_label.hide()
        layout.addWidget(self._group_label)

        # "Account" pill — always visible.  Click toggles the
        # AccountListPanel inside the Nano tab.  When clicked from a
        # different tab, the host switches to Nano first.
        self._accounts_pill = QPushButton("Account", self)
        self._accounts_pill.setFont(_FONT_NORMAL)
        self._accounts_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self._accounts_pill.setFixedHeight(_CHIP_HEIGHT)
        self._accounts_pill.setStyleSheet(
            "QPushButton {"
            " background: #539BF5; color: white;"
            " border: none; border-radius: 6px;"
            " padding: 4px 12px; margin-right: 12px;"
            " font-weight: bold; font-size: 12px;"
            "}"
            "QPushButton:hover { background: #6BA8F6; }")
        self._accounts_pill.clicked.connect(
            self.accountsToggleRequested.emit)
        layout.addWidget(self._accounts_pill)

        # Padlock — locks the session (opaque overlay, password to
        # resume; ⌘L does the same).  Neutral chip styling so it reads
        # as chrome, not a trading action.  Hidden until the host
        # confirms an authenticated session (set_lock_visible).
        self._lock_btn = QPushButton("🔒", self)
        self._lock_btn.setFont(_FONT_NORMAL)
        self._lock_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lock_btn.setFixedHeight(_CHIP_HEIGHT)
        self._lock_btn.setToolTip("Lock session (⌘L)")
        self._lock_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._lock_btn.setStyleSheet(
            "QPushButton {"
            " background: #232A3B; color: #A3AFC2;"
            " border: none; border-radius: 6px;"
            " padding: 4px 10px; margin-right: 12px;"
            " font-size: 12px;"
            "}"
            "QPushButton:hover { background: #334058; color: white; }")
        self._lock_btn.clicked.connect(self.lockRequested.emit)
        self._lock_btn.hide()
        layout.addWidget(self._lock_btn)

        # Activate first tab
        first = next(iter(self._buttons.values()))
        first.set_active(True)

    def set_active_group(
        self, name: str | None, selected_count: int = 0,
    ) -> None:
        """Show / hide the blue active-group chip centered in the tab
        bar.  When a group is active the chip text combines the group
        name with the number of accounts currently checked inside
        it, e.g. ``"Alpha 2"``.  Hidden when *name* is ``None`` or
        empty."""
        if not name:
            self._group_label.hide()
            return
        self._group_label.setText(f"{name} {int(selected_count)}")
        self._group_label.show()


    def set_tab_visible(self, name: str, visible: bool) -> None:
        """Show/hide a single tab button (used for role gating — an admin
        cannot trade, so the Nano / StrategyEditor tabs are hidden)."""
        btn = self._buttons.get(name)
        if btn is not None:
            btn.setVisible(visible)

    def set_accounts_pill_visible(self, visible: bool) -> None:
        self._accounts_pill.setVisible(visible)

    def set_lock_visible(self, visible: bool) -> None:
        """Show the padlock only for authenticated sessions — legacy /
        test construction has no user to re-authenticate."""
        self._lock_btn.setVisible(visible)

    def _on_tab_click(self, name: str) -> None:
        for label, btn in self._buttons.items():
            btn.set_active(label == name)
        self.nav_changed.emit(name)
        self.tabBarClicked.emit(name)  # Emit signal for all tabs

    # ── Title-bar gestures ────────────────────────────────────────────────────

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        """Double-click empty title-bar space → toggle window zoom,
        matching the native macOS title-bar behavior.  Interactive
        children (tab buttons, Account pill, group chip) receive and
        accept their own events, so this handler only fires for
        clicks on the bar's empty regions.

        Guarded with a short debounce window: macOS occasionally
        delivers the platform's own zoom double-click AND a Qt
        ``mouseDoubleClickEvent`` for the same gesture, and a burst
        of rapid clicks can also land two events back-to-back.  We
        therefore ignore any second toggle that arrives within
        Qt's ``doubleClickInterval`` of the previous one, which
        previously caused the window to grow and snap back.
        """
        if event.button() == Qt.MouseButton.LeftButton:
            interval_ms = max(QApplication.doubleClickInterval(), 300)
            now_ms = int(time.monotonic() * 1000)
            last_ms = getattr(self, "_last_zoom_toggle_ms", 0)
            if now_ms - last_ms < interval_ms:
                event.accept()
                return
            self._last_zoom_toggle_ms = now_ms
            win = self.window()
            if win is not None:
                if win.isMaximized() or win.isFullScreen():
                    win.showNormal()
                else:
                    win.showMaximized()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), _C_BAR_BG)
        p.setPen(QPen(_C_BORDER, 1))
        p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()
