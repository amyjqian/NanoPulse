"""
TradesModel – flat execution log (QAbstractTableModel).

One row per Fill.  No parent-level aggregation; each child fill from a
multi-exchange order is a distinct chronological row.  New fills are
inserted at index 0.  Deduplication is by execId so the startup
re-snapshot after a reconnect is idempotent.
"""
from __future__ import annotations

import logging
from bisect import bisect_left
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from src.ui.theme import Theme
from src.utils.helpers import format_option_symbol_two_line

from .table_theme import (
    CELL_FONT, CELL_FONT_BOLD, GroupKeyRole, SortValueRole, header_alignment)

log = logging.getLogger(__name__)

# ── Custom roles ──────────────────────────────────────────────────────────────
# Re-exported: the view bands contiguous runs of equal key, so one
# submission reads as a single block.
__all_roles__ = (GroupKeyRole,)


def order_group_key(row: dict) -> str:
    """Identity of the *submission* a fill belongs to.

    One click in this app places an order per selected account, so a
    single action produces several orders — each with its own orderRef
    and account, all stamped with the same submission second.  Grouping
    per order would put every one of those on its own row-band, which is
    just stripes; grouping by the submission is what makes a batch read
    as one block.

    The stamp comes from the orderRef, whose format is
    ``nano_{YYMMDD}_{HHMMSS}_{tag}`` (``ib_worker._generate_order_ref``,
    legacy refs carry ``MMDD``): the first two fields are the submission
    time, the tag is per-order.  Fills with no nano ref — placed in TWS
    or by another client — fall back to their execution second, and a
    fill with neither becomes its own group rather than joining a bogus
    shared one.

    Instrument and side join the key because each worker stamps its own
    ref: the second is shared by everything submitted in that second, so
    two unrelated clicks landing together would otherwise merge.  A batch
    is one instrument on one side across accounts, so this never splits a
    real group — it only separates clicks that happened to coincide.
    """
    contract = row.get("local_symbol") or row.get("symbol") or ""
    side = row.get("side") or ""
    ref = row.get("order_ref") or ""
    if ref.startswith("nano_"):
        parts = ref.split("_")
        if len(parts) >= 3:
            return f"sub_{parts[1]}_{parts[2]}_{contract}_{side}"
    ts = row.get("time_sort") or ""
    if ts:
        # ISO stamp truncated to the second.
        return f"exec_{ts[:19]}_{contract}_{side}"
    return f"fill_{row.get('exec_id', '')}"


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
    "Time", "Symbol", "Type", "Side", "Qty", "Price", "Exch", "Ref",
    "Account", "Expiry", "Strike / right", "Comm", "R.P&L",
    "Order id", "Submitter", "Liquidation", "Last liquidity",
]

# Stable keys for each column — used by the preferences dialog for
# visibility / ordering state that survives header renames.
COL_KEYS: list[str] = [
    "time", "symbol", "sec_type", "side", "qty", "price", "exchange",
    "order_ref", "account", "expiry", "strike_right", "commission",
    "realized_pnl", "order_id", "submitter", "liquidation", "last_liquidity",
]

_NUM_COLS = len(HEADERS)

_RIGHT_ALIGN_COLS = {
    COL_QTY, COL_PRICE, COL_STRIKE_RIGHT,
    COL_COMM, COL_REAL_PNL, COL_ORDER_ID,
    COL_LAST_LIQ,
}

# ── Theme tokens ─────────────────────────────────────────────────────────────
# Shared with the Positions tables (src/ui/theme.py POS_*): the softer
# green/red is deliberate for dense grids, where full-saturation colour on
# hundreds of digits reads as alarm.
CLR_BUY    = Theme.POS_GAIN   # BOT – green
CLR_SELL   = Theme.POS_LOSS   # SLD – red
TEXT       = Theme.POS_TEXT
TEXT_DIM   = Theme.POS_TEXT_MUTED
TEXT_BOLD  = Theme.POS_TEXT


class TradesModel(QAbstractTableModel):
    """Flat execution log — one row per Fill, newest first."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # Fills oldest→newest INTERNALLY; the view presents them reversed
        # (view row r ↔ internal position len-1-r).  Live fills arrive in
        # chronological order, so the hot path is a pure append and the
        # exec-id index below never shifts — the previous newest-first
        # list re-shifted (and rebuilt the whole index dict) on every
        # insert, O(N²) over a session.
        self._rows: list[dict] = []
        # execId → INTERNAL position (for dedupe + surgical patches)
        self._index: dict[str, int] = {}

        # Shared cell fonts — same family, weight and size as the Positions
        # tables (family + weight only; size comes from the stylesheet).
        self._side_font = CELL_FONT_BOLD
        self._symbol_font = CELL_FONT_BOLD

    # ── Proxy helpers ───────────────────────────────────────────────────────

    def column_keys(self) -> list[str]:
        return list(COL_KEYS)

    def column_headers(self) -> list[str]:
        return list(HEADERS)

    def row_data(self, row: int) -> dict | None:
        """Return the raw row dict so ``TradesFilterProxy`` can filter on
        keys directly without string-scanning every display column.
        *row* is a VIEW row (newest first)."""
        if 0 <= row < len(self._rows):
            return self._rows[len(self._rows) - 1 - row]
        return None

    # ── Qt model plumbing ───────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else _NUM_COLS

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation != Qt.Orientation.Horizontal \
                or not 0 <= section < _NUM_COLS:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            # Captions sit over their values.
            return header_alignment(section, _RIGHT_ALIGN_COLS)
        return None

    def data(self, index: QModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row_idx = index.row()
        if row_idx < 0 or row_idx >= len(self._rows):
            return None
        row = self._rows[len(self._rows) - 1 - row_idx]
        col = index.column()

        if role == GroupKeyRole:
            gk = row.get("_group_key")
            return gk if gk is not None else order_group_key(row)
        if role == SortValueRole:
            return self._sort_value(row, col)
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
            # Same emphasis as the Positions table: the instrument and the
            # P&L are what you scan for, so those carry the weight.
            if col in (COL_SIDE, COL_SYMBOL, COL_REAL_PNL):
                return CELL_FONT_BOLD
            return CELL_FONT
        return None

    # ── Cell rendering ──────────────────────────────────────────────────────

    @staticmethod
    def _fmt_qty(v: float) -> str:
        if not v:
            return ""
        return f"{int(v)}" if float(v).is_integer() else f"{v:g}"

    @staticmethod
    def _sort_value(row: dict, col: int):
        """Raw sortable value for a cell — numeric columns return numbers
        so the sort proxy orders 9.99 < 10.00 instead of lexicographically.
        Types are consistent per column; missing values sort as 0 / ""."""
        if col == COL_TIME:
            return row.get("time_sort", "") or ""
        if col == COL_SYMBOL:
            return (row.get("local_symbol", "")
                    or row.get("symbol", "") or "").upper()
        if col == COL_SECTYPE:
            return row.get("sec_type", "") or ""
        if col == COL_SIDE:
            return row.get("side", "") or ""
        if col == COL_QTY:
            return float(row.get("shares", 0) or 0)
        if col == COL_PRICE:
            return float(row.get("price", 0.0) or 0.0)
        if col == COL_EXCH:
            return row.get("exchange", "") or ""
        if col == COL_REF:
            return row.get("order_ref", "") or ""
        if col == COL_ACCOUNT:
            return row.get("account", "") or ""
        if col == COL_EXPIRY:
            return row.get("expiry", "") or ""
        if col == COL_STRIKE_RIGHT:
            return float(row.get("strike", 0) or 0)
        if col == COL_COMM:
            return float(row.get("commission") or 0.0)
        if col == COL_REAL_PNL:
            return float(row.get("realized_pnl") or 0.0)
        if col == COL_ORDER_ID:
            return int(row.get("order_id", 0) or 0)
        if col == COL_SUBMITTER:
            return row.get("label", "") or ""
        if col == COL_LIQUIDATION:
            return 1 if row.get("liquidation") else 0
        if col == COL_LAST_LIQ:
            return int(row.get("last_liquidity", 0) or 0)
        return ""

    def _cell_text(self, row: dict, col: int) -> str:
        if col == COL_TIME:
            return row.get("time", "") or ""
        if col == COL_SYMBOL:
            # Prefer localSymbol ("NQM6") over root symbol ("NQ") so the
            # trades blotter matches the orders blotter and positions view.
            # Options stack root+expiry over strike+right — the shared
            # delegate paints newline'd cells as two small-font lines.
            return format_option_symbol_two_line(
                row.get("local_symbol", "") or row.get("symbol", "") or "")
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
            # TWS only sends a commissionReport for a subset of fills.
            # Render blank when the report isn't present; show the value
            # otherwise (including legitimate zeros).
            if c is None:
                return ""
            return f"{c:.4f}".rstrip("0").rstrip(".")
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
        """Insert a new fill into its time-sorted slot (newest first),
        or refresh in place if its execId is already present (dedupe
        across startup re-snapshots).

        Live ticks arrive in chronological order so the insertion
        point is always row 0 — no different from the earlier
        unconditional ``insert(0, …)``.  Snapshot replay after a
        reconnect can emit fills in worker-thread order rather than
        time order, though; inserting by ``time_sort`` ensures the
        flat view always reads newest-first regardless of arrival
        order."""
        exec_id = fill.get("exec_id", "")
        if "_group_key" not in fill:
            # Precompute the band key once — GroupBandDelegate asks for
            # it per painted cell.
            fill["_group_key"] = order_group_key(fill)

        if exec_id and exec_id in self._index:
            pos = self._index[exec_id]
            self._rows[pos] = fill
            view_row = len(self._rows) - 1 - pos
            top_left = self.index(view_row, 0)
            bottom_right = self.index(view_row, _NUM_COLS - 1)
            self.dataChanged.emit(top_left, bottom_right)
            return

        # Ascending-time insertion (internal order).  time_sort is an
        # ISO-style string, so lexicographic compare matches
        # chronological order.  bisect_left places an equal-timestamp
        # fill BEFORE existing equals internally — i.e. below them in
        # the newest-first view, exactly where the old descending scan
        # put it.  Blank time_sort sorts to the internal front = the
        # view's tail, also as before.  Live fills are newest ⇒ the
        # common case is a pure append and no index entry ever shifts.
        ts = fill.get("time_sort", "") or ""
        n = len(self._rows)
        pos = bisect_left(self._rows, ts,
                          key=lambda f: f.get("time_sort", "") or "")

        view_row = n - pos     # where the new fill appears in the view
        self.beginInsertRows(QModelIndex(), view_row, view_row)
        self._rows.insert(pos, fill)
        if pos < n:
            # Rare out-of-order insert (reconnect replay emits fills
            # pre-sorted, so live sessions never hit this): shift the
            # entries at or after the insertion point.
            for eid, p in self._index.items():
                if p >= pos:
                    self._index[eid] = p + 1
        if exec_id:
            self._index[exec_id] = pos
        self.endInsertRows()

    def clear(self) -> None:
        if not self._rows:
            return
        self.beginResetModel()
        self._rows.clear()
        self._index.clear()
        self.endResetModel()
