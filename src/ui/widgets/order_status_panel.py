from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

# ── Theme tokens (shared with connections_view / account_list_panel) ──────────
BG_PANEL = "#1e2230"
BG_TABLE = "#1A1D24"
BG_TABLE_ALT = "#1E222A"
BG_HEADER = "#14171D"
BORDER = "#30363D"
TEXT = "#C9D1D9"
TEXT_DIM = "#8B949E"

CLR_BUY = "#3FB950"
CLR_SELL = "#F85149"
CLR_OPEN = "#58A6FF"
CLR_FILLED = "#3FB950"
CLR_CANCELLED = "#8B949E"
CLR_CANCEL_BTN = "#3A1A1A"
CLR_CANCEL_BTN_BORDER = "#4A2A2A"
CLR_EXTERNAL_BG = "#2A2420"          # muted row background for external orders

# ── Fonts ─────────────────────────────────────────────────────────────────────
_TITLE_FONT = QFont("Helvetica Neue, Arial", 11)
_TITLE_FONT.setWeight(QFont.Weight.Bold)

_TABLE_FONT = QFont("Menlo, Consolas, monospace", 11)

# ── Column indices ────────────────────────────────────────────────────────────
COL_SYMBOL = 0
COL_ACCOUNT = 1
COL_ACTION = 2
COL_TYPE = 3
COL_QTY = 4
COL_FILL = 5
COL_STATUS = 6
COL_TIME = 7
COL_LMT = 8
COL_STP = 9
COL_TIF = 10
COL_CANCEL = 11

_COLUMNS = [
    "Symbol", "Account", "Action", "Type", "Qty",
    "Fill", "Status", "Time", "Limit", "Stop", "TIF", "",
]

# ── Filter categories ────────────────────────────────────────────────────────
_FILTER_ALL = "All"
_FILTER_OPEN = "Open"
_FILTER_CANCELLED = "Cancelled"
_FILTER_COMPLETED = "Completed"
_FILTERS = [_FILTER_ALL, _FILTER_OPEN, _FILTER_CANCELLED, _FILTER_COMPLETED]

# Map raw status strings to our categories
_STATUS_CATEGORY: dict[str, str] = {
    "Submitted":    _FILTER_OPEN,
    "PreSubmitted": _FILTER_OPEN,
    "Open":         _FILTER_OPEN,
    "Filled":       _FILTER_COMPLETED,
    "Completed":    _FILTER_COMPLETED,
    "Cancelled":    _FILTER_CANCELLED,
    "Canceled":     _FILTER_CANCELLED,
    "Inactive":     _FILTER_CANCELLED,
}

# ── Table QSS ────────────────────────────────────────────────────────────────
_TABLE_STYLE = f"""
    QTableWidget {{
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
        padding: 5px 8px;
        font-weight: 600;
        font-size: 11px;
    }}
    QTableWidget::item {{
        padding: 4px 8px;
    }}
    QScrollBar:vertical {{
        background: {BG_TABLE};
        width: 6px;
    }}
    QScrollBar::handle:vertical {{
        background: #3A3E48;
        border-radius: 3px;
        min-height: 20px;
    }}
    QScrollBar:horizontal {{
        background: {BG_TABLE};
        height: 6px;
    }}
    QScrollBar::handle:horizontal {{
        background: #3A3E48;
        border-radius: 3px;
        min-width: 20px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        height: 0; width: 0;
    }}
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_internal_order(order_ref: str) -> bool:
    """Return True if the order was placed by NanoPulse (orderRef starts with 'nano_')."""
    return bool(order_ref) and order_ref.startswith("nano_")


# ══════════════════════════════════════════════════════════════════════════════
#  OrderStatusPanel
# ══════════════════════════════════════════════════════════════════════════════

class OrderStatusPanel(QWidget):
    """Live order blotter with status filter tabs and per-row cancel buttons.

    Public API
    ----------
    add_order(order_id, data)   – insert or update a row
    update_order(order_id, data) – update an existing row (alias for add_order)
    remove_order(order_id)      – delete a row
    clear_orders()              – remove all rows

    Signals
    -------
    cancel_requested(str)       – emitted with the *order_id* when the user
                                  clicks a Cancel button.
    """

    cancel_requested = Signal(str)   # order_id
    refresh_requested = Signal()     # user clicked Refresh

    def __init__(self, parent=None):
        super().__init__(parent)
        self._order_rows: dict[str, int] = {}   # order_id → table row index
        self._active_filter: str = _FILTER_ALL
        self._setup_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setObjectName("OrderStatusPanel")
        self.setStyleSheet(f"""
            QWidget#OrderStatusPanel {{
                background: {BG_PANEL};
                border: 1px solid {BORDER};
                border-radius: 4px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # ── Header row: title + filter buttons ────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(0)

        title = QLabel("Order Status")
        title.setFont(_TITLE_FONT)
        title.setStyleSheet(f"color: #ffffff; border: none;")
        header.addWidget(title)

        # Refresh button
        refresh_btn = QPushButton("⟳")
        refresh_btn.setToolTip("Refresh orders from TWS")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setFixedSize(26, 26)
        refresh_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {TEXT_DIM};
                border: 1px solid {BORDER};
                border-radius: 4px;
                font-size: 14px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                color: #ffffff;
                border-color: {CLR_OPEN};
                background: #253545;
            }}
            QPushButton:pressed {{
                background: #1a2535;
            }}
        """)
        refresh_btn.clicked.connect(self._on_refresh_clicked)
        header.addWidget(refresh_btn)

        header.addStretch(1)

        self._filter_btns: dict[str, QPushButton] = {}
        for label in _FILTERS:
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setCheckable(True)
            btn.setChecked(label == _FILTER_ALL)
            btn.clicked.connect(lambda checked, lbl=label: self._on_filter_clicked(lbl))
            self._filter_btns[label] = btn
            header.addWidget(btn)

        self._apply_filter_styles()
        root.addLayout(header)

        # ── Order table ───────────────────────────────────────────────────────
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(True)
        self._table.setFont(_TABLE_FONT)
        self._table.setStyleSheet(_TABLE_STYLE)

        hdr = self._table.horizontalHeader()
        hdr.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hdr.setStretchLastSection(True)
        # All columns are user-resizable via drag
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # Sensible default widths
        _default_widths = {
            COL_SYMBOL: 80, COL_ACCOUNT: 100, COL_ACTION: 55,
            COL_TYPE: 55,   COL_QTY: 45,     COL_FILL: 60,
            COL_STATUS: 80, COL_TIME: 70,    COL_LMT: 60,
            COL_STP: 60,    COL_TIF: 45,     COL_CANCEL: 64,
        }
        for col, w in _default_widths.items():
            self._table.setColumnWidth(col, w)

        self._table.verticalHeader().setDefaultSectionSize(30)
        root.addWidget(self._table, stretch=1)

    # ── Filter bar ────────────────────────────────────────────────────────────

    def _apply_filter_styles(self) -> None:
        """Re-style all filter buttons: active pill vs muted text."""
        for label, btn in self._filter_btns.items():
            if label == self._active_filter:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #253545;
                        color: #ffffff;
                        border: 1px solid {CLR_OPEN};
                        border-radius: 10px;
                        padding: 3px 12px;
                        font-size: 11px;
                        font-weight: 600;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: transparent;
                        color: {TEXT_DIM};
                        border: none;
                        border-radius: 10px;
                        padding: 3px 12px;
                        font-size: 11px;
                        font-weight: 600;
                    }}
                    QPushButton:hover {{ color: #ffffff; }}
                """)

    def _on_filter_clicked(self, label: str) -> None:
        self._active_filter = label
        # Update checked state
        for lbl, btn in self._filter_btns.items():
            btn.setChecked(lbl == label)
        self._apply_filter_styles()
        self._apply_row_visibility()

    def _apply_row_visibility(self) -> None:
        """Show / hide rows based on the active filter."""
        for row in range(self._table.rowCount()):
            status_item = self._table.item(row, COL_STATUS)
            if status_item is None:
                continue
            status = status_item.text()
            category = _STATUS_CATEGORY.get(status, _FILTER_OPEN)
            visible = (self._active_filter == _FILTER_ALL
                       or category == self._active_filter)
            self._table.setRowHidden(row, not visible)

    # ── Public data API ───────────────────────────────────────────────────────

    def add_order(self, order_id: str, data: dict[str, Any]) -> None:
        """Insert a new order row or update an existing one.

        *data* keys (all optional, missing → ``"–"``):
            symbol, account, action, order_type, quantity,
            limit_price, stop_price, tif, fill_price, status,
            time, time_sort
        """
        if order_id in self._order_rows:
            self._update_existing_row(order_id, data)
            return

        # Find the correct insertion point to keep descending time_sort order.
        new_sort = data.get("time_sort", "")
        insert_row = self._find_sorted_row(new_sort)

        self._table.insertRow(insert_row)
        # Shift all existing row indices at or below insert_row
        for oid in self._order_rows:
            if self._order_rows[oid] >= insert_row:
                self._order_rows[oid] += 1
        self._order_rows[order_id] = insert_row

        self._populate_row(insert_row, order_id, data)
        self._apply_row_visibility()
        log.debug("OrderStatus: added order %s at row %d", order_id, insert_row)

    def _find_sorted_row(self, new_sort: str) -> int:
        """Return the row index at which to insert so that time_sort is descending."""
        for row in range(self._table.rowCount()):
            item = self._table.item(row, COL_TIME)
            if item is None:
                continue
            existing_sort = item.data(Qt.ItemDataRole.UserRole) or ""
            if new_sort >= existing_sort:
                return row
        return self._table.rowCount()

    def update_order(self, order_id: str, data: dict[str, Any]) -> None:
        """Alias for ``add_order`` – updates if exists, inserts otherwise."""
        self.add_order(order_id, data)

    def remove_order(self, order_id: str) -> None:
        """Remove a single order row."""
        row = self._order_rows.pop(order_id, None)
        if row is None:
            return
        self._table.removeRow(row)
        # Shift indices above the removed row
        for oid, r in self._order_rows.items():
            if r > row:
                self._order_rows[oid] = r - 1
        log.debug("OrderStatus: removed order %s", order_id)

    def clear_orders(self) -> None:
        """Remove every row."""
        self._table.setRowCount(0)
        self._order_rows.clear()
        log.debug("OrderStatus: cleared all orders")

    # ── Row helpers ───────────────────────────────────────────────────────────

    def _populate_row(self, row: int, order_id: str, data: dict[str, Any]) -> None:
        """Fill every cell in *row* from *data* and embed a Cancel button."""
        dash = "–"
        symbol = data.get("symbol", dash)
        account = data.get("account", dash)
        action = data.get("action", dash)
        order_type = data.get("order_type", dash)
        quantity = data.get("quantity", dash)
        limit_price = data.get("limit_price", dash)
        stop_price = data.get("stop_price", dash)
        tif = data.get("tif", dash)
        fill_price = data.get("fill_price", dash)
        status = data.get("status", "Open")
        time_str = data.get("time", dash)
        time_sort = data.get("time_sort", "")
        order_ref = data.get("order_ref", "")

        is_internal = _is_internal_order(order_ref)

        values = [
            str(symbol), str(account), str(action), str(order_type),
            str(quantity), str(fill_price), str(status), str(time_str),
            str(limit_price), str(stop_price), str(tif),
        ]

        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            # Store sortable timestamp on the Time column
            if col == COL_TIME:
                item.setData(Qt.ItemDataRole.UserRole, time_sort)
                item.setForeground(QColor(TEXT_DIM))
            # Colour-code Action and Status
            elif col == COL_ACTION:
                item.setForeground(QColor(
                    CLR_BUY if text.upper() == "BUY" else
                    CLR_SELL if text.upper() == "SELL" else TEXT
                ))
                item.setFont(QFont("Helvetica Neue, Arial", 11, QFont.Weight.Bold))
            elif col == COL_STATUS:
                item.setForeground(QColor(self._status_colour(text)))
                item.setFont(QFont("Helvetica Neue, Arial", 11, QFont.Weight.Bold))

            # External order → muted background on every cell
            if not is_internal:
                item.setBackground(QColor(CLR_EXTERNAL_BG))

            self._table.setItem(row, col, item)

        # Cancel button – only for internal open orders
        self._embed_cancel_button(row, order_id, status, is_internal)

    def _update_existing_row(self, order_id: str, data: dict[str, Any]) -> None:
        """Overwrite cell contents for an already-existing order row."""
        row = self._order_rows[order_id]
        self._populate_row(row, order_id, data)
        self._apply_row_visibility()
        log.debug("OrderStatus: updated order %s at row %d", order_id, row)

    def _embed_cancel_button(self, row: int, order_id: str, status: str,
                             is_internal: bool = True) -> None:
        """Place a Cancel button in the last column.

        The button is only visible and enabled for **internal** orders
        (orderRef starts with ``nano_``) that are in an open state.
        External orders never get a Cancel button.
        """
        category = _STATUS_CATEGORY.get(status, _FILTER_OPEN)
        if is_internal and category == _FILTER_OPEN:
            btn = QPushButton("Cancel")
            btn.setFixedHeight(24)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {CLR_CANCEL_BTN};
                    color: {CLR_SELL};
                    border: 1px solid {CLR_CANCEL_BTN_BORDER};
                    border-radius: 3px;
                    padding: 2px 8px;
                    font-size: 10px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background: #4A2424;
                    border-color: {CLR_SELL};
                }}
                QPushButton:pressed {{
                    background: #5A2A2A;
                }}
            """)
            btn.clicked.connect(lambda _, oid=order_id: self._on_cancel_clicked(oid))
            self._table.setCellWidget(row, COL_CANCEL, btn)
        else:
            # Remove any existing button for non-open or external orders
            self._table.removeCellWidget(row, COL_CANCEL)
            placeholder = QTableWidgetItem("")
            if not is_internal:
                placeholder.setBackground(QColor(CLR_EXTERNAL_BG))
            self._table.setItem(row, COL_CANCEL, placeholder)

    def _on_cancel_clicked(self, order_id: str) -> None:
        log.info("OrderStatus: cancel requested for order %s", order_id)
        self.cancel_requested.emit(order_id)

    def _on_refresh_clicked(self) -> None:
        """Clear all rows and ask the workers to re-snapshot from TWS."""
        log.info("OrderStatus: refresh requested by user")
        self.clear_orders()
        self.refresh_requested.emit()

    @staticmethod
    def _status_colour(status: str) -> str:
        """Return hex colour for a given status string."""
        cat = _STATUS_CATEGORY.get(status, _FILTER_OPEN)
        if cat == _FILTER_OPEN:
            return CLR_OPEN
        if cat == _FILTER_COMPLETED:
            return CLR_FILLED
        return CLR_CANCELLED

