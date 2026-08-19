"""
OrderStatusModel – QAbstractTableModel for the order blotter.

Design principles (mirrors AccountsTableModel)
───────────────────────────────────────────────
* **Composite key** ``order_id`` → row index for O(1) look-ups.
* **Sorted insertion** – newest orders (by ``time_sort``) appear at the top.
* **Surgical repaints** – only ``dataChanged`` is emitted per-cell;
  ``layoutChanged`` is never emitted during streaming updates.
* **Formatting** – action / status columns are colour-coded; external
  orders receive a muted background.
* **Cancel column** metadata is exposed via custom roles so a delegate
  can decide whether to paint a button.

OrderStatusFilterProxy – thin QSortFilterProxyModel that filters rows
by status category ("All", "Active", "Open", "Completed", "Cancelled").
"""
from __future__ import annotations

import logging
import textwrap
from bisect import bisect_right
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QColor

from src.ui.theme import Theme
from src.utils.helpers import format_option_symbol_two_line

from .table_theme import CELL_FONT_BOLD, header_alignment

log = logging.getLogger(__name__)

# ── Column indices ────────────────────────────────────────────────────────────
COL_SYMBOL = 0
COL_ACCOUNT = 1
COL_ACTION = 2
COL_TYPE = 3
COL_QTY = 4
COL_FILL = 5
COL_STATUS = 6
COL_CANCEL = 7
COL_TIME = 8
COL_LMT = 9
COL_STP = 10
COL_TIF = 11

HEADERS = [
    "Symbol", "Account", "Action", "Type", "Qty",
    "Fill", "Status", "", "Time", "Limit", "Stop", "TIF",
]

_NUM_COLS = len(HEADERS)

# Data-dict key → column index (COL_CANCEL has no data key)
_KEY_FOR_COL: dict[int, str] = {
    COL_SYMBOL:  "symbol",
    COL_ACCOUNT: "account",
    COL_ACTION:  "action",
    COL_TYPE:    "order_type",
    COL_QTY:     "quantity",
    COL_FILL:    "fill_price",
    COL_STATUS:  "status",
    COL_TIME:    "time",
    COL_LMT:     "limit_price",
    COL_STP:     "stop_price",
    COL_TIF:     "tif",
}

# ── Theme tokens ──────────────────────────────────────────────────────────────
# Shared with the Positions/Trades tables (src/ui/theme.py POS_*) so the
# three blotters read as one system.
CLR_BUY = Theme.POS_GAIN
CLR_SELL = Theme.POS_LOSS
CLR_OPEN = Theme.POS_ACCENT
CLR_FILLED = Theme.POS_GAIN
CLR_CANCELLED = Theme.POS_TEXT_MUTED
# Row tint marking an order this app didn't place (TWS / another client).
# Warm, to stay distinct from every cool tone in the row-state ladder.
CLR_EXTERNAL_BG = "#2a2018"
TEXT = Theme.POS_TEXT
TEXT_DIM = Theme.POS_TEXT_MUTED

# ── Filter categories ─────────────────────────────────────────────────────────
FILTER_ALL = "All"
FILTER_ACTIVE = "Active"
FILTER_OPEN = "Open"
FILTER_COMPLETED = "Completed"
FILTER_CANCELLED = "Cancelled"
FILTERS = [FILTER_ALL, FILTER_ACTIVE, FILTER_OPEN, FILTER_COMPLETED, FILTER_CANCELLED]

STATUS_CATEGORY: dict[str, str] = {
    "Submitted":    FILTER_OPEN,
    "PreSubmitted": FILTER_OPEN,
    "Open":         FILTER_OPEN,
    "PendingSubmit": FILTER_OPEN,
    "PendingCancel": FILTER_OPEN,
    "Filled":       FILTER_COMPLETED,
    "Completed":    FILTER_COMPLETED,
    "Cancelled":    FILTER_CANCELLED,
    "Canceled":     FILTER_CANCELLED,
    "Inactive":     FILTER_CANCELLED,
    # Terminal failures — nothing left to cancel, so they must NOT fall
    # into the FILTER_OPEN default (that's what paints the Cancel button
    # and lists them under Active/Open).
    "Rejected":        FILTER_CANCELLED,
    "ValidationError": FILTER_CANCELLED,
}

# ── Custom item-data roles ────────────────────────────────────────────────────
OrderIdRole = Qt.ItemDataRole.UserRole + 1
OrderRefRole = Qt.ItemDataRole.UserRole + 2
IsInternalRole = Qt.ItemDataRole.UserRole + 3
StatusCategoryRole = Qt.ItemDataRole.UserRole + 4
TimeSortRole = Qt.ItemDataRole.UserRole + 5
RawStatusRole = Qt.ItemDataRole.UserRole + 6

# ── Fonts (shared with the panel) ────────────────────────────────────────────
# Shared cell fonts — family and weight only; the size comes from the
# table's stylesheet, so all the blotters render at one size.
ACTION_FONT = CELL_FONT_BOLD
STATUS_FONT = CELL_FONT_BOLD
SYMBOL_FONT = CELL_FONT_BOLD

# Numeric columns read right, text columns left — same rule as the other
# blotters, so captions sit over their values.
_RIGHT_ALIGN_COLS = {COL_QTY, COL_FILL, COL_LMT, COL_STP}


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_internal_order(order_ref: str) -> bool:
    return bool(order_ref) and order_ref.startswith("nano_")


def status_colour(status: str) -> str:
    cat = STATUS_CATEGORY.get(status, FILTER_OPEN)
    if cat == FILTER_OPEN:
        return CLR_OPEN
    if cat == FILTER_COMPLETED:
        return CLR_FILLED
    return CLR_CANCELLED


# ══════════════════════════════════════════════════════════════════════════════
#  OrderStatusModel
# ══════════════════════════════════════════════════════════════════════════════

class OrderStatusModel(QAbstractTableModel):
    """Central model for the order blotter.

    Each row is stored as a plain ``dict`` with the keys accepted by
    ``add_order`` / ``update_order``.  The model exposes custom roles
    (``OrderIdRole``, ``IsInternalRole``, etc.) so delegates and proxy
    filters can query order metadata without reaching into internal storage.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # Orders oldest→newest INTERNALLY; the view presents them
        # reversed (view row r ↔ internal position len-1-r).  New orders
        # are the newest, so the hot path is a pure append and the map
        # below never shifts — the previous newest-first list rewrote
        # the whole map on every insert, O(N) per order all day.
        self._orders: list[dict] = []
        self._order_map: dict[str, int] = {}   # order_id → INTERNAL pos

    # ── Qt model interface ────────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._orders)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return _NUM_COLS

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation != Qt.Orientation.Horizontal \
                or not 0 <= section < _NUM_COLS:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return header_alignment(section, _RIGHT_ALIGN_COLS)
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ── data() ────────────────────────────────────────────────────────────────

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if row >= len(self._orders):
            return None

        order = self._orders[len(self._orders) - 1 - row]
        order_ref = order.get("order_ref", "")
        internal = is_internal_order(order_ref)
        status = order.get("status", "")
        error_msg = order.get("error_message", "")

        # ── DisplayRole ──────────────────────────────────────────────────
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_CANCEL:
                return None  # delegate handles this column
            if col == COL_STATUS and error_msg:
                return f"{status} ⚠"
            if col == COL_SYMBOL:
                # Options stack root+expiry over strike+right — the shared
                # delegate paints newline'd cells as two small-font lines.
                return format_option_symbol_two_line(order.get("symbol", "–"))
            key = _KEY_FOR_COL.get(col)
            return order.get(key, "–") if key else None

        # ── TextAlignmentRole ────────────────────────────────────────────
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in _RIGHT_ALIGN_COLS:
                return int(Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        # ── ForegroundRole ───────────────────────────────────────────────
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == COL_ACTION:
                action = order.get("action", "").upper()
                if action == "BUY":
                    return QColor(CLR_BUY)
                if action == "SELL":
                    return QColor(CLR_SELL)
            if col == COL_STATUS:
                return QColor(status_colour(status))
            if col == COL_TIME:
                return QColor(TEXT_DIM)
            return None

        # ── FontRole ─────────────────────────────────────────────────────
        if role == Qt.ItemDataRole.FontRole:
            if col == COL_ACTION:
                return ACTION_FONT
            if col == COL_STATUS:
                return STATUS_FONT
            if col == COL_SYMBOL:
                return SYMBOL_FONT
            return None

        # ── BackgroundRole ───────────────────────────────────────────────
        if role == Qt.ItemDataRole.BackgroundRole:
            if not internal:
                return QColor(CLR_EXTERNAL_BG)
            return None

        # ── ToolTipRole ──────────────────────────────────────────────────
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == COL_STATUS and error_msg:
                lines: list[str] = []
                for para in error_msg.splitlines():
                    para = para.strip()
                    if para:
                        lines.extend(textwrap.wrap(para, width=60))
                return "<html>" + "<br>".join(lines) + "</html>"
            return None

        # ── Custom roles ─────────────────────────────────────────────────
        if role == OrderIdRole:
            return order.get("order_id", "")
        if role == OrderRefRole:
            return order_ref
        if role == IsInternalRole:
            return internal
        if role == RawStatusRole:
            return status
        if role == StatusCategoryRole:
            return STATUS_CATEGORY.get(status, FILTER_OPEN)
        if role == TimeSortRole:
            return order.get("time_sort", "")

        return None

    # ── Public data API ───────────────────────────────────────────────────────

    def add_order(self, order_id: str, data: dict[str, Any]) -> None:
        """Insert a new order row or update an existing one.

        *data* keys (all optional, missing → ``"–"``):
            order_ref, symbol, account, action, order_type, quantity,
            limit_price, stop_price, tif, fill_price, status,
            time, time_sort, error_message
        """
        if order_id in self._order_map:
            self._update_existing(order_id, data)
            return
        self._insert_sorted(order_id, data)

    def update_order(self, order_id: str, data: dict[str, Any]) -> None:
        """Alias for ``add_order``."""
        self.add_order(order_id, data)

    def remove_order(self, order_id: str) -> None:
        """Remove a single order row."""
        pos = self._order_map.pop(order_id, None)
        if pos is None:
            return
        view_row = len(self._orders) - 1 - pos
        self.beginRemoveRows(QModelIndex(), view_row, view_row)
        self._orders.pop(pos)
        self.endRemoveRows()
        # Internal positions above the removed slot shifted down by one.
        for oid, p in self._order_map.items():
            if p > pos:
                self._order_map[oid] = p - 1
        log.debug("OrderStatusModel: removed %s (was view row %d)",
                  order_id, view_row)

    def clear(self) -> None:
        """Remove every row."""
        if not self._orders:
            return
        self.beginResetModel()
        self._orders.clear()
        self._order_map.clear()
        self.endResetModel()
        log.debug("OrderStatusModel: cleared all orders")

    def has_order(self, order_id: str) -> bool:
        """True if a row for *order_id* already exists."""
        return order_id in self._order_map

    def order_status(self, order_id: str) -> str:
        """Current status string for *order_id* ("" if unknown)."""
        pos = self._order_map.get(order_id)
        if pos is None:
            return ""
        return self._orders[pos].get("status", "")

    def order_at(self, row: int) -> dict | None:
        """Return the raw order dict at VIEW *row*, or None."""
        if 0 <= row < len(self._orders):
            return self._orders[len(self._orders) - 1 - row]
        return None

    def order_id_at(self, row: int) -> str:
        """Return order_id for *row*."""
        order = self.order_at(row)
        return order.get("order_id", "") if order else ""

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _insert_sorted(self, order_id: str, data: dict) -> None:
        """Insert so the VIEW stays descending by time_sort.  New orders
        are the newest ⇒ internal append, no map maintenance."""
        data = dict(data)  # shallow copy
        data["order_id"] = order_id
        new_sort = data.get("time_sort", "")

        pos = self._find_sorted_row(new_sort)
        n = len(self._orders)
        view_row = n - pos
        self.beginInsertRows(QModelIndex(), view_row, view_row)
        self._orders.insert(pos, data)
        if pos < n:
            # Rare non-newest insert: shift the entries above the slot.
            for oid, p in self._order_map.items():
                if p >= pos:
                    self._order_map[oid] = p + 1
        self._order_map[order_id] = pos
        self.endInsertRows()
        log.debug("OrderStatusModel: added %s at view row %d",
                  order_id, view_row)

    def _update_existing(self, order_id: str, data: dict) -> None:
        """Surgically update an existing row — only changed columns emit
        ``dataChanged``.
        """
        pos = self._order_map[order_id]
        order = self._orders[pos]
        row = len(self._orders) - 1 - pos   # view row for dataChanged

        changed_cols: list[int] = []
        for col, key in _KEY_FOR_COL.items():
            new_val = data.get(key)
            if new_val is not None and order.get(key) != new_val:
                order[key] = new_val
                changed_cols.append(col)

        # Also update non-column meta fields.  con_id/sec_type may be
        # absent or zero on the first (pre-check) emit and arrive with a
        # later status — only overwrite with truthy values.
        for meta_key in ("order_ref", "error_message", "time_sort", "order_id"):
            new_val = data.get(meta_key)
            if new_val is not None:
                order[meta_key] = new_val
        for meta_key in ("con_id", "sec_type"):
            new_val = data.get(meta_key)
            if new_val:
                order[meta_key] = new_val

        # Status change may affect the cancel column appearance
        if COL_STATUS in changed_cols:
            changed_cols.append(COL_CANCEL)

        if changed_cols:
            left = self.index(row, min(changed_cols))
            right = self.index(row, max(changed_cols))
            self.dataChanged.emit(left, right, [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.ForegroundRole,
                Qt.ItemDataRole.BackgroundRole,
                Qt.ItemDataRole.ToolTipRole,
                Qt.ItemDataRole.FontRole,
                IsInternalRole,
                StatusCategoryRole,
                RawStatusRole,
            ])
            log.debug("OrderStatusModel: updated %s at row %d (cols %s)",
                      order_id, row, changed_cols)

    def _find_sorted_row(self, new_sort: str) -> int:
        """INTERNAL position to insert at so the view stays descending.
        ``bisect_right`` lands an equal-timestamp order after existing
        equals internally — above them in the view, which is where the
        old descending scan (`new_sort >= existing → insert here`) put
        it.  A blank ``new_sort`` lands at internal 0 = the view's tail,
        also as before."""
        return bisect_right(self._orders, new_sort,
                            key=lambda o: o.get("time_sort", ""))


# ══════════════════════════════════════════════════════════════════════════════
#  OrderStatusFilterProxy
# ══════════════════════════════════════════════════════════════════════════════

class OrderStatusFilterProxy(QSortFilterProxyModel):
    """Thin proxy that filters rows by status category."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active_filter: str = FILTER_ALL

    @property
    def active_filter(self) -> str:
        return self._active_filter

    def set_filter(self, category: str) -> None:
        if self._active_filter == category:
            return
        self._active_filter = category
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if self._active_filter == FILTER_ALL:
            return True

        idx = self.sourceModel().index(source_row, COL_STATUS)
        cat = idx.data(StatusCategoryRole)
        if cat is None:
            return True

        if self._active_filter == FILTER_ACTIVE:
            return cat in (FILTER_OPEN, FILTER_COMPLETED)

        return cat == self._active_filter

