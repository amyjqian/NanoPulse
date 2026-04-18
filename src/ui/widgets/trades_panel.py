"""
TradesPanel – QTableView wrapper around ``TradesModel``.

Pure presentation layer: exposes ``add_fill`` / ``clear_fills`` which pass
straight through to the model.  FlashTradeView connects the worker's
``fill_updated`` signal to ``add_fill``.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .trades_model import (
    TradesModel,
    COL_TIME, COL_SYMBOL, COL_SECTYPE, COL_SIDE, COL_QTY, COL_PRICE, COL_EXCH,
    COL_REF, COL_ACCOUNT, COL_EXPIRY, COL_STRIKE_RIGHT, COL_COMM, COL_REAL_PNL,
    COL_ORDER_ID, COL_SUBMITTER, COL_LIQUIDATION, COL_LAST_LIQ,
)

log = logging.getLogger(__name__)

# ── Theme tokens (dark mode, matches OrderStatusPanel / AccountPositionPanel) ─
BG_TABLE     = "#1A1D24"
BG_TABLE_ALT = "#1E222A"
BG_HEADER    = "#14171D"
BORDER       = "#30363D"
TEXT         = "#C9D1D9"
TEXT_DIM     = "#8B949E"

_TABLE_FONT = QFont("Menlo, Consolas, monospace", 11)

_TABLE_STYLE = f"""
    QTableView {{
        background-color: {BG_TABLE};
        alternate-background-color: {BG_TABLE_ALT};
        color: {TEXT};
        gridline-color: {BORDER};
        border: none;
        font-size: 11px;
        selection-background-color: #253545;
        selection-color: {TEXT};
    }}
    QHeaderView::section {{
        background-color: {BG_HEADER};
        color: {TEXT_DIM};
        border: none;
        border-bottom: 1px solid {BORDER};
        border-right: 1px solid {BORDER};
        padding: 4px 8px;
        font-weight: 700;
        font-size: 10px;
        letter-spacing: 0.06em;
    }}
    QTableView::item {{ padding: 0px 6px; }}
    QScrollBar:vertical {{
        background: {BG_TABLE};
        width: 8px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: #4A4E58;
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{ background: #5E6370; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar:horizontal {{
        background: {BG_TABLE};
        height: 10px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background: #4A4E58;
        border-radius: 4px;
        min-width: 40px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: #5E6370; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
"""


class TradesPanel(QWidget):
    """Flat execution log bound to a ``TradesModel``.

    Public API
    ----------
    add_fill(fill_dict)
    clear_fills()
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = TradesModel(self)
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setFont(_TABLE_FONT)
        self._table.setStyleSheet(_TABLE_STYLE)

        hdr = self._table.horizontalHeader()
        hdr.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        self._apply_default_column_widths()
        self._table.verticalHeader().setDefaultSectionSize(30)

        root.addWidget(self._table, stretch=1)

    def _apply_default_column_widths(self) -> None:
        fm = QFontMetrics(self._table.font())
        pad = fm.horizontalAdvance("W")
        samples = {
            COL_TIME:         "Mmm00 00:00:00",
            COL_SYMBOL:       "WWWWWW",
            COL_SECTYPE:      "FUTW",
            COL_SIDE:         "BOTW",
            COL_QTY:          "99999",
            COL_PRICE:        "9999.99",
            COL_EXCH:         "NASDAQW",
            COL_REF:          "nano_0000_000000_wwww",
            COL_ACCOUNT:      "WWWWWWWWW",
            COL_EXPIRY:       "YYYYMMDD",
            COL_STRIKE_RIGHT: "99999 P",
            COL_COMM:         "999.99",
            COL_REAL_PNL:     "+9999.99",
            COL_ORDER_ID:     "999999",
            COL_SUBMITTER:    "qiancapital168",
            COL_LIQUIDATION:  "LIQ",
            COL_LAST_LIQ:     "LLQ",
        }
        for col, sample in samples.items():
            w = fm.horizontalAdvance(sample) + pad
            self._table.setColumnWidth(col, w)

    # ── Public API ──────────────────────────────────────────────────────────

    def add_fill(self, fill: dict) -> None:
        self._model.add_fill(fill)

    def clear_fills(self) -> None:
        self._model.clear()
