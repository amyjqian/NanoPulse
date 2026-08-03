"""
PositionsTradesTabs – tabbed container hosting the Positions grid and the
Trades execution log.

The utility controls that belong to each view — Refresh + Zero Pos toggle
for Positions; Summary toggle + date filter + search + gear for Trades —
are pulled out of their panels and parked in this QTabWidget's top-right
corner so they sit on the same horizontal line as the POSITIONS / TRADES
tabs.  A ``QStackedWidget`` swaps between the two bars on tab change so
only the controls relevant to the active view are visible.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QStackedWidget, QTabWidget, QWidget

# ── Theme tokens ──────────────────────────────────────────────────────────────
ACCENT    = "#E08A3C"
BG_PANEL  = "#151922"
# Matches BG_HEADER used by the Positions and Trades column-header rows
# so the tab strip and the header row directly below it read as one band.
BG_HEADER = "#151922"
TEXT      = "#E6EBF2"
TEXT_DIM  = "#A3AFC2"

_TAB_QSS = f"""
    QTabWidget {{
        background: {BG_HEADER};
    }}
    QTabWidget::pane {{
        border: none;
        background: {BG_PANEL};
        top: -1px;
    }}
    QTabWidget::tab-bar {{
        alignment: left;
        left: 4px;
    }}
    QTabBar {{
        qproperty-drawBase: 0;
        background: {BG_HEADER};
    }}
    QTabBar::tab {{
        background: transparent;
        color: {TEXT_DIM};
        padding: 7px 18px;
        margin: 0;
        border: none;
        border-bottom: 2px solid transparent;
        font-family: Inter, "Helvetica Neue", Arial, sans-serif;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.10em;
    }}
    QTabBar::tab:hover {{
        color: {TEXT};
    }}
    QTabBar::tab:selected {{
        color: {ACCENT};
        border-bottom: 2px solid {ACCENT};
    }}
"""

_POSITIONS_TAB_INDEX = 0
_TRADES_TAB_INDEX = 1


class PositionsTradesTabs(QTabWidget):
    """QTabWidget switching between ``positions_widget`` and ``trades_widget``.

    Each child widget may expose a ``control_bar`` attribute; both are
    installed into a single QStackedWidget that lives in the tab-bar
    corner.  Switching tabs flips the stack so only the active view's
    utility controls are visible.
    """

    def __init__(self, positions_widget: QWidget,
                 trades_widget: QWidget, parent=None):
        super().__init__(parent)
        self.setDocumentMode(True)
        self.setTabPosition(QTabWidget.TabPosition.North)
        self.setStyleSheet(_TAB_QSS)
        # Kill the left/right scroll arrows that appear when the corner
        # widget + tabs don't fit.  With only two short tab labels it's
        # much better to let the text elide than to hide tabs behind
        # scroll buttons (which themselves also eat horizontal space).
        self.tabBar().setUsesScrollButtons(False)
        self.setElideMode(Qt.TextElideMode.ElideRight)
        self.addTab(positions_widget, "Pos")
        self.addTab(trades_widget, "Trds")

        self._corner_stack = QStackedWidget(self)
        # Index 0 → Positions bar; Index 1 → Trades bar.  An empty widget
        # is substituted when a view doesn't expose a control_bar so
        # tab-bar geometry stays stable.
        positions_bar = getattr(positions_widget, "control_bar", None) or QWidget()
        trades_bar = getattr(trades_widget, "control_bar", None) or QWidget()
        self._corner_stack.addWidget(positions_bar)    # 0
        self._corner_stack.addWidget(trades_bar)       # 1
        self.setCornerWidget(self._corner_stack, Qt.Corner.TopRightCorner)

        self.currentChanged.connect(self._on_tab_changed)
        self.setCurrentIndex(_POSITIONS_TAB_INDEX)
        self._on_tab_changed(_POSITIONS_TAB_INDEX)

    def _on_tab_changed(self, index: int) -> None:
        self._corner_stack.setCurrentIndex(
            1 if index == _TRADES_TAB_INDEX else 0)
