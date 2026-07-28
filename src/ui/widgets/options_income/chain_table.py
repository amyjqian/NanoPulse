"""
Options Income ticket — strike ladder (§4.4).

``ChainModel`` holds up to six OTM-biased strike rows with live-updating
delta / credit / yield / annualized figures; ``ChainView`` renders them
with the app's dark table styling and paints a left accent bar on the
selected row.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStyledItemDelegate,
    QTableView,
)

from src.ui.theme import Theme

ACCENT_BLUE = "#0071e3"

COL_STRIKE = 0
COL_DELTA = 1
COL_CREDIT = 2
COL_YIELD = 3
COL_ANN = 4

HEADERS = ["STRIKE", "DELTA", "CREDIT", "YIELD", "ANN."]
NUM_COLS = len(HEADERS)

_MONO = QFont("Menlo, Consolas, monospace", 11)
_GREEN = QColor("#3FB950")


class ChainModel(QAbstractTableModel):
    """Row dicts: strike, tag (OTM/ITM/ATM), conid, delta, credit,
    yield_pct, ann_pct — display-ready values computed by the ticket."""

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
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index: QModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        rd = self._rows[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_STRIKE:
                tag = rd.get("tag", "")
                return (f"{rd['strike']:g}  {tag}" if tag
                        else f"{rd['strike']:g}")
            if col == COL_DELTA:
                d = rd.get("delta", 0.0)
                return f"{abs(d):.2f}" if d else "—"
            if col == COL_CREDIT:
                c = rd.get("credit")
                return f"${c:,.0f}" if c else "—"
            if col == COL_YIELD:
                y = rd.get("yield_pct")
                return f"{y:.2f}%" if y else "—"
            if col == COL_ANN:
                a = rd.get("ann_pct")
                return f"{a:.1f}%" if a else "—"

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == COL_STRIKE:
                return int(Qt.AlignmentFlag.AlignLeft
                           | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignRight
                       | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col in (COL_YIELD, COL_ANN) and rd.get("yield_pct"):
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


class _AccentBarDelegate(QStyledItemDelegate):
    """Paints the 3-px left accent bar on the selected row's first cell."""

    def paint(self, painter: QPainter, option, index) -> None:
        super().paint(painter, option, index)
        view = self.parent()
        if (index.column() == COL_STRIKE and isinstance(view, QTableView)
                and view.currentIndex().row() == index.row()):
            painter.save()
            painter.fillRect(
                option.rect.x(), option.rect.y(),
                3, option.rect.height(), QColor(ACCENT_BLUE))
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
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_STRIKE, QHeaderView.ResizeMode.Stretch)
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

    @property
    def chain_model(self) -> ChainModel:
        return self._model

    def select_row(self, row: int) -> None:
        if 0 <= row < self._model.rowCount():
            self.selectRow(row)
