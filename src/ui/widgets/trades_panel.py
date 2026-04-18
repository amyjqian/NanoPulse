"""
TradesPanel – control-bar + QStackedWidget hosting the flat and summary
trades views, each wrapped in a ``TradesFilterProxy``.

Public API (unchanged signature)
-------------------------------
add_fill(fill_dict)   – routes the fill into *both* underlying models so
                        flipping the Summary toggle is instant (no re-fetch).
clear_fills()         – wipes both models.

The Settings gear opens ``TradesColumnsDialog`` for whichever view is
active; Apply projects the returned visible-keys list onto the table via
``setColumnHidden`` + ``header.moveSection``.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .trades_control_bar import TradesControlBar
from .trades_columns_dialog import TradesColumnsDialog
from .trades_filter_proxy import TradesFilterProxy
from .trades_model import (
    TradesModel,
    COL_TIME, COL_SYMBOL, COL_SECTYPE, COL_SIDE, COL_QTY, COL_PRICE, COL_EXCH,
    COL_REF, COL_ACCOUNT, COL_EXPIRY, COL_STRIKE_RIGHT, COL_COMM, COL_REAL_PNL,
    COL_ORDER_ID, COL_SUBMITTER, COL_LIQUIDATION, COL_LAST_LIQ,
)
from .trades_summary_model import (
    TradesSummaryModel,
    S_COL_SYMBOL, S_COL_BUYS, S_COL_SELLS, S_COL_NET,
    S_COL_AVG_BUY, S_COL_AVG_SELL, S_COL_NET_TOTAL,
    S_COL_COMMISSION, S_COL_NET_INCL_COMM, S_COL_REALIZED, S_COL_EXCHANGES,
)

log = logging.getLogger(__name__)

# ── Theme tokens ──────────────────────────────────────────────────────────────
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


# Default selected columns per view (keys, in order).  Leaves advanced
# columns hidden so first-run looks like the reference screenshot.
_DEFAULT_FLAT_VISIBLE = [
    "time", "symbol", "sec_type", "side", "qty", "price",
    "exchange", "order_ref", "account", "expiry", "strike_right",
    "commission", "realized_pnl", "submitter",
]
_DEFAULT_SUMMARY_VISIBLE = [
    "symbol", "buys", "sells", "net", "avg_buy", "avg_sell",
    "net_total", "commission", "net_incl_comm", "realized_pnl", "exchanges",
]


class TradesPanel(QWidget):
    """Control-bar + stacked flat/summary tables, each behind a proxy."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._flat_model = TradesModel(self)
        self._summary_model = TradesSummaryModel(self)

        self._flat_proxy = TradesFilterProxy(self, is_summary=False)
        self._flat_proxy.setSourceModel(self._flat_model)

        self._summary_proxy = TradesFilterProxy(self, is_summary=True)
        self._summary_proxy.setSourceModel(self._summary_model)

        # Per-view visible column keys (order matters)
        self._visible_flat = list(_DEFAULT_FLAT_VISIBLE)
        self._visible_summary = list(_DEFAULT_SUMMARY_VISIBLE)

        self._setup_ui()
        self._apply_column_visibility("flat")
        self._apply_column_visibility("summary")

    # ── UI construction ─────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._control_bar = TradesControlBar(self)
        self._control_bar.summaryToggled.connect(self._on_summary_toggled)
        self._control_bar.dateFilterChanged.connect(self._on_date_changed)
        self._control_bar.searchTextChanged.connect(self._on_search_changed)
        self._control_bar.settingsClicked.connect(self._open_columns_dialog)
        root.addWidget(self._control_bar)

        self._stack = QStackedWidget(self)
        self._flat_table = self._make_table(self._flat_proxy)
        self._summary_table = self._make_table(self._summary_proxy)
        self._stack.addWidget(self._flat_table)      # index 0
        self._stack.addWidget(self._summary_table)   # index 1
        root.addWidget(self._stack, 1)

        self._apply_flat_default_widths()
        self._apply_summary_default_widths()

    def _make_table(self, proxy) -> QTableView:
        t = QTableView(self)
        t.setModel(proxy)
        t.setAlternatingRowColors(True)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.verticalHeader().setVisible(False)
        t.setShowGrid(False)
        t.setFont(_TABLE_FONT)
        t.setStyleSheet(_TABLE_STYLE)
        t.verticalHeader().setDefaultSectionSize(30)

        hdr = t.horizontalHeader()
        hdr.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                 | Qt.AlignmentFlag.AlignVCenter)
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionsMovable(True)
        return t

    def _apply_flat_default_widths(self) -> None:
        fm = QFontMetrics(self._flat_table.font())
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
            self._flat_table.setColumnWidth(col, fm.horizontalAdvance(sample) + pad)

    def _apply_summary_default_widths(self) -> None:
        fm = QFontMetrics(self._summary_table.font())
        pad = fm.horizontalAdvance("W")
        samples = {
            S_COL_SYMBOL:        "WWWWWW",
            S_COL_BUYS:          "99999",
            S_COL_SELLS:         "99999",
            S_COL_NET:           "+99999",
            S_COL_AVG_BUY:       "9999.9999",
            S_COL_AVG_SELL:      "9999.9999",
            S_COL_NET_TOTAL:     "+9,999,999.99",
            S_COL_COMMISSION:    "9,999.99",
            S_COL_NET_INCL_COMM: "+9,999,999.99",
            S_COL_REALIZED:      "+9,999.99",
            S_COL_EXCHANGES:     "NASDAQ, ARCA",
        }
        for col, sample in samples.items():
            self._summary_table.setColumnWidth(col, fm.horizontalAdvance(sample) + pad)

    # ── Control-bar handlers ────────────────────────────────────────────────

    def _on_summary_toggled(self, on: bool) -> None:
        self._stack.setCurrentIndex(1 if on else 0)

    def _on_date_changed(self, days: int) -> None:
        self._flat_proxy.setDaysBack(days)
        # Summary proxy ignores date; folding across dates would require
        # re-aggregation which is out of scope for the "instant filter" path.

    def _on_search_changed(self, text: str) -> None:
        self._flat_proxy.setSearchText(text)
        self._summary_proxy.setSearchText(text)

    # ── Column visibility + order ───────────────────────────────────────────

    def _active_view_kind(self) -> str:
        return "summary" if self._stack.currentIndex() == 1 else "flat"

    def _open_columns_dialog(self) -> None:
        kind = self._active_view_kind()
        if kind == "flat":
            all_keys = self._flat_model.column_keys()
            headers = self._flat_model.column_headers()
            visible = self._visible_flat
        else:
            all_keys = self._summary_model.column_keys()
            headers = self._summary_model.column_headers()
            visible = self._visible_summary

        dlg = TradesColumnsDialog(
            view_kind=kind,
            all_keys=all_keys,
            all_headers=headers,
            visible_keys=visible,
            parent=self.window(),
        )
        dlg.columnsApplied.connect(lambda keys, k=kind: self._apply_new_columns(k, keys))
        dlg.exec()

    def _apply_new_columns(self, kind: str, keys: list[str]) -> None:
        if kind == "flat":
            self._visible_flat = list(keys)
        else:
            self._visible_summary = list(keys)
        self._apply_column_visibility(kind)

    def _apply_column_visibility(self, kind: str) -> None:
        """Project the visible-keys list onto the QTableView header:
        hide non-selected columns, then reorder selected ones to match."""
        if kind == "flat":
            table = self._flat_table
            all_keys = self._flat_model.column_keys()
            visible = self._visible_flat
        else:
            table = self._summary_table
            all_keys = self._summary_model.column_keys()
            visible = self._visible_summary
        hdr = table.horizontalHeader()

        key_to_logical = {k: i for i, k in enumerate(all_keys)}

        # 1. Hide / show
        visible_set = set(visible)
        for k, logical in key_to_logical.items():
            hdr.setSectionHidden(logical, k not in visible_set)

        # 2. Reorder.  For each position in the desired order, find the
        # section whose current visual index maps to that key and move it.
        for target_visual, key in enumerate(visible):
            logical = key_to_logical.get(key)
            if logical is None:
                continue
            current_visual = hdr.visualIndex(logical)
            if current_visual != target_visual:
                hdr.moveSection(current_visual, target_visual)

    # ── Public API (unchanged) ──────────────────────────────────────────────

    def add_fill(self, fill: dict) -> None:
        self._flat_model.add_fill(fill)
        self._summary_model.add_fill(fill)

    def clear_fills(self) -> None:
        self._flat_model.clear()
        self._summary_model.clear()
