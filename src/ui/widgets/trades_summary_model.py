"""
TradesSummaryModel – one row per symbol, aggregated across every fill.

Columns: Financial Instrument, Buys, Sells, Net, Avg Buy Price,
Avg Sell Price, Net Total, Commission, Net Incl. Comm, Realized P&L,
Exchange List.

The model owns the aggregate state.  ``add_fill`` either updates an
existing row (dedupe by execId) or folds a new fill into its symbol's
bucket and emits the right ``beginInsertRows`` / ``dataChanged`` pairs.
"""
from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont

log = logging.getLogger(__name__)

# ── Column indices ────────────────────────────────────────────────────────────
S_COL_SYMBOL        = 0
S_COL_BUYS          = 1
S_COL_SELLS         = 2
S_COL_NET           = 3
S_COL_AVG_BUY       = 4
S_COL_AVG_SELL      = 5
S_COL_NET_TOTAL     = 6
S_COL_COMMISSION    = 7
S_COL_NET_INCL_COMM = 8
S_COL_REALIZED      = 9
S_COL_EXCHANGES     = 10

S_HEADERS: list[str] = [
    "Financial instrument", "Buys", "Sells", "Net",
    "Avg buy price", "Avg sell price", "Net total",
    "Comm", "Net incl. comm", "Rlz P&L", "Exchanges",
]

# Column key used by the preferences dialog / column visibility state
S_COL_KEYS: list[str] = [
    "symbol", "buys", "sells", "net", "avg_buy", "avg_sell",
    "net_total", "commission", "net_incl_comm", "realized_pnl",
    "exchanges",
]

_S_NUM_COLS = len(S_HEADERS)

_S_RIGHT_ALIGN_COLS = {
    S_COL_BUYS, S_COL_SELLS, S_COL_NET,
    S_COL_AVG_BUY, S_COL_AVG_SELL, S_COL_NET_TOTAL,
    S_COL_COMMISSION, S_COL_NET_INCL_COMM, S_COL_REALIZED,
}

# Theme tokens (keep in sync with trades_model)
CLR_BUY    = "#3FB950"
CLR_SELL   = "#F85149"
TEXT       = "#C9D1D9"
TEXT_DIM   = "#8B949E"
TEXT_BOLD  = "#E6EDF3"


class TradesSummaryModel(QAbstractTableModel):
    """Symbol-grouped aggregate view of the same fill stream feeding
    ``TradesModel``.  One row per symbol."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # symbol → aggregate dict
        self._agg: dict[str, dict] = {}
        # ordered list of symbols as presented (insertion order of first fill)
        self._order: list[str] = []
        # execId → raw fill dict — keeps the last flattened fill per execId so
        # a re-snapshot that re-emits the same fill doesn't double-count it
        self._fills: dict[str, dict] = {}

        self._symbol_font = QFont("Helvetica Neue, Arial", 11)
        self._symbol_font.setWeight(QFont.Weight.DemiBold)

    # ── Proxy helpers ───────────────────────────────────────────────────────

    def row_data(self, row: int) -> dict | None:
        if 0 <= row < len(self._order):
            return self._agg[self._order[row]]
        return None

    def column_keys(self) -> list[str]:
        return list(S_COL_KEYS)

    def column_headers(self) -> list[str]:
        return list(S_HEADERS)

    # ── Qt model plumbing ───────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._order)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else _S_NUM_COLS

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole \
                and orientation == Qt.Orientation.Horizontal \
                and 0 <= section < _S_NUM_COLS:
            return S_HEADERS[section]
        return None

    def data(self, index: QModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._order):
            return None
        agg = self._agg[self._order[row]]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            return self._cell_text(agg, col)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._cell_color(agg, col)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in _S_RIGHT_ALIGN_COLS:
                return int(Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.FontRole and col == S_COL_SYMBOL:
            return self._symbol_font
        return None

    # ── Cell rendering ──────────────────────────────────────────────────────

    @staticmethod
    def _fmt_qty(v: float) -> str:
        if not v:
            return "0"
        return f"{int(v)}" if float(v).is_integer() else f"{v:g}"

    @staticmethod
    def _fmt_money(v: float) -> str:
        return f"{v:,.2f}" if v else "0.00"

    @staticmethod
    def _fmt_signed(v: float) -> str:
        if v == 0:
            return "0.00"
        return f"{v:+,.2f}"

    def _cell_text(self, agg: dict, col: int) -> str:
        if col == S_COL_SYMBOL:
            return agg.get("symbol", "")
        if col == S_COL_BUYS:
            return self._fmt_qty(agg.get("buys_qty", 0.0))
        if col == S_COL_SELLS:
            return self._fmt_qty(agg.get("sells_qty", 0.0))
        if col == S_COL_NET:
            net = agg.get("buys_qty", 0.0) - agg.get("sells_qty", 0.0)
            if net == 0:
                return "0"
            return f"{int(net):+}" if float(net).is_integer() else f"{net:+g}"
        if col == S_COL_AVG_BUY:
            b_q = agg.get("buys_qty", 0.0)
            return f"{(agg.get('buys_notional', 0.0) / b_q):.4f}" if b_q else ""
        if col == S_COL_AVG_SELL:
            s_q = agg.get("sells_qty", 0.0)
            return f"{(agg.get('sells_notional', 0.0) / s_q):.4f}" if s_q else ""
        if col == S_COL_NET_TOTAL:
            return self._fmt_signed(self._net_total(agg))
        if col == S_COL_COMMISSION:
            c = agg.get("commission", 0.0)
            return f"{c:,.2f}" if c else ""
        if col == S_COL_NET_INCL_COMM:
            return self._fmt_signed(self._net_total(agg) - agg.get("commission", 0.0))
        if col == S_COL_REALIZED:
            pnl = agg.get("realized_pnl", 0.0)
            if pnl == 0:
                # If no fill carried a commissionReport we render blank rather
                # than a misleading 0.00.
                return "0.00" if agg.get("has_any_realized") else ""
            return self._fmt_signed(pnl)
        if col == S_COL_EXCHANGES:
            return ", ".join(sorted(agg.get("exchanges", set())))
        return ""

    @staticmethod
    def _net_total(agg: dict) -> float:
        """Cash-flow net: sells proceeds minus buys cost."""
        return agg.get("sells_notional", 0.0) - agg.get("buys_notional", 0.0)

    def _cell_color(self, agg: dict, col: int) -> QColor | None:
        if col == S_COL_SYMBOL:
            return QColor(TEXT_BOLD)
        if col == S_COL_NET:
            net = agg.get("buys_qty", 0.0) - agg.get("sells_qty", 0.0)
            if net > 0:
                return QColor(CLR_BUY)
            if net < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        if col in (S_COL_NET_TOTAL, S_COL_NET_INCL_COMM):
            v = (self._net_total(agg) if col == S_COL_NET_TOTAL
                 else self._net_total(agg) - agg.get("commission", 0.0))
            if v > 0:
                return QColor(CLR_BUY)
            if v < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        if col == S_COL_REALIZED:
            pnl = agg.get("realized_pnl", 0.0)
            if pnl > 0:
                return QColor(CLR_BUY)
            if pnl < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        if col in (S_COL_EXCHANGES,):
            return QColor(TEXT_DIM)
        return QColor(TEXT)

    # ── Public mutation API ─────────────────────────────────────────────────

    def add_fill(self, fill: dict) -> None:
        """Fold a flattened fill into its symbol aggregate, or refresh in
        place if a prior fill with the same execId was already counted."""
        exec_id = fill.get("exec_id", "")
        symbol = fill.get("symbol", "") or "—"

        # Re-snapshot case: same execId arrived again.  Roll back the old
        # contribution and re-apply with the new data (commission may now
        # have arrived for the same execId).
        prev = self._fills.get(exec_id) if exec_id else None
        if prev is not None:
            prev_symbol = prev.get("symbol", "") or "—"
            self._subtract_from_agg(prev_symbol, prev)
            if self._agg.get(prev_symbol, {}).get("fill_count", 0) == 0:
                self._drop_symbol_row(prev_symbol)

        # Add to aggregate.  Insert a new row if this symbol is new.
        if symbol not in self._agg:
            new_row = len(self._order)
            self.beginInsertRows(QModelIndex(), new_row, new_row)
            self._agg[symbol] = self._empty_agg(symbol)
            self._order.append(symbol)
            self.endInsertRows()

        self._add_to_agg(symbol, fill)
        if exec_id:
            self._fills[exec_id] = fill

        # Emit dataChanged for the row so the view refreshes.
        if symbol in self._agg:
            row = self._order.index(symbol)
            tl = self.index(row, 0)
            br = self.index(row, _S_NUM_COLS - 1)
            self.dataChanged.emit(tl, br)

    def clear(self) -> None:
        if not self._order:
            return
        self.beginResetModel()
        self._agg.clear()
        self._order.clear()
        self._fills.clear()
        self.endResetModel()

    # ── Internal aggregation helpers ────────────────────────────────────────

    @staticmethod
    def _empty_agg(symbol: str) -> dict:
        return {
            "symbol": symbol,
            "buys_qty": 0.0,
            "sells_qty": 0.0,
            "buys_notional": 0.0,
            "sells_notional": 0.0,
            "commission": 0.0,
            "realized_pnl": 0.0,
            "has_any_realized": False,
            "exchanges": set(),
            "fill_count": 0,
        }

    def _add_to_agg(self, symbol: str, fill: dict) -> None:
        agg = self._agg[symbol]
        side = fill.get("side", "")
        shares = float(fill.get("shares", 0.0) or 0.0)
        price = float(fill.get("price", 0.0) or 0.0)
        if side == "BOT":
            agg["buys_qty"] += shares
            agg["buys_notional"] += shares * price
        elif side == "SLD":
            agg["sells_qty"] += shares
            agg["sells_notional"] += shares * price
        if fill.get("commission") is not None:
            agg["commission"] += float(fill["commission"])
        if fill.get("realized_pnl") is not None:
            agg["realized_pnl"] += float(fill["realized_pnl"])
            agg["has_any_realized"] = True
        exch = fill.get("exchange", "")
        if exch:
            agg["exchanges"].add(exch)
        agg["fill_count"] += 1

    def _subtract_from_agg(self, symbol: str, fill: dict) -> None:
        agg = self._agg.get(symbol)
        if agg is None:
            return
        side = fill.get("side", "")
        shares = float(fill.get("shares", 0.0) or 0.0)
        price = float(fill.get("price", 0.0) or 0.0)
        if side == "BOT":
            agg["buys_qty"] -= shares
            agg["buys_notional"] -= shares * price
        elif side == "SLD":
            agg["sells_qty"] -= shares
            agg["sells_notional"] -= shares * price
        if fill.get("commission") is not None:
            agg["commission"] -= float(fill["commission"])
        if fill.get("realized_pnl") is not None:
            agg["realized_pnl"] -= float(fill["realized_pnl"])
        agg["fill_count"] = max(0, agg["fill_count"] - 1)
        # Exchange membership isn't reversed — harmless; a symbol that once
        # routed through NYSE still shows NYSE in its exchange list.

    def _drop_symbol_row(self, symbol: str) -> None:
        if symbol not in self._order:
            return
        row = self._order.index(symbol)
        self.beginRemoveRows(QModelIndex(), row, row)
        self._order.pop(row)
        self._agg.pop(symbol, None)
        self.endRemoveRows()
