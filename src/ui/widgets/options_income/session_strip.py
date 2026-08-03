"""
Options Income ticket — the "This session" order table.

The ticket stays open after a submit so several orders can be laddered
back-to-back; outcomes must therefore be visible in the ticket itself
(the main window's Orders panel sits behind this modal dialog).  Every
order submitted from this window gets one row here — updated live from
the worker's ``order_status_updated`` stream, matched by ``orderRef`` —
with a clickable Cancel cell while the order is still open at TWS
(two-step: a first click arms "Confirm?", a second click within 3 s
sends the cancel — a stray click alone can never cancel an order).

Rendered as a table with the same dark styling as the strike ladder
(``chain_table.ChainView``).  Columns: TIME · ACTION · QTY · SYMBOL
(underlying stock) · STRIKE · TYPE (CALL/PUT) · PRICE · STATUS · CANCEL.

Pure view: the ticket owns the worker wiring and calls ``add_order`` /
``update_order``; Cancel clicks surface as ``cancel_requested(ref)``.
"""
from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView

from src.ui.theme import Theme
from src.core import config_manager
from src.ui.widgets.order_status_model import (
    FILTER_CANCELLED,
    FILTER_COMPLETED,
    FILTER_OPEN,
    STATUS_CATEGORY,
)

log = logging.getLogger(__name__)

# User-dragged column widths persist across opens under this layout key
# (same per-user store as the ladder's columns and the dividers).
_COL_LAYOUT_KEY = "options_session_columns"

COL_TIME = 0
COL_ACTION = 1
COL_QTY = 2
COL_SYMBOL = 3
COL_STRIKE = 4
COL_TYPE = 5
COL_PRICE = 6
COL_STATUS = 7
COL_CANCEL = 8

HEADERS = ["TIME", "ACTION", "QTY", "SYMBOL", "STRIKE", "TYPE", "PRICE",
           "STATUS", "CANCEL"]
NUM_COLS = len(HEADERS)

_MONO = QFont("JetBrains Mono, Menlo, Consolas, monospace", 11)
_TYPE_WORD = {"C": "CALL", "P": "PUT"}

_STATUS_COLOR = {
    FILTER_OPEN: "#539BF5",
    FILTER_COMPLETED: Theme.ACCENT,
    FILTER_CANCELLED: Theme.TEXT_DIM,
}
_CLR_REJECTED = "#F85149"


class _SessionModel(QAbstractTableModel):
    """Row dicts: ref, time, action, qty, symbol, strike, right, price,
    status, fill, error, cancel_clicked.  Newest order on top."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []

    # ── Qt interface ────────────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return NUM_COLS

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (role == Qt.ItemDataRole.DisplayRole
                and orientation == Qt.Orientation.Horizontal):
            return HEADERS[section]
        return None

    def data(self, index: QModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        rd = self._rows[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_TIME:
                return rd.get("time", "")
            if col == COL_ACTION:
                return rd.get("action", "")
            if col == COL_QTY:
                return str(rd.get("qty", ""))
            if col == COL_SYMBOL:
                return rd.get("symbol", "")
            if col == COL_STRIKE:
                return f"{rd.get('strike', 0):g}"
            if col == COL_TYPE:
                r = rd.get("right", "")
                return _TYPE_WORD.get(r, r)
            if col == COL_PRICE:
                return rd.get("price", "")
            if col == COL_STATUS:
                return self.status_text(rd)
            if col == COL_CANCEL:
                if self.can_cancel(rd):
                    return "Confirm?" if rd.get("confirm") else "Cancel"
                if self.cancelling(rd):
                    return "Cancelling…"
                return ""

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (COL_QTY, COL_STRIKE, COL_PRICE):
                return int(Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == COL_ACTION:
                return QColor(Theme.CLR_BUY if rd.get("action") == "BUY"
                              else Theme.CLR_SELL)
            if col == COL_STATUS:
                status = rd.get("status") or ""
                category = STATUS_CATEGORY.get(status)
                return QColor(_STATUS_COLOR.get(
                    category,
                    _CLR_REJECTED if status == "Rejected"
                    else Theme.TEXT_DIM))
            if col == COL_CANCEL:
                if not self.can_cancel(rd):
                    return QColor(Theme.TEXT_DIM)
                return QColor("#F85149" if rd.get("confirm")
                              else _CLR_REJECTED)
            return QColor(Theme.TEXT)

        if role == Qt.ItemDataRole.FontRole:
            if (col == COL_CANCEL and rd.get("confirm")
                    and self.can_cancel(rd)):
                f = QFont(_MONO)
                f.setBold(True)
                return f
            return _MONO

        if role == Qt.ItemDataRole.ToolTipRole and col == COL_STATUS:
            return rd.get("error") or None

        return None

    # ── Row state helpers ───────────────────────────────────────────────────

    @staticmethod
    def status_text(rd: dict) -> str:
        status = rd.get("status") or "Sending…"
        fill = rd.get("fill") or "–"
        if status == "Filled" and fill != "–":
            return f"Filled @ {fill}"
        return status

    @staticmethod
    def can_cancel(rd: dict) -> bool:
        status = rd.get("status") or ""
        return (STATUS_CATEGORY.get(status) == FILTER_OPEN
                and status != "PendingCancel"
                and not rd.get("cancel_clicked"))

    @staticmethod
    def cancelling(rd: dict) -> bool:
        return (rd.get("status") == "PendingCancel"
                or bool(rd.get("cancel_clicked")))

    # ── API ─────────────────────────────────────────────────────────────────

    def prepend(self, rd: dict) -> None:
        self.beginInsertRows(QModelIndex(), 0, 0)
        self._rows.insert(0, rd)
        self.endInsertRows()

    def row_data(self, row: int) -> dict | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def refresh(self, rd: dict) -> None:
        try:
            i = self._rows.index(rd)
        except ValueError:
            return
        self.dataChanged.emit(self.index(i, 0), self.index(i, NUM_COLS - 1))


class SessionOrdersStrip(QTableView):
    """The orders submitted from this ticket window, newest on top."""

    cancel_requested = Signal(str)           # orderRef

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = _SessionModel(self)
        self._rows: dict[str, dict] = {}     # ref → row dict (shared)
        self.setModel(self._model)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(26)
        hh = self.horizontalHeader()
        # Interactive = user-draggable column edges; the last column
        # absorbs the leftover so the table always fills its width.
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        hh.setMinimumSectionSize(40)
        hh.setHighlightSections(False)
        # No header row — the columns read fine from the values alone
        # (time · side · qty · symbol · strike · CALL/PUT · price …).
        # Resizing therefore happens by dragging column edges directly
        # on the rows (see mouse*Event below).
        hh.setVisible(False)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._drag_col = -1                  # column being edge-dragged
        self._drag_start_x = 0
        self._drag_start_w = 0
        # Same dark table styling as the strike ladder (ChainView);
        # no header rules — the header row is hidden.
        self.setStyleSheet(
            f"QTableView{{ background:{Theme.BG_MAIN}; color:{Theme.TEXT};"
            f" border:1px solid {Theme.BORDER}; border-radius:6px;"
            " selection-background-color:rgba(0,113,227,0.14);"
            f" selection-color:{Theme.TEXT}; }}"
            # Scrollbars — same slim dark style as the Orders view.
            f"QScrollBar:vertical{{ background:{Theme.BG_MAIN};"
            " width:8px; margin:0; }"
            "QScrollBar::handle:vertical{ background:#334058;"
            " border-radius:4px; min-height:24px; }"
            "QScrollBar::handle:vertical:pressed{ background:#6E7380; }"
            f"QScrollBar:horizontal{{ background:{Theme.BG_MAIN};"
            " height:10px; margin:0; }"
            "QScrollBar::handle:horizontal{ background:#334058;"
            " border-radius:4px; min-width:40px; }"
            "QScrollBar::handle:horizontal:pressed{ background:#6E7380; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            "{ height:0; }"
            "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal"
            "{ width:0; }")
        self.clicked.connect(self._on_clicked)

        # Misclick guard: the first click on a Cancel cell only ARMS it
        # ("Cancel" → "Confirm?"); the cancel is sent on a second click
        # within the confirm window, else the cell quietly reverts.
        self._armed_ref: str | None = None
        self._confirm_timer = QTimer(self)
        self._confirm_timer.setSingleShot(True)
        self._confirm_timer.setInterval(3000)
        self._confirm_timer.timeout.connect(self._disarm_confirm)

        # ── Column-width persistence ────────────────────────────────────
        # Saved widths apply immediately; otherwise the first layout pass
        # sizes columns to their content, and only user drags after that
        # are recorded (debounced — a drag emits sectionResized per px).
        # Initialised before hide() below — hide() delivers hideEvent.
        self._applying_widths = False
        self._widths_ready = False
        self._save_debounce = QTimer(self)
        self._save_debounce.setSingleShot(True)
        self._save_debounce.setInterval(400)
        self._save_debounce.timeout.connect(self._save_widths)
        saved = self._load_widths()
        if saved:
            self._apply_widths(saved)
            self._widths_ready = True
        hh.sectionResized.connect(self._on_section_resized)

        # Lives in the bottom pane of the rail column's splitter — fills
        # whatever height the user drags it to.
        self.setMinimumHeight(64)
        self.hide()                          # appears with the first order

    # ── Column widths ───────────────────────────────────────────────────────

    @staticmethod
    def _load_widths() -> list[int] | None:
        try:
            cols = config_manager.load_layout().get(_COL_LAYOUT_KEY) or []
            widths = [int(w) for w in cols]
            if len(widths) == NUM_COLS and all(w > 0 for w in widths):
                return widths
        except Exception:
            log.debug("SessionOrdersStrip: could not load column widths",
                      exc_info=True)
        return None

    def _apply_widths(self, widths: list[int]) -> None:
        self._applying_widths = True
        try:
            for col in range(NUM_COLS - 1):     # last column stretches
                self.setColumnWidth(col, widths[col])
        finally:
            self._applying_widths = False

    def _on_section_resized(self, *_args) -> None:
        if self._widths_ready and not self._applying_widths:
            self._save_debounce.start()

    def _save_widths(self) -> None:
        try:
            layout = config_manager.load_layout()
            layout[_COL_LAYOUT_KEY] = [self.columnWidth(c)
                                       for c in range(NUM_COLS)]
            config_manager.save_layout(layout)
        except Exception:
            log.debug("SessionOrdersStrip: could not save column widths",
                      exc_info=True)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        if not self._widths_ready and self.viewport().width() > 200:
            # First open with nothing saved — fit columns to content.
            self._applying_widths = True
            try:
                self.resizeColumnsToContents()
            finally:
                self._applying_widths = False
            self._widths_ready = True

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # Flush a pending debounced save so a drag right before closing
        # the ticket isn't lost.
        if self._save_debounce.isActive():
            self._save_debounce.stop()
            self._save_widths()
        super().hideEvent(event)

    # ── API (called by the ticket) ──────────────────────────────────────────

    def add_order(self, ref: str, fields: dict) -> None:
        """*fields*: time, action, qty, symbol, strike, right, price."""
        rd = dict(fields)
        rd.update({"ref": ref, "status": "", "fill": "–", "error": "",
                   "cancel_clicked": False})
        self._rows[ref] = rd
        self._model.prepend(rd)              # newest on top
        self.show()

    def update_order(self, ref: str, status: str, fill_price: str,
                     error: str) -> None:
        rd = self._rows.get(ref)
        if rd is None:
            return
        if ref == self._armed_ref:
            # The order moved on under an armed confirm — disarm rather
            # than let a stale "Confirm?" cancel a changed order.
            self._disarm_confirm()
        # Any fresh status settles a pending cancel click (either the
        # cancel confirmed, or the order moved on and can be re-tried).
        rd.update({"status": status, "fill": fill_price, "error": error,
                   "cancel_clicked": False})
        self._model.refresh(rd)

    def can_cancel(self, ref: str) -> bool:
        rd = self._rows.get(ref)
        return rd is not None and _SessionModel.can_cancel(rd)

    def status_text(self, ref: str) -> str:
        rd = self._rows.get(ref)
        return _SessionModel.status_text(rd) if rd else ""

    def request_cancel(self, ref: str) -> None:
        """Mark the row as cancelling (double-click guard) and notify
        the ticket; the Cancelled status event settles the final state."""
        rd = self._rows.get(ref)
        if rd is None or not _SessionModel.can_cancel(rd):
            return
        rd["cancel_clicked"] = True
        rd["confirm"] = False
        self._model.refresh(rd)
        self.cancel_requested.emit(ref)

    # ── Interaction ─────────────────────────────────────────────────────────

    def _on_clicked(self, index: QModelIndex) -> None:
        if index.column() != COL_CANCEL:
            # Clicking anywhere else stands down an armed confirm.
            self._disarm_confirm()
            return
        rd = self._model.row_data(index.row())
        if rd is None or not _SessionModel.can_cancel(rd):
            return
        if self._armed_ref == rd["ref"]:
            self._disarm_confirm()
            self.request_cancel(rd["ref"])
        else:
            self._arm_confirm(rd["ref"])

    def _arm_confirm(self, ref: str) -> None:
        self._disarm_confirm()               # only one row armed at once
        rd = self._rows.get(ref)
        if rd is None:
            return
        self._armed_ref = ref
        rd["confirm"] = True
        self._model.refresh(rd)
        self._confirm_timer.start()

    def _disarm_confirm(self) -> None:
        self._confirm_timer.stop()
        ref, self._armed_ref = self._armed_ref, None
        if ref:
            rd = self._rows.get(ref)
            if rd and rd.get("confirm"):
                rd["confirm"] = False
                self._model.refresh(rd)

    # Headerless column resizing: drag a column's right edge anywhere in
    # the rows.  setColumnWidth fires sectionResized, so the debounced
    # width persistence above records these drags too.

    _EDGE_PX = 5

    def _edge_col_at(self, x: int) -> int:
        """Column whose right edge is within ``_EDGE_PX`` of viewport
        *x* (last column excluded — it stretches)."""
        for col in range(NUM_COLS - 1):
            right = self.columnViewportPosition(col) + self.columnWidth(col)
            if abs(x - right) <= self._EDGE_PX:
                return col
        return -1

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        x = int(event.position().x())
        if event.button() == Qt.MouseButton.LeftButton:
            col = self._edge_col_at(x)
            if col >= 0:
                self._drag_col = col
                self._drag_start_x = x
                self._drag_start_w = self.columnWidth(col)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        x = int(event.position().x())
        if self._drag_col >= 0:
            w = max(40, self._drag_start_w + x - self._drag_start_x)
            self.setColumnWidth(self._drag_col, w)
            event.accept()
            return
        self.viewport().setCursor(
            Qt.CursorShape.SplitHCursor if self._edge_col_at(x) >= 0
            else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._drag_col >= 0:
            self._drag_col = -1
            event.accept()
            return
        super().mouseReleaseEvent(event)
