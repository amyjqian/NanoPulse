"""
Strategy Tier Editor – flat spreadsheet view backed by an SQLite
``strategy_tiers`` table.

Database schema (normalised, 5-column):
    CREATE TABLE IF NOT EXISTS strategy_tiers (
        symbol     TEXT,
        order_type TEXT,   -- "take_profit" | "stop_loss" | "stop" | "trailing"
        tier_level INTEGER, -- 1..4
        offset     REAL,
        value      REAL,
        PRIMARY KEY (symbol, order_type, tier_level)
    );

The custom QAbstractTableModel pivots those rows into a 33-column wide
spreadsheet:
    Col 0          : Symbol
    Col 1-8  (TP)  : take_profit  T1-T4  Off / Val
    Col 9-16 (SL)  : stop_loss    T1-T4  Off / Val
    Col 17-24 (ES) : stop         T1-T4  Off / Val
    Col 25-32 (TS) : trailing     T1-T4  Off / Val
"""
from __future__ import annotations

import copy
import logging
import os
import sqlite3
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.ui.theme import Theme

log = logging.getLogger(__name__)

# Database lives at  <project-root>/data/strategy_tiers.db
_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "strategy_tiers.db"

# ── Palette ───────────────────────────────────────────────────────────────────
BG_PRIMARY   = "#151922"
BG_SECONDARY = "#1B2130"
BG_HEADER    = "#151922"
BORDER       = "#262E3F"
TEXT         = "#E6EBF2"
TEXT_DIM     = "#A3AFC2"

BLUE   = "#539BF5"
GREEN  = Theme.ACCENT
ORANGE = "#F2A13A"
RED    = "#F85149"
PURPLE = "#BC8CFF"
COMPUTED = "#4ECDC4"   # teal – read-only computed Val cells

# Per-section cell / header background tints
_CELL_BG = {
    "take_profit": "#1A221A",
    "stop_loss":   "#221A1A",
    "stop":        "#1A1A25",
    "trailing":    "#1A2222",
}
_HDR_BG = {
    "take_profit": "#1F3D1F",
    "stop_loss":   "#3D1F1F",
    "stop":        "#1F1F3D",
    "trailing":    "#1F3D3D",
}
_LEGEND = {
    "TP = Take Profit": GREEN,
    "SL = Stop Loss":   RED,
    "ES = Entry Stop":  BLUE,
    "TS = Trailing":    PURPLE,
}

# ── Schema constants ──────────────────────────────────────────────────────────
_ORDER_TYPES = ["take_profit", "stop_loss", "stop", "trailing"]
_PREFIXES    = ["TP", "SL", "ES", "TS"]

# Build column-key list  (col 0 → None, cols 1..32 → (order_type, tier, field))
_COL_KEYS: list[tuple | None] = [None]
for _ot in _ORDER_TYPES:
    for _tier in range(1, 5):
        _COL_KEYS.append((_ot, _tier, "offset"))
        _COL_KEYS.append((_ot, _tier, "value"))

# Build display headers
_HEADERS: list[str] = ["Symbol"]
for _pfx in _PREFIXES:
    for _t in range(1, 5):
        _HEADERS.append(f"{_pfx} T{_t} Off")
        _HEADERS.append(f"{_pfx} T{_t} Val")

_NCOLS = len(_HEADERS)  # 33


# ── Model ─────────────────────────────────────────────────────────────────────

class StrategyPivotModel(QAbstractTableModel):
    """
    Pivots the normalised SQLite rows into a 33-column wide in-memory table.

    Internal state:
        _symbols : list[str]           – display order
        _data    : dict[str, dict]     – symbol → {(order_type, tier, field): float}
    """

    # ------------------------------------------------------------------
    def __init__(self, parent=None):
        super().__init__(parent)
        self._symbols: list[str] = []
        self._data:   dict[str, dict] = {}
        # symbol → float multiplier, loaded from saved contracts
        self._multipliers: dict[str, float] = {}

    # ── Multiplier helpers ────────────────────────────────────────────
    def set_multipliers(self, mults: dict[str, float]) -> None:
        """Replace the multiplier lookup and refresh every Val column in all views."""
        self._multipliers = dict(mults)
        if self._symbols:
            # All val columns live at even offsets starting from col 2
            tl = self.index(0, 2)
            br = self.index(self.rowCount() - 1, _NCOLS - 1)
            self.dataChanged.emit(tl, br, [Qt.ItemDataRole.DisplayRole,
                                           Qt.ItemDataRole.ForegroundRole])

    def _multiplier(self, symbol: str) -> float:
        """Return the float multiplier for *symbol*, falling back to 1.0."""
        return self._multipliers.get(symbol, 1.0)

    # ------------------------------------------------------------------
    def _empty_row(self) -> dict:
        return {
            (ot, tier, field): 0.0
            for ot in _ORDER_TYPES
            for tier in range(1, 5)
            for field in ("offset", "value")
        }

    # ── QAbstractTableModel interface ─────────────────────────────────
    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._symbols)

    def columnCount(self, parent=QModelIndex()) -> int:
        return _NCOLS

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return _HEADERS[section]
            if role == Qt.ItemDataRole.ToolTipRole:
                if section > 0 and _COL_KEYS[section][2] == "value":
                    return "Auto-calculated: Offset × Multiplier  (read-only)"
                if section > 0 and _COL_KEYS[section][2] == "offset":
                    return "Enter tick/price offset"
            if role == Qt.ItemDataRole.BackgroundRole:
                if section == 0:
                    return QColor(BG_HEADER)
                key = _COL_KEYS[section]
                return QColor(_HDR_BG[key[0]])
            if role == Qt.ItemDataRole.ForegroundRole:
                if section == 0:
                    return QColor(TEXT_DIM)
                key = _COL_KEYS[section]
                if key[2] == "value":
                    return QColor(COMPUTED)
                pfx_idx = _ORDER_TYPES.index(key[0])
                return QColor([GREEN, RED, BLUE, PURPLE][pfx_idx])
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        symbol = self._symbols[row]
        key = _COL_KEYS[col] if col > 0 else None

        # ── Display / Edit ────────────────────────────────────────────
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == 0:
                return symbol

            if key[2] == "offset":
                val = self._data[symbol].get(key, 0.0)
                if role == Qt.ItemDataRole.EditRole:
                    return float(val)
                if val == 0.0:
                    return ""
                return f"{val:.4f}".rstrip("0").rstrip(".")

            # Val column – always computed (multiplier falls back to 1.0)
            if role == Qt.ItemDataRole.EditRole:
                return None   # not editable
            offset_key = (key[0], key[1], "offset")
            offset = self._data[symbol].get(offset_key, 0.0)
            mult = self._multiplier(symbol)
            val = offset * mult
            if val == 0.0:
                return ""
            return f"{val:,.2f}"

        # ── Alignment ────────────────────────────────────────────────
        if role == Qt.ItemDataRole.TextAlignmentRole:
            align = (Qt.AlignmentFlag.AlignLeft if col == 0
                     else Qt.AlignmentFlag.AlignRight)
            return int(align | Qt.AlignmentFlag.AlignVCenter)

        # ── Foreground colour ────────────────────────────────────────
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == 0:
                return QColor(TEXT)
            if key[2] == "value":
                # teal = real multiplier; amber = fallback 1.0
                known = symbol in self._multipliers
                return QColor(COMPUTED if known else ORANGE)
            return QColor(ORANGE)

        # ── Background ───────────────────────────────────────────────
        if role == Qt.ItemDataRole.BackgroundRole:
            if col == 0:
                return QColor(BG_PRIMARY)
            return QColor(_CELL_BG[key[0]])

        # ── Tooltip ──────────────────────────────────────────────────
        if role == Qt.ItemDataRole.ToolTipRole:
            if col > 0 and key[2] == "value":
                offset_key = (key[0], key[1], "offset")
                offset = self._data[symbol].get(offset_key, 0.0)
                if symbol in self._multipliers:
                    mult = self._multipliers[symbol]
                    return (f"Offset {offset} × Multiplier {mult:g} "
                            f"= {offset * mult:,.2f}")
                return (f'No contract found for "{symbol}" – '
                        f'using fallback multiplier 1.0\n'
                        f'Offset {offset} × 1.0 = {offset:,.2f}')
            return None

        return None

    def setData(self, index: QModelIndex, value,
                role=Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.EditRole:
            return False
        row, col = index.row(), index.column()
        if col == 0:
            return False
        key = _COL_KEYS[col]
        if key[2] == "value":
            return False   # computed – never editable
        symbol = self._symbols[row]
        try:
            self._data[symbol][key] = float(value)
            self.dataChanged.emit(index, index, [role])
            # Notify the adjacent Val column so it recomputes
            val_idx = self.index(row, col + 1)
            self.dataChanged.emit(val_idx, val_idx,
                                  [Qt.ItemDataRole.DisplayRole,
                                   Qt.ItemDataRole.ForegroundRole,
                                   Qt.ItemDataRole.ToolTipRole])
            return True
        except (ValueError, TypeError):
            return False

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        f = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        col = index.column()
        if col > 0 and _COL_KEYS[col][2] == "offset":
            f |= Qt.ItemFlag.ItemIsEditable   # only Offset cells are editable
        return f

    # ── Database I/O ──────────────────────────────────────────────────

    @staticmethod
    def _ensure_table(conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_tiers (
                symbol     TEXT,
                order_type TEXT,
                tier_level INTEGER,
                offset     REAL,
                value      REAL,
                PRIMARY KEY (symbol, order_type, tier_level)
            )
        """)
        conn.commit()

    def load_from_db(self) -> None:
        """Initialise table if absent, then populate the model from disk."""
        os.makedirs(_DB_PATH.parent, exist_ok=True)
        conn = sqlite3.connect(_DB_PATH)
        try:
            self._ensure_table(conn)
            self.beginResetModel()
            self._symbols = []
            self._data    = {}
            cursor = conn.execute(
                "SELECT symbol, order_type, tier_level, offset, value "
                "FROM strategy_tiers ORDER BY symbol"
            )
            for sym, ot, tier, offset, val in cursor:
                if sym not in self._data:
                    self._symbols.append(sym)
                    self._data[sym] = self._empty_row()
                if ot in _ORDER_TYPES and 1 <= tier <= 4:
                    self._data[sym][(ot, tier, "offset")] = float(offset or 0)
                    self._data[sym][(ot, tier, "value")]  = float(val or 0)
            self.endResetModel()
            log.info("StrategyEditor: loaded %d symbol(s) from %s",
                     len(self._symbols), _DB_PATH)
        finally:
            conn.close()

    def save_to_db(self) -> None:
        """Persist all in-memory rows to the SQLite database."""
        os.makedirs(_DB_PATH.parent, exist_ok=True)
        conn = sqlite3.connect(_DB_PATH)
        try:
            self._ensure_table(conn)
            for sym in self._symbols:
                d = self._data[sym]
                mult = self._multiplier(sym)
                for ot in _ORDER_TYPES:
                    for tier in range(1, 5):
                        offset = d.get((ot, tier, "offset"), 0.0)
                        computed_val = offset * mult   # mult >= 1.0 always
                        conn.execute(
                            "INSERT OR REPLACE INTO strategy_tiers "
                            "(symbol, order_type, tier_level, offset, value) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (sym, ot, tier, offset, computed_val),
                        )
            conn.commit()
            log.info("StrategyEditor: saved %d symbol(s) to %s",
                     len(self._symbols), _DB_PATH)
        finally:
            conn.close()

    # ── Mutation helpers ──────────────────────────────────────────────

    def add_symbol(self, symbol: str) -> bool:
        symbol = symbol.strip().upper()
        if not symbol or symbol in self._data:
            return False
        pos = len(self._symbols)
        self.beginInsertRows(QModelIndex(), pos, pos)
        self._symbols.append(symbol)
        self._data[symbol] = self._empty_row()
        self.endInsertRows()
        return True

    def delete_symbol_at(self, row: int) -> bool:
        if not (0 <= row < len(self._symbols)):
            return False
        sym = self._symbols[row]
        self.beginRemoveRows(QModelIndex(), row, row)
        self._symbols.pop(row)
        del self._data[sym]
        self.endRemoveRows()
        return True

    def symbol_at(self, row: int) -> str | None:
        return self._symbols[row] if 0 <= row < len(self._symbols) else None

    # ── Clipboard helpers ─────────────────────────────────────────────

    def copy_row_data(self, row: int) -> dict | None:
        """Return a deep copy of *row*'s numeric data dict, or None."""
        sym = self.symbol_at(row)
        return copy.deepcopy(self._data[sym]) if sym is not None else None

    def insert_symbol_with_data(self, symbol: str, data: dict) -> bool:
        """Insert *symbol* pre-populated with *data* (a copy_row_data result)."""
        symbol = symbol.strip().upper()
        if not symbol or symbol in self._data:
            return False
        pos = len(self._symbols)
        self.beginInsertRows(QModelIndex(), pos, pos)
        self._symbols.append(symbol)
        self._data[symbol] = dict(data)   # values are all floats – shallow ok
        self.endInsertRows()
        return True

    def rows_to_tsv(self, rows: list[int]) -> str:
        """Serialise *rows* to a tab-separated string suitable for the clipboard.

        Format:  Symbol \\t col1_val \\t col2_val … (33 fields per line)
        Val columns contain the computed Offset × Multiplier value.
        """
        lines: list[str] = []
        for row in rows:
            sym = self.symbol_at(row)
            if sym is None:
                continue
            d = self._data[sym]
            mult = self._multiplier(sym)
            parts = [sym]
            for k in _COL_KEYS[1:]:
                if k[2] == "offset":
                    parts.append(str(d.get(k, 0.0)))
                else:
                    offset = d.get((k[0], k[1], "offset"), 0.0)
                    parts.append(str(offset * mult))
            lines.append("\t".join(parts))
        return "\n".join(lines)

    @staticmethod
    def parse_tsv_rows(text: str) -> list[tuple[str, dict]]:
        """Parse clipboard text (33-column TSV) into a list of (symbol, data) tuples.

        Lines that cannot be parsed are silently skipped.
        """
        results: list[tuple[str, dict]] = []
        for line in text.splitlines():
            parts = line.strip().split("\t")
            if len(parts) != _NCOLS:
                continue
            sym = parts[0].strip().upper()
            if not sym:
                continue
            d: dict = {}
            for i, key in enumerate(_COL_KEYS[1:], start=1):
                try:
                    d[key] = float(parts[i])
                except (ValueError, IndexError):
                    d[key] = 0.0
            results.append((sym, d))
        return results


# ── Delegate ──────────────────────────────────────────────────────────────────

class _SpinDelegate(QStyledItemDelegate):
    """QDoubleSpinBox editor for numeric offset / value cells."""

    def createEditor(self, parent, option, index):
        sb = QDoubleSpinBox(parent)
        sb.setDecimals(4)
        sb.setMinimum(0.0)
        sb.setMaximum(999_999.0)
        sb.setSingleStep(0.01)
        sb.setStyleSheet(f"""
            QDoubleSpinBox {{
                background-color: #1A2A3A;
                color: {TEXT};
                border: 1px solid {BLUE};
                border-radius: 3px;
                padding: 2px 6px;
            }}
        """)
        return sb

    def setEditorData(self, editor, index):
        val = index.data(Qt.ItemDataRole.EditRole)
        editor.setValue(float(val) if val is not None else 0.0)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.value(), Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


# ── Two-pane frozen-column widget ─────────────────────────────────────────────

_TABLE_STYLE = f"""
    QTableView {{
        background-color: {BG_PRIMARY};
        color: {TEXT};
        gridline-color: {BORDER};
        border: none;
        font-size: 12px;
        selection-background-color: #1C2740;
        selection-color: {TEXT};
    }}
    QHeaderView::section {{
        background-color: {BG_HEADER};
        color: {TEXT_DIM};
        border: none;
        border-bottom: 1px solid {BORDER};
        border-right: 1px solid {BORDER};
        padding: 4px 6px;
        font-weight: 600;
        font-size: 11px;
    }}
    QTableView::item {{
        padding: 3px 8px;
    }}
    QScrollBar:vertical   {{ background: {BG_PRIMARY}; width: 8px; }}
    QScrollBar:horizontal {{ background: {BG_PRIMARY}; height: 8px; }}
    QScrollBar::handle:vertical   {{ background: #232A3B; border-radius: 4px; min-height: 20px; }}
    QScrollBar::handle:horizontal {{ background: #232A3B; border-radius: 4px; min-width:  20px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
"""

_FROZEN_STYLE = _TABLE_STYLE + f"""
    QTableView {{
        border-right: 2px solid {BORDER};
    }}
"""


class _FrozenColumnWidget(QWidget):
    """
    Left pane (Symbol, frozen) + right pane (data, scrollable).
    Both share the same StrategyPivotModel; vertical scroll is kept in sync.
    """

    FROZEN_COL_WIDTH = 120

    # Emitted when the user right-clicks; carries the global position and
    # the logical row index (-1 if outside all rows).
    context_menu_requested = Signal(object, int)   # (QPoint, row)

    def __init__(self, model: StrategyPivotModel, parent=None):
        super().__init__(parent)
        self._model = model
        self._delegate = _SpinDelegate(self)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Frozen pane ───────────────────────────────────────────────────────
        self._frozen = QTableView()
        self._frozen.setModel(model)
        self._frozen.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._frozen.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._frozen.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._frozen.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._frozen.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection)
        self._frozen.verticalHeader().setVisible(False)
        self._frozen.setStyleSheet(_FROZEN_STYLE)
        self._frozen.setShowGrid(True)
        self._frozen.verticalHeader().setDefaultSectionSize(30)
        self._frozen.setFixedWidth(self.FROZEN_COL_WIDTH + 2)  # +border
        self._frozen.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed)
        self._frozen.setColumnWidth(0, self.FROZEN_COL_WIDTH)
        # Hide all non-symbol columns
        for col in range(1, _NCOLS):
            self._frozen.hideColumn(col)
        self._frozen.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)

        # ── Data pane ─────────────────────────────────────────────────────────
        self._data_view = QTableView()
        self._data_view.setModel(model)
        self._data_view.hideColumn(0)
        self._data_view.setItemDelegate(self._delegate)
        self._data_view.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked)
        self._data_view.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._data_view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._data_view.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._data_view.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._data_view.verticalHeader().setVisible(False)
        self._data_view.verticalHeader().setDefaultSectionSize(30)
        self._data_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        self._data_view.horizontalHeader().setStretchLastSection(False)
        self._data_view.setStyleSheet(_TABLE_STYLE)
        self._data_view.setShowGrid(True)
        for col in range(1, _NCOLS):
            self._data_view.setColumnWidth(col, 80)

        # ── Sync scrolling ────────────────────────────────────────────────────
        self._data_view.verticalScrollBar().valueChanged.connect(
            self._frozen.verticalScrollBar().setValue)
        self._frozen.verticalScrollBar().valueChanged.connect(
            self._data_view.verticalScrollBar().setValue)

        # Keep frozen row heights in sync when data is added/removed
        model.rowsInserted.connect(self._sync_row_heights)
        model.rowsRemoved.connect(self._sync_row_heights)
        model.modelReset.connect(self._sync_row_heights)

        # Right-click context menus – both panes forward to parent
        self._frozen.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._data_view.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._frozen.customContextMenuRequested.connect(
            lambda pt: self._dispatch_context(self._frozen, pt))
        self._data_view.customContextMenuRequested.connect(
            lambda pt: self._dispatch_context(self._data_view, pt))

        layout.addWidget(self._frozen)
        layout.addWidget(self._data_view, 1)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _dispatch_context(self, view: QTableView, local_pt) -> None:
        """Map a viewport-local point to a row index and emit the signal."""
        idx = view.indexAt(local_pt)
        row = idx.row() if idx.isValid() else -1
        self.context_menu_requested.emit(view.viewport().mapToGlobal(local_pt), row)

    def _sync_row_heights(self, *_args) -> None:
        for row in range(self._model.rowCount()):
            h = self._data_view.rowHeight(row)
            self._frozen.setRowHeight(row, h)

    def selected_rows(self) -> list[int]:
        idxs = self._data_view.selectionModel().selectedRows()
        return sorted({i.row() for i in idxs})


# ── Toolbar button styles ─────────────────────────────────────────────────────

_BTN_BASE = f"""
    QPushButton {{
        background-color: transparent;
        color: {TEXT};
        border: 1px solid #262E3F;
        border-radius: 4px;
        padding: 6px 16px;
        font-weight: 500;
        font-size: 13px;
    }}
    QPushButton:hover {{ background-color: #232A3B; }}
    QPushButton:pressed {{ background-color: {BG_PRIMARY}; }}
"""
_BTN_SAVE = f"""
    QPushButton {{
        background-color: #1A3A2A;
        color: {GREEN};
        border: 1px solid #2A4A3A;
        border-radius: 4px;
        padding: 6px 16px;
        font-weight: bold;
        font-size: 13px;
    }}
    QPushButton:hover {{ background-color: #244A34; }}
    QPushButton:pressed {{ background-color: #1A3A2A; }}
"""
_BTN_DELETE = f"""
    QPushButton {{
        background-color: #2A1A1A;
        color: {RED};
        border: 1px solid #3A2A2A;
        border-radius: 4px;
        padding: 6px 16px;
        font-weight: 500;
        font-size: 13px;
    }}
    QPushButton:hover {{ background-color: #3A2424; }}
    QPushButton:pressed {{ background-color: #2A1A1A; }}
"""


# ── Top-level view ────────────────────────────────────────────────────────────

class StrategyEditorView(QWidget):
    """
    Full-screen Strategy Tier Editor page.

    Toolbar:  ➕ Add Symbol  |  💾 Save Database  |  🗑️ Delete Symbol
    Body:     FrozenColumnWidget  (33-column pivot table)
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = StrategyPivotModel(self)
        self._setup_ui()
        self._model.load_from_db()
        self._load_multipliers()

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"StrategyEditorView {{ background-color: {BG_PRIMARY}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # Title
        title = QLabel("Strategy Tier Editor")
        title.setFont(QFont("Inter, Helvetica Neue, Arial", 16, QFont.Weight.DemiBold))
        title.setStyleSheet(
            f"QLabel {{ color: {TEXT}; background: transparent; }}")
        root.addWidget(title)

        # Section legend
        legend_row = QHBoxLayout()
        legend_row.setSpacing(20)
        for text, color in _LEGEND.items():
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"QLabel {{ color: {color}; font-size: 11px; "
                f"background: transparent; font-weight: bold; }}")
            legend_row.addWidget(lbl)
        legend_row.addStretch()
        root.addLayout(legend_row)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        btn_add = QPushButton("➕  Add Symbol")
        btn_add.setStyleSheet(_BTN_BASE)
        btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_add.clicked.connect(self._on_add)
        toolbar.addWidget(btn_add)

        btn_save = QPushButton("💾  Save Database")
        btn_save.setStyleSheet(_BTN_SAVE)
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.clicked.connect(self._on_save)
        toolbar.addWidget(btn_save)

        btn_del = QPushButton("🗑️  Delete Symbol")
        btn_del.setStyleSheet(_BTN_DELETE)
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.clicked.connect(self._on_delete)
        toolbar.addWidget(btn_del)

        btn_dup = QPushButton("⧉  Duplicate Row")
        btn_dup.setStyleSheet(_BTN_BASE)
        btn_dup.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_dup.setToolTip("Duplicate selected row(s)  (Ctrl+D)")
        btn_dup.clicked.connect(self._on_duplicate)
        toolbar.addWidget(btn_dup)

        btn_refresh = QPushButton("🔄  Refresh Multipliers")
        btn_refresh.setStyleSheet(_BTN_BASE)
        btn_refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_refresh.setToolTip(
            "Reload contract multipliers from the Contracts tab")
        btn_refresh.clicked.connect(self._load_multipliers)
        toolbar.addWidget(btn_refresh)

        toolbar.addStretch()

        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(
            f"QLabel {{ color: {GREEN}; font-size: 12px; "
            f"background: transparent; }}")
        toolbar.addWidget(self._status_lbl)

        root.addLayout(toolbar)

        # Pivot table with frozen Symbol column
        self._table_widget = _FrozenColumnWidget(self._model, self)
        self._table_widget.context_menu_requested.connect(self._show_context_menu)
        root.addWidget(self._table_widget, 1)

        # ── Keyboard shortcuts ────────────────────────────────────────────────
        QShortcut(QKeySequence("Ctrl+C"), self).activated.connect(self._on_copy)
        QShortcut(QKeySequence("Ctrl+V"), self).activated.connect(self._on_paste)
        QShortcut(QKeySequence("Ctrl+D"), self).activated.connect(self._on_duplicate)

    # ── Slot: Add Symbol ──────────────────────────────────────────────────────

    def _on_add(self) -> None:
        text, ok = QInputDialog.getText(
            self, "Add Symbol",
            "Enter contract symbol (e.g. NQ, AAPL, MGC):")
        if not ok or not text.strip():
            return
        sym = text.strip().upper()
        if self._model.add_symbol(sym):
            self._set_status(f'Added  "{sym}"')
        else:
            QMessageBox.warning(
                self, "Duplicate",
                f'"{sym}" is already in the table.')

    # ── Slot: Save ────────────────────────────────────────────────────────────

    def _on_save(self) -> None:
        try:
            self._model.save_to_db()
            from src.core.reference_data import get_instance as _rd
            _rd().reload_strategy_tiers()
            n = self._model.rowCount()
            self._set_status(f"✅  Saved {n} symbol(s) to database")
        except Exception as exc:
            log.exception("StrategyEditor save error")
            QMessageBox.critical(self, "Save Failed", str(exc))

    # ── Slot: Delete ──────────────────────────────────────────────────────────

    def _on_delete(self) -> None:
        rows = self._table_widget.selected_rows()
        if not rows:
            QMessageBox.information(
                self, "No Selection",
                "Select one or more rows in the table first.")
            return
        symbols = [s for s in
                   (self._model.symbol_at(r) for r in rows) if s]
        reply = QMessageBox.question(
            self, "Delete Symbol(s)",
            f"Delete {len(symbols)} symbol(s)?\n\n" +
            "\n".join(f"  • {s}" for s in symbols),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for row in reversed(rows):
            self._model.delete_symbol_at(row)
        self._set_status(f"🗑️  Deleted {len(rows)} symbol(s)")

    # ── Slot: Copy (Ctrl+C) ───────────────────────────────────────────────────

    def _on_copy(self) -> None:
        rows = self._table_widget.selected_rows()
        if not rows:
            return
        tsv = self._model.rows_to_tsv(rows)
        QApplication.clipboard().setText(tsv)
        self._set_status(
            f"📋  Copied {len(rows)} row(s) to clipboard  (Ctrl+V to paste)")

    # ── Slot: Paste (Ctrl+V) ──────────────────────────────────────────────────

    def _on_paste(self) -> None:
        text = QApplication.clipboard().text().strip()
        if not text:
            return
        parsed = StrategyPivotModel.parse_tsv_rows(text)
        if not parsed:
            QMessageBox.warning(
                self, "Paste Failed",
                "Clipboard data doesn't match the expected 33-column format.\n"
                "Use Ctrl+C inside this table to copy rows first.")
            return

        added = 0
        for src_sym, data in parsed:
            # If symbol already exists, ask for a new name
            if src_sym in self._model._data:
                new_sym = self._ask_new_symbol(
                    src_sym,
                    f'"{src_sym}" already exists.\n\nEnter a new symbol name:',
                    f"{src_sym}_COPY",
                )
                if new_sym is None:
                    continue        # user cancelled this row
                sym = new_sym
            else:
                sym = src_sym

            if self._model.insert_symbol_with_data(sym, data):
                added += 1

        if added:
            self._set_status(f"📋  Pasted {added} row(s)")
        else:
            self._set_status("Paste: no new rows added")

    # ── Slot: Duplicate (Ctrl+D) ──────────────────────────────────────────────

    def _on_duplicate(self) -> None:
        rows = self._table_widget.selected_rows()
        if not rows:
            QMessageBox.information(
                self, "No Selection",
                "Select one or more rows to duplicate.")
            return

        added = 0
        for row in rows:
            src_sym = self._model.symbol_at(row)
            if src_sym is None:
                continue
            data = self._model.copy_row_data(row)
            new_sym = self._ask_new_symbol(
                src_sym,
                f'Duplicating  "{src_sym}"\n\nEnter symbol name for the copy:',
                f"{src_sym}_COPY",
            )
            if new_sym is None:
                continue
            if self._model.insert_symbol_with_data(new_sym, data):
                added += 1
            else:
                QMessageBox.warning(
                    self, "Duplicate Failed",
                    f'"{new_sym}" already exists.')

        if added:
            self._set_status(f"⧉  Duplicated {added} row(s)")

    # ── Context menu ──────────────────────────────────────────────────────────

    def _show_context_menu(self, global_pos, row: int) -> None:
        has_selection = bool(self._table_widget.selected_rows())

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: #1B2130;
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 4px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 3px;
            }}
            QMenu::item:selected {{
                background-color: #1C2740;
            }}
            QMenu::item:disabled {{
                color: {TEXT_DIM};
            }}
            QMenu::separator {{
                height: 1px;
                background: {BORDER};
                margin: 4px 8px;
            }}
        """)

        act_copy = menu.addAction("📋  Copy row(s)          Ctrl+C")
        act_copy.setEnabled(has_selection)

        act_paste = menu.addAction("📋  Paste as new row(s)  Ctrl+V")
        clipboard_ok = bool(QApplication.clipboard().text().strip())
        act_paste.setEnabled(clipboard_ok)

        menu.addSeparator()

        act_dup = menu.addAction("⧉  Duplicate row(s)      Ctrl+D")
        act_dup.setEnabled(has_selection)

        menu.addSeparator()

        act_del = menu.addAction("🗑️  Delete row(s)")
        act_del.setEnabled(has_selection)

        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen == act_copy:
            self._on_copy()
        elif chosen == act_paste:
            self._on_paste()
        elif chosen == act_dup:
            self._on_duplicate()
        elif chosen == act_del:
            self._on_delete()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ask_new_symbol(self, src: str, prompt: str,
                        default: str) -> str | None:
        """Show an input dialog; return the validated symbol or None if cancelled."""
        while True:
            text, ok = QInputDialog.getText(
                self, f"New Symbol for  {src}", prompt,
                text=default)
            if not ok:
                return None
            sym = text.strip().upper()
            if not sym:
                QMessageBox.warning(self, "Invalid", "Symbol cannot be empty.")
                continue
            if sym in self._model._data:
                QMessageBox.warning(
                    self, "Duplicate",
                    f'"{sym}" already exists.  Choose a different name.')
                default = sym + "_COPY"
                continue
            return sym

    # ── Multiplier loader ─────────────────────────────────────────────────────

    def _load_multipliers(self) -> None:
        """Read multipliers from ReferenceData (already loaded at startup)."""
        from src.core.reference_data import get_instance as _rd
        rd = _rd()

        # If the cache is still empty (e.g. strategy editor opened before
        # main window had a chance to reload), fall back to a direct load.
        if not rd.contracts and not rd.multipliers:
            rd.reload_contracts()

        mults   = rd.multipliers          # dict[UPPER_SYMBOL → float]
        missing = [
            sym for sym in (self._model.symbol_at(r)
                            for r in range(self._model.rowCount()))
            if sym and sym.upper() not in mults
        ]

        self._model.set_multipliers({k.upper(): v for k, v in mults.items()})

        if missing:
            self._set_status(
                f"⚠  Using fallback ×1.0 for: {', '.join(missing)}"
                f" — add them in the Contracts tab to use their real multiplier"
            )
        elif mults:
            self._set_status(
                f"Multipliers loaded for {len(mults)} contract(s)"
            )

    def _set_status(self, msg: str) -> None:
        self._status_lbl.setText(msg)





















