"""
Options Income ticket — strike ladder (§4.4).

``ChainModel`` holds an ATM-centred window of strike rows — OTM and ITM
blocks divided by a separator row at the spot price — with live-updating
delta / credit / yield figures; ``ChainView`` renders them
with the app's dark table styling and paints a left accent bar on the
selected row.
"""
from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStyledItemDelegate,
    QTableView,
)

from src.ui.theme import Theme
from src.core import config_manager

log = logging.getLogger(__name__)

ACCENT_BLUE = "#539BF5"

# User-dragged column widths persist across opens under this layout key
# (same per-user store as the ticket's window size).
_COL_LAYOUT_KEY = "options_chain_columns"

COL_STRIKE = 0
COL_BID = 1
COL_ASK = 2
COL_LAST = 3
COL_DELTA = 4
COL_CREDIT = 5
COL_YIELD = 6

HEADERS = ["STRIKE", "BID", "ASK", "LAST", "DELTA", "CREDIT", "YIELD"]
NUM_COLS = len(HEADERS)

_MONO = QFont("JetBrains Mono, Menlo, Consolas, monospace", 11)
_GREEN = QColor("#3FB950")

# ATM separator row: {"sep": True, "label": "831.92"} — painted by the
# delegate as a horizontal rule between the ITM and OTM sections.
SEP_ROLE = int(Qt.ItemDataRole.UserRole)
SEP_LABEL_ROLE = SEP_ROLE + 1
SEP_ROW_HEIGHT = 22


class ChainModel(QAbstractTableModel):
    """Row dicts: strike, conid, delta, credit, yield_pct —
    display-ready values computed by the ticket.  Rows arrive sorted
    descending by strike (highest on top).  A ``{"sep": True, "label"}``
    row marks the ITM/OTM boundary and is not selectable."""

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

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if (index.isValid() and index.row() < len(self._rows)
                and self._rows[index.row()].get("sep")):
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index: QModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        rd = self._rows[index.row()]
        col = index.column()

        if rd.get("sep"):
            if role == SEP_ROLE:
                return True
            if role == SEP_LABEL_ROLE:
                return rd.get("label", "")
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_STRIKE:
                return f"{rd['strike']:g}"
            if col in (COL_BID, COL_ASK, COL_LAST):
                key = {COL_BID: "bid", COL_ASK: "ask",
                       COL_LAST: "last"}[col]
                v = rd.get(key)
                # None = no data yet; 0.00 is a real quote (deep-OTM
                # strikes legitimately have a zero bid).
                return f"{v:,.2f}" if v is not None else "—"
            if col == COL_DELTA:
                d = rd.get("delta", 0.0)
                return f"{abs(d):.2f}" if d else "—"
            if col == COL_CREDIT:
                c = rd.get("credit")
                return f"${c:,.0f}" if c else "—"
            if col == COL_YIELD:
                y = rd.get("yield_pct")
                return f"{y:.2f}%" if y else "—"

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == COL_STRIKE:
                return int(Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignRight
                       | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == COL_YIELD and rd.get("yield_pct"):
                return _GREEN
            if col == COL_DELTA:
                return QColor(Theme.TEXT_DIM)
            return QColor(Theme.TEXT)

        if role == Qt.ItemDataRole.FontRole:
            return _MONO

        return None

    # ── API ─────────────────────────────────────────────────────────────────

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_data(self, row: int) -> dict | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def row_for_conid(self, conid: int) -> int:
        for i, rd in enumerate(self._rows):
            if rd.get("conid") == conid:
                return i
        return -1

    def update_row(self, row: int, patch: dict) -> None:
        if not (0 <= row < len(self._rows)):
            return
        self._rows[row].update(patch)
        self.dataChanged.emit(
            self.index(row, 0), self.index(row, NUM_COLS - 1))

    def set_sep_label(self, label: str) -> None:
        """Refresh the ATM separator's label (spot moves with quotes)."""
        for i, rd in enumerate(self._rows):
            if rd.get("sep"):
                if rd.get("label") != label:
                    rd["label"] = label
                    self.dataChanged.emit(
                        self.index(i, 0), self.index(i, NUM_COLS - 1))
                return


class _AccentBarDelegate(QStyledItemDelegate):
    """Paints the 3-px left accent bar on the selected row's first cell,
    and the ATM separator row (label + horizontal rule)."""

    def paint(self, painter: QPainter, option, index) -> None:
        if index.data(SEP_ROLE):
            self._paint_sep(painter, option, index)
            return
        super().paint(painter, option, index)
        view = self.parent()
        if (index.column() == COL_STRIKE and isinstance(view, QTableView)
                and view.currentIndex().row() == index.row()):
            painter.save()
            painter.fillRect(
                option.rect.x(), option.rect.y(),
                3, option.rect.height(), QColor(ACCENT_BLUE))
            painter.restore()

    @staticmethod
    def _paint_sep(painter: QPainter, option, index) -> None:
        painter.save()
        r = option.rect
        y = r.center().y()
        line = QColor(ACCENT_BLUE)
        line.setAlpha(110)
        painter.setPen(QPen(line, 1))
        if index.column() == COL_STRIKE:
            label = index.data(SEP_LABEL_ROLE) or ""
            if label:
                f = QFont(_MONO)
                f.setPointSize(10)
                f.setBold(True)
                painter.setFont(f)
                tw = painter.fontMetrics().horizontalAdvance(label)
                painter.setPen(QColor(ACCENT_BLUE))
                painter.drawText(
                    r.adjusted(8, 0, 0, 0),
                    int(Qt.AlignmentFlag.AlignLeft
                        | Qt.AlignmentFlag.AlignVCenter), label)
                painter.setPen(QPen(line, 1))
                painter.drawLine(r.x() + 8 + tw + 8, y, r.right(), y)
            else:
                painter.drawLine(r.x(), y, r.right(), y)
        else:
            painter.drawLine(r.x(), y, r.right(), y)
        painter.restore()


class ChainView(QTableView):
    """Strike ladder view — click a row to select the leg."""

    row_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = ChainModel(self)
        self.setModel(self._model)
        self.setItemDelegate(_AccentBarDelegate(self))
        self.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(34)
        hh = self.horizontalHeader()
        # Interactive = user-draggable column edges — every column,
        # including the last (a stretch section can't be dragged, which
        # made YIELD un-resizable once it became the last column).  The
        # first-open equal split in resizeEvent still fills the width.
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(False)
        hh.setMinimumSectionSize(48)
        hh.setHighlightSections(False)
        self.setStyleSheet(
            f"QTableView{{ background:{Theme.BG_MAIN}; color:{Theme.TEXT};"
            f" border:1px solid {Theme.BORDER}; border-radius:6px;"
            " selection-background-color:rgba(0,113,227,0.14);"
            f" selection-color:{Theme.TEXT}; }}"
            f"QHeaderView::section{{ background:{Theme.BG_MAIN};"
            f" color:{Theme.TEXT_DIM}; border:none;"
            f" border-bottom:1px solid {Theme.BORDER};"
            " padding:6px 8px; font-size:10px; font-weight:bold;"
            " letter-spacing:1px; }")
        sel_model = self.selectionModel()
        sel_model.currentRowChanged.connect(
            lambda cur, _prev: self.row_selected.emit(cur.row()))
        # Separator rows render as a thin rule, not a full-height row.
        self._model.modelReset.connect(self._apply_sep_heights)

        # ── Column-width persistence ────────────────────────────────────
        # Saved widths apply immediately; otherwise the first layout pass
        # equal-splits the viewport (the pre-Interactive look) and only
        # user drags after that are recorded (debounced — a drag emits
        # sectionResized per pixel).
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

    # ── Column widths ───────────────────────────────────────────────────────

    @staticmethod
    def _load_widths() -> list[int] | None:
        try:
            cols = config_manager.load_layout().get(_COL_LAYOUT_KEY) or []
            widths = [int(w) for w in cols]
            if len(widths) == NUM_COLS and all(w > 0 for w in widths):
                return widths
        except Exception:
            log.debug("ChainView: could not load column widths",
                      exc_info=True)
        return None

    def _apply_widths(self, widths: list[int]) -> None:
        self._applying_widths = True
        try:
            for col in range(NUM_COLS):
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
            log.debug("ChainView: could not save column widths",
                      exc_info=True)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        if not self._widths_ready and self.viewport().width() > 200:
            # First open with nothing saved — equal split across columns.
            self._apply_widths(
                [max(48, self.viewport().width() // NUM_COLS)] * NUM_COLS)
            self._widths_ready = True

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # Flush a pending debounced save so a drag right before closing
        # the ticket isn't lost.
        if self._save_debounce.isActive():
            self._save_debounce.stop()
            self._save_widths()
        super().hideEvent(event)

    def _apply_sep_heights(self) -> None:
        for i in range(self._model.rowCount()):
            if (self._model.row_data(i) or {}).get("sep"):
                self.setRowHeight(i, SEP_ROW_HEIGHT)

    @property
    def chain_model(self) -> ChainModel:
        return self._model

    def select_row(self, row: int) -> None:
        if not (0 <= row < self._model.rowCount()):
            return
        if (self._model.row_data(row) or {}).get("sep"):
            # Never land on the separator — take the nearest strike row.
            for alt in (row + 1, row - 1):
                if (0 <= alt < self._model.rowCount()
                        and not (self._model.row_data(alt) or {}).get("sep")):
                    row = alt
                    break
            else:
                return
        self.selectRow(row)
        # The ladder can hold ~100 strikes — keep the chosen leg (and
        # the ATM area around it) in view instead of the far-OTM top.
        self.scrollTo(self._model.index(row, COL_STRIKE),
                      QAbstractItemView.ScrollHint.PositionAtCenter)
