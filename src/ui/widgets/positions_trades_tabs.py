"""
PositionsTradesTabs – tabbed container hosting the Positions grid, the
Trades execution log and the Options Income ticket.

The utility controls that belong to each view — Refresh + Zero Pos toggle
for Positions; Summary toggle + date filter + search + gear for Trades —
are pulled out of their panels and parked in this QTabWidget's top-right
corner so they sit on the same horizontal line as the POSITIONS / TRADES /
OPTIONS tabs.  A ``QStackedWidget`` swaps between the bars on tab change
so only the controls relevant to the active view are visible (the Options
ticket exposes no bar, so its page is an empty widget that keeps the
tab-bar geometry stable).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QStackedWidget, QTabWidget, QWidget

from src.ui.theme import Theme
from . import table_theme as tt

# ── Theme tokens ──────────────────────────────────────────────────────────────
BG_PANEL  = Theme.POS_BG_PAGE
TEXT      = "#eeeef0"
TEXT_DIM  = "#7a8494"
TEXT_HOVER = Theme.POS_TEXT_2
ACCENT    = Theme.POS_ACCENT

# Underline tabs: the selected tab is marked by a 2px accent rule flush
# with the toolbar's bottom border, not by a raised chip.
_TAB_QSS = f"""
    QTabWidget {{
        background: transparent;
    }}
    QTabWidget::pane {{
        border: none;
        border-top: 1px solid {Theme.POS_BORDER};
        background: {BG_PANEL};
        top: -1px;
    }}
    QTabWidget::tab-bar {{
        alignment: left;
        left: 4px;
    }}
    QTabBar {{
        qproperty-drawBase: 0;
        background: transparent;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {TEXT_DIM};
        padding: 6px 4px;
        margin: 2px 18px 0 0;
        border: none;
        border-bottom: 2px solid transparent;
        font-family: Inter, "Helvetica Neue", Arial, sans-serif;
        font-size: 13px;
        font-weight: 600;
    }}
    QTabBar::tab:hover {{
        color: {TEXT_HOVER};
    }}
    QTabBar::tab:selected {{
        color: {TEXT};
        border-bottom: 2px solid {ACCENT};
    }}
"""

_POSITIONS_TAB_INDEX = 0
_TRADES_TAB_INDEX = 1
_OPTIONS_TAB_INDEX = 2


class PositionsTradesTabs(QTabWidget):
    """QTabWidget switching between ``positions_widget``, ``trades_widget``
    and ``options_widget``.

    Each child widget may expose a ``control_bar`` attribute; the bars
    are installed into a single QStackedWidget that lives in the tab-bar
    corner, one page per tab (an empty widget stands in for a view
    without a bar).  Switching tabs flips the stack so only the active
    view's utility controls are visible.
    """

    def __init__(self, positions_widget: QWidget,
                 trades_widget: QWidget, options_widget: QWidget,
                 parent=None):
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
        self.addTab(positions_widget, "Positions")     # _POSITIONS_TAB_INDEX
        self.addTab(trades_widget, "Trades")           # _TRADES_TAB_INDEX
        self.addTab(options_widget, "Options")         # _OPTIONS_TAB_INDEX

        self._corner_stack = QStackedWidget(self)
        # Stack page i ↔ tab i.  An empty widget is substituted when a
        # view doesn't expose a control_bar so tab-bar geometry stays
        # stable.
        positions_bar = getattr(positions_widget, "control_bar", None) or QWidget()
        trades_bar = getattr(trades_widget, "control_bar", None) or QWidget()
        options_bar = getattr(options_widget, "control_bar", None) or QWidget()
        self._corner_stack.addWidget(positions_bar)    # 0
        self._corner_stack.addWidget(trades_bar)       # 1
        self._corner_stack.addWidget(options_bar)      # 2
        # The stack right-aligns the bars against the same edge, so a
        # control only stays put across a tab switch if each bar carries
        # the same width to the right of it.  The bars keep their own
        # control order; this pads whichever one is short at each anchor
        # (Summary pill, Zero Pos / date filter, search box).  Bars
        # without anchors (the Options page) are skipped.
        tt.align_control_bars(positions_bar, trades_bar, options_bar)
        self.setCornerWidget(self._corner_stack, Qt.Corner.TopRightCorner)

        self.currentChanged.connect(self._on_tab_changed)
        self.setCurrentIndex(_POSITIONS_TAB_INDEX)
        self._on_tab_changed(_POSITIONS_TAB_INDEX)

    def _on_tab_changed(self, index: int) -> None:
        # Stack pages mirror the tab order, so the index maps 1:1.
        self._corner_stack.setCurrentIndex(
            min(max(index, 0), self._corner_stack.count() - 1))
