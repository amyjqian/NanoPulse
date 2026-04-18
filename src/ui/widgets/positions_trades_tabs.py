"""
PositionsTradesTabs – tabbed container hosting the Positions grid and the
Trades execution log.

Uses a plain ``QTabWidget`` in document-mode with a custom stylesheet so
that:
  * the pane has no visible border,
  * the tab bar sits flush on the panel background,
  * unselected tabs are subdued gray,
  * the selected tab shows its label in the #CC6600 accent and carries a
    2 px accent-coloured bottom border.
"""
from __future__ import annotations

from PySide6.QtWidgets import QTabWidget, QWidget

# ── Theme tokens ──────────────────────────────────────────────────────────────
ACCENT   = "#CC6600"
BG_PANEL = "#1e2230"
TEXT     = "#C9D1D9"
TEXT_DIM = "#8B949E"

_TAB_QSS = f"""
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
        background: {BG_PANEL};
    }}
    QTabBar::tab {{
        background: transparent;
        color: {TEXT_DIM};
        padding: 7px 18px;
        margin: 0;
        border: none;
        border-bottom: 2px solid transparent;
        font-family: "Helvetica Neue", Arial, sans-serif;
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


class PositionsTradesTabs(QTabWidget):
    """QTabWidget switching between ``positions_widget`` and ``trades_widget``."""

    def __init__(self, positions_widget: QWidget,
                 trades_widget: QWidget, parent=None):
        super().__init__(parent)
        self.setDocumentMode(True)
        self.setTabPosition(QTabWidget.TabPosition.North)
        self.setStyleSheet(_TAB_QSS)
        self.addTab(positions_widget, "POSITIONS")
        self.addTab(trades_widget, "TRADES")
        self.setCurrentIndex(0)
