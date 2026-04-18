"""
TradesModel – flat execution log (QAbstractTableModel).

One row per Fill.  No parent-level aggregation; each child fill from a
multi-exchange order is a distinct chronological row.  New fills are
inserted at index 0.  Deduplication is by execId so the startup
re-snapshot after a reconnect is idempotent.
"""
from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont

log = logging.getLogger(__name__)

# ── Column indices ────────────────────────────────────────────────────────────
COL_TIME          = 0
COL_SYMBOL        = 1
COL_SECTYPE       = 2
COL_SIDE          = 3
COL_QTY           = 4
COL_PRICE         = 5
COL_EXCH          = 6
COL_REF           = 7
COL_ACCOUNT       = 8
COL_EXPIRY        = 9
COL_STRIKE_RIGHT  = 10
COL_COMM          = 11
COL_REAL_PNL      = 12
COL_ORDER_ID      = 13
COL_SUBMITTER     = 14
COL_LIQUIDATION   = 15
COL_LAST_LIQ      = 16

HEADERS: list[str] = [
    "TIME", "SYMBOL", "TYPE", "SIDE", "QTY", "PRICE", "EXCH", "REF",
    "ACCOUNT", "EXPIRY", "STRIKE / RIGHT", "COMMISSION", "REALIZED PNL",
    "ORDER ID", "SUBMITTER", "LIQUIDATION", "LAST LIQUIDITY",
]

_NUM_COLS = len(HEADERS)

_RIGHT_ALIGN_COLS = {
    COL_QTY, COL_PRICE, COL_STRIKE_RIGHT,
    COL_COMM, COL_REAL_PNL, COL_ORDER_ID,
    COL_LAST_LIQ,
}

# ── Theme tokens (match the rest of the FlashTrade panels) ───────────────────
CLR_BUY    = "#3FB950"   # BOT – green
CLR_SELL   = "#F85149"   # SLD – red
TEXT       = "#C9D1D9"
TEXT_DIM   = "#8B949E"
TEXT_BOLD  = "#E6EDF3"


class TradesModel(QAbstractTableModel):
    """Flat execution log — one row per Fill, newest first."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        # execId → row index (for dedupe + surgical commission patches)
        self._index: dict[str, int] = {}

        self._side_font = QFont("Helvetica Neue, Arial", 11)
        self._side_font.setWeight(QFont.Weight.Bold)

        self._symbol_font = QFont("Helvetica Neue, Arial", 11)
        self._symbol_font.setWeight(QFont.Weight.DemiBold)

    # ── Qt model plumbing ───────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else _NUM_COLS

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole \
                and orientation == Qt.Orientation.Horizontal \
                and 0 <= section < _NUM_COLS:
            return HEADERS[section]
        return None

    def data(self, index: QModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row_idx = index.row()
        if row_idx < 0 or row_idx >= len(self._rows):
            return None
        row = self._rows[row_idx]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return self._cell_text(row, col)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._cell_color(row, col)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in _RIGHT_ALIGN_COLS:
                return int(Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.FontRole:
            if col == COL_SIDE:
                return self._side_font
            if col == COL_SYMBOL:
                return self._symbol_font
        return None

    # ── Cell rendering ──────────────────────────────────────────────────────

    @staticmethod
    def _fmt_qty(v: float) -> str:
        if not v:
            return ""
        return f"{int(v)}" if float(v).is_integer() else f"{v:g}"

    def _cell_text(self, row: dict, col: int) -> str:
        if col == COL_TIME:
            return row.get("time", "") or ""
        if col == COL_SYMBOL:
            return row.get("symbol", "") or ""
        if col == COL_SECTYPE:
            return row.get("sec_type", "") or ""
        if col == COL_SIDE:
            return row.get("side", "") or ""
        if col == COL_QTY:
            return self._fmt_qty(float(row.get("shares", 0) or 0))
        if col == COL_PRICE:
            price = float(row.get("price", 0.0) or 0.0)
            return f"{price:.2f}" if price else ""
        if col == COL_EXCH:
            return row.get("exchange", "") or ""
        if col == COL_REF:
            return row.get("order_ref", "") or ""
        if col == COL_ACCOUNT:
            return row.get("account", "") or ""
        if col == COL_EXPIRY:
            return row.get("expiry", "") or ""
        if col == COL_STRIKE_RIGHT:
            strike = float(row.get("strike", 0) or 0)
            right = row.get("right", "") or ""
            if strike and right:
                return f"{strike:g} {right}"
            if strike:
                return f"{strike:g}"
            return right
        if col == COL_COMM:
            c = row.get("commission")
            return "…" if c is None else f"{c:.2f}"
        if col == COL_REAL_PNL:
            pnl = row.get("realized_pnl")
            if pnl is None:
                return ""
            if pnl == 0:
                return "0.00"
            return f"{pnl:+.2f}"
        if col == COL_ORDER_ID:
            oid = int(row.get("order_id", 0) or 0)
            return str(oid) if oid else ""
        if col == COL_SUBMITTER:
            return row.get("label", "") or ""
        if col == COL_LIQUIDATION:
            return "Y" if row.get("liquidation") else ""
        if col == COL_LAST_LIQ:
            ll = int(row.get("last_liquidity", 0) or 0)
            return str(ll) if ll else ""
        return ""

    def _cell_color(self, row: dict, col: int) -> QColor | None:
        side = row.get("side", "")
        if col == COL_SIDE:
            if side == "BOT":
                return QColor(CLR_BUY)
            if side == "SLD":
                return QColor(CLR_SELL)
            return QColor(TEXT)
        if col == COL_REAL_PNL:
            pnl = row.get("realized_pnl")
            if pnl is None:
                return QColor(TEXT_DIM)
            if pnl > 0:
                return QColor(CLR_BUY)
            if pnl < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        if col in (COL_PRICE, COL_SYMBOL):
            return QColor(TEXT_BOLD)
        if col in (COL_SECTYPE, COL_REF, COL_EXCH, COL_ACCOUNT, COL_EXPIRY,
                   COL_ORDER_ID, COL_SUBMITTER,
                   COL_LIQUIDATION, COL_LAST_LIQ):
            return QColor(TEXT_DIM)
        return QColor(TEXT)

    # ── Public mutation API ─────────────────────────────────────────────────

    def add_fill(self, fill: dict) -> None:
        """Insert a new fill at row 0, or refresh in place if its execId
        is already present (dedupe across startup re-snapshots)."""
        exec_id = fill.get("exec_id", "")

        if exec_id and exec_id in self._index:
            row = self._index[exec_id]
            self._rows[row] = fill
            top_left = self.index(row, 0)
            bottom_right = self.index(row, _NUM_COLS - 1)
            self.dataChanged.emit(top_left, bottom_right)
            return

        self.beginInsertRows(QModelIndex(), 0, 0)
        self._rows.insert(0, fill)
        if self._index:
            self._index = {eid: r + 1 for eid, r in self._index.items()}
        if exec_id:
            self._index[exec_id] = 0
        self.endInsertRows()

    def clear(self) -> None:
        if not self._rows:
            return
        self.beginResetModel()
        self._rows.clear()
        self._index.clear()
        self.endResetModel()
