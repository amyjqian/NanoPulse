from __future__ import annotations

import logging
import textwrap
from typing import Any

from PySide6.QtCore import Qt, Signal, QSettings
from PySide6.QtGui import QColor, QFont, QFontMetrics
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
COL_CANCEL = 7
COL_TIME = 8
COL_LMT = 9
COL_STP = 10
COL_TIF = 11

_COLUMNS = [
    "Symbol", "Account", "Action", "Type", "Qty",
    "Fill", "Status", "", "Time", "Limit", "Stop", "TIF",
]

# ── Filter categories ────────────────────────────────────────────────────────
_FILTER_ALL = "All"
_FILTER_ACTIVE = "Active"       # Open + Completed combined (excludes Cancelled)
_FILTER_OPEN = "Open"
_FILTER_CANCELLED = "Cancelled"
_FILTER_COMPLETED = "Completed"
_FILTERS = [_FILTER_ALL, _FILTER_ACTIVE, _FILTER_OPEN, _FILTER_COMPLETED, _FILTER_CANCELLED]

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
    "Rejected":     _FILTER_CANCELLED,   # IB error 201 – order rejected by TWS/IB
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
        padding: 2px 2px;
        font-weight: 600;
        font-size: 11px;
    }}
    QTableWidget::item {{
        padding: 0px 2px;
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

        # Restore saved column widths, or fall back to QFontMetrics defaults
        self._restore_table_state()

        self._table.verticalHeader().setDefaultSectionSize(30)
        root.addWidget(self._table, stretch=1)

    # ── Column state persistence (QSettings) ──────────────────────────────────

    _SETTINGS_KEY = "OrderStatusPanel/headerState"

    def _restore_table_state(self) -> None:
        """Restore saved column widths from QSettings.

        If no saved state exists (first launch), fall back to the default
        QFontMetrics-based sizing.
        """
        settings = QSettings("NanoPulse", "Dashboard")
        state = settings.value(self._SETTINGS_KEY)
        if state is not None:
            if self._table.horizontalHeader().restoreState(state):
                log.debug("OrderStatus: restored header state from QSettings")
                return
        # First launch or corrupt state → use font-metrics defaults
        self._apply_font_metrics_widths()
        log.debug("OrderStatus: applied default QFontMetrics column widths")

    def save_table_state(self) -> None:
        """Persist the current column widths to QSettings."""
        settings = QSettings("NanoPulse", "Dashboard")
        state = self._table.horizontalHeader().saveState()
        settings.setValue(self._SETTINGS_KEY, state)
        log.debug("OrderStatus: saved header state to QSettings")

    # ── Dynamic column sizing ────────────────────────────────────────────────

    def _apply_font_metrics_widths(self) -> None:
        """Set each column width using QFontMetrics so sizes adapt to the
        table's current font rather than relying on hardcoded pixel values.
        """
        fm = QFontMetrics(self._table.font())
        pad = fm.horizontalAdvance("W")  # ~1 char extra breathing room

        col_samples = {
            COL_SYMBOL:  "WWWWWW",      # 6 letters
            COL_ACCOUNT: "WWWWWWWWW",   # 9 letters
            COL_ACTION:  "WWWWW",       # 5 letters
            COL_TYPE:    "WWWWW",       # 5 letters
            COL_QTY:     "9999",        # 4 digits
            COL_FILL:    "999999999",   # 9 digits
            COL_STATUS:  "WWWWWWWWWW",  # 10 letters
            COL_CANCEL:  "WWWWWWWW",    # 8 letters
            COL_TIME:    "00:00:00",    # 8 characters
            COL_LMT:     "999999999",   # 9 digits
            COL_STP:     "999999999",   # 9 digits
            COL_TIF:     "WWWW",        # 4 letters
        }

        for col, sample in col_samples.items():
            w = fm.horizontalAdvance(sample) + pad
            self._table.setColumnWidth(col, w)

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
            # UserRole stores the raw status string when the display text
            # has been decorated (e.g. "Inactive ⚠"). Fall back to .text().
            raw_status = status_item.data(Qt.ItemDataRole.UserRole)
            status = raw_status if isinstance(raw_status, str) else status_item.text()
            category = _STATUS_CATEGORY.get(status, _FILTER_OPEN)
            if self._active_filter == _FILTER_ALL:
                visible = True
            elif self._active_filter == _FILTER_ACTIVE:
                # Active = Open + Completed; hides cancelled/rejected noise
                visible = category in (_FILTER_OPEN, _FILTER_COMPLETED)
            else:
                visible = category == self._active_filter
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
        error_message = data.get("error_message", "")

        is_internal = _is_internal_order(order_ref)

        # Explicit (column_index, text) pairs – COL_CANCEL is a widget, not text.
        cell_values = [
            (COL_SYMBOL,  str(symbol)),
            (COL_ACCOUNT, str(account)),
            (COL_ACTION,  str(action)),
            (COL_TYPE,    str(order_type)),
            (COL_QTY,     str(quantity)),
            (COL_FILL,    str(fill_price)),
            (COL_STATUS,  str(status)),
            (COL_TIME,    str(time_str)),
            (COL_LMT,     str(limit_price)),
            (COL_STP,     str(stop_price)),
            (COL_TIF,     str(tif)),
        ]

        for col, text in cell_values:
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
                if error_message:
                    # Show ⚠ so the user knows there is more info on hover.
                    # Store the raw status in UserRole so _apply_row_visibility
                    # can still look it up in _STATUS_CATEGORY correctly.
                    item.setData(Qt.ItemDataRole.UserRole, text)
                    item.setText(f"{text} ⚠")
                    # Build a word-wrapped HTML tooltip (~60 chars per line)
                    lines: list[str] = []
                    for para in error_message.splitlines():
                        para = para.strip()
                        if para:
                            lines.extend(textwrap.wrap(para, width=60))
                    item.setToolTip("<html>" + "<br>".join(lines) + "</html>")

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
        """Place a Cancel button in the Cancel column (next to Status).

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

