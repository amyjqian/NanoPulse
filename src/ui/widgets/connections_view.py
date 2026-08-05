from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import (
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QStyledItemDelegate,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core import config_manager
from src.core.ib_worker import IBWorkerThread
from .accounts_model import (
    AccountsTableModel,
    COL_ACCOUNT,
    COL_ALIAS,
    COL_DAILY_PNL,
    COL_EXCESS_LIQ,
    COL_NET_LIQ,
    COL_TOTAL_CASH,
)

log = logging.getLogger(__name__)

# ── Theme tokens ──────────────────────────────────────────────────────────────
from src.ui.theme import Theme

BG_PRIMARY = "#151922"
BG_SECONDARY = "#1B2130"
BG_HEADER = "#151922"
BORDER = "#262E3F"
TEXT = "#E6EBF2"
TEXT_DIM = "#A3AFC2"
GREEN = Theme.ACCENT
RED = Theme.CLR_SELL
BLUE = "#539BF5"

_TOOLBAR_BTN = f"""
    QPushButton {{
        background-color: transparent;
        color: {TEXT};
        border: 1px solid #262E3F;
        border-radius: 4px;
        padding: 4px 12px;
        font-weight: 500;
        font-size: 13px;
    }}
    QPushButton:hover {{ background-color: #232A3B; }}
    QPushButton:pressed {{ background-color: {BG_PRIMARY}; }}
    QPushButton:disabled {{ background-color: transparent; color: #6A7690; border-color: {BORDER}; }}
"""

_CONNECT_SS = f"""
    QPushButton {{
        background-color: {GREEN};
        color: #ffffff;
        border: none;
        border-radius: 4px;
        padding: 4px 10px;
        min-width: 90px;
        font-weight: bold;
        font-size: 13px;
    }}
    QPushButton:hover {{ background-color: #4cd860; }}
    QPushButton:pressed {{ background-color: #35a145; }}
    QPushButton:disabled {{ background-color: #1A3A2A; color: #6A7690; }}
"""

_DISCONNECT_SS = f"""
    QPushButton {{
        background-color: {RED};
        color: #ffffff;
        border: none;
        border-radius: 4px;
        padding: 4px 10px;
        min-width: 90px;
        font-weight: bold;
        font-size: 13px;
    }}
    QPushButton:hover {{ background-color: #fc746d; }}
    QPushButton:pressed {{ background-color: #c83c34; }}
"""

_ADD_SS = f"""
    QPushButton {{
        background-color: #6A7690;
        color: #ffffff;
        border: none;
        border-radius: 3px;
        padding: 4px 10px;
        min-width: 90px;
        font-weight: bold;
        font-size: 13px;
    }}
    QPushButton:hover {{ background-color: #6A7690; }}
    QPushButton:pressed {{ background-color: #464B54; }}
"""

_DELETE_SS = _ADD_SS

_NEUTRAL_SS = _TOOLBAR_BTN

# Connection config table columns
# Layout-dict key for the Connect page's top/bottom splitter sizes
# (config_manager.load_layout / save_layout).
_K_CONNECT_SPLIT = "connect_v_splitter"

_CFG_COLS = ["Label", "Host", "Port", "Client ID", "Type",
             "Priority", "Auto Start", "Auto Reconnect", "Status", "Action"]
_COL_LABEL = 0
_COL_HOST = 1
_COL_PORT = 2
_COL_CID = 3
_COL_TYPE = 4
_COL_PRIORITY = 5
_COL_AUTO = 6
_COL_AUTO_RECONNECT = 7
_COL_STATUS = 8
_COL_ACTION = 9


# ── Shared dark stylesheet for QTableWidget / QTableView ──────────────────────
_TABLE_STYLE = f"""
    QTableWidget, QTableView {{
        background-color: {BG_PRIMARY};
        alternate-background-color: {BG_SECONDARY};
        color: {TEXT};
        gridline-color: {BORDER};
        border: 1px solid {BORDER};
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
        padding: 5px 8px;
        font-weight: 600;
        font-size: 11px;
    }}
    QTableWidget::item, QTableView::item {{
        padding: 4px 8px;
    }}
    QTableWidget QLineEdit, QTableView QLineEdit {{
        background-color: {BG_SECONDARY};
        color: {TEXT};
        border: 1px solid #6A7690;
        border-radius: 2px;
        padding: 4px 8px;
        selection-background-color: #1C2740;
        selection-color: {TEXT};
        outline: none;
    }}
    QTableWidget QLineEdit:focus, QTableView QLineEdit:focus {{
        border: 1px solid #6A7690;
        outline: none;
    }}
    QScrollBar:vertical {{ background: {BG_PRIMARY}; width: 8px; }}
    QScrollBar::handle:vertical {{ background: #232A3B; border-radius: 4px; min-height: 20px; }}
    QScrollBar:horizontal {{ background: {BG_PRIMARY}; height: 8px; }}
    QScrollBar::handle:horizontal {{ background: #232A3B; border-radius: 4px; min-width: 20px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
"""


class _ToggleSwitch(QWidget):
    """iOS-style toggle switch with On/Off label."""

    toggled = Signal(bool)

    _TRACK_W = 36
    _TRACK_H = 20
    _KNOB_MARGIN = 3
    _LABEL_GAP = 6

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self._checked = checked
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_size()

    def _update_size(self) -> None:
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self.font())
        label_w = max(fm.horizontalAdvance("On"), fm.horizontalAdvance("Off"))
        total_w = label_w + self._LABEL_GAP + self._TRACK_W + 4
        self.setFixedSize(total_w, self._TRACK_H)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, on: bool) -> None:
        if self._checked != on:
            self._checked = on
            self.update()
            self.toggled.emit(on)

    def mousePressEvent(self, event) -> None:
        self.setChecked(not self._checked)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        h = self._TRACK_H
        tw = self._TRACK_W
        m = self._KNOB_MARGIN
        knob_d = h - m * 2

        # ── Label (left side) ─────────────────────────────────────────────
        label_text = "On" if self._checked else "Off"
        label_color = QColor(TEXT) if self._checked else QColor(TEXT_DIM)
        p.setPen(label_color)
        f = QFont("Inter, Helvetica Neue, Arial", 11)
        f.setWeight(QFont.Weight.Medium)
        p.setFont(f)
        fm = p.fontMetrics()
        label_x = 0
        label_y = (h + fm.ascent() - fm.descent()) // 2
        p.drawText(label_x, label_y, label_text)
        label_w = fm.horizontalAdvance(label_text)

        # ── Track (right side) ────────────────────────────────────────────
        track_x = label_w + self._LABEL_GAP
        r = h // 2

        track_color = QColor("#539BF5") if self._checked else QColor("#232A3B")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_color))
        p.drawRoundedRect(track_x, 0, tw, h, r, r)

        # ── Knob ──────────────────────────────────────────────────────────
        knob_x = (track_x + tw - knob_d -
                  m) if self._checked else (track_x + m)
        knob_y = m
        p.setBrush(QBrush(QColor("#FFFFFF")))
        p.drawEllipse(knob_x, knob_y, knob_d, knob_d)
        p.end()


class _CellComboBox(QComboBox):
    """QComboBox that drops its popup *below* the cell with a fixed min-width."""

    _MIN_POPUP_WIDTH = 110

    def showPopup(self) -> None:
        super().showPopup()
        popup = self.view().window()
        # Shift popup so it starts right below the combobox, not overlapping it.
        pos = self.mapToGlobal(self.rect().bottomLeft())
        w = max(popup.width(), self._MIN_POPUP_WIDTH)
        popup.setGeometry(pos.x(), pos.y() + 2, w, popup.height())


# Columns whose raw cell text is a financial float ("1,234.56" / "+500").
_NUMERIC_COLS: frozenset[int] = frozenset({
    COL_NET_LIQ, COL_TOTAL_CASH, COL_EXCESS_LIQ, COL_DAILY_PNL,
})


class _AccountsSortFilterProxy(QSortFilterProxyModel):
    """Sort / filter wrapper around ``AccountsTableModel``.

    * Multi-column sort: callers set a list of ``(column, order)``
      pairs via :meth:`set_sort_specs`; ``lessThan`` walks that list
      in priority order so ties on the first column break using the
      next column and so on.
    * The virtual totals row appended by ``AccountsTableModel`` is
      pinned to the bottom — it's never sorted alongside data rows
      and always passes the filter, matching what users expect from
      a footer row.
    * ``headerData`` decorates each sorted column's header with an
      ▲ / ▼ arrow (plus a rank number for secondary / tertiary
      sorts) so users can see the current sort state directly.
    * ``filterAcceptsRow`` narrows to rows whose Account or Alias
      column contains the current search text (case-insensitive).
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._sort_specs: list[tuple[int, Qt.SortOrder]] = []
        self._search: str = ""
        self.setDynamicSortFilter(True)

    # ── Sort state ──────────────────────────────────────────────────────────

    def sort_specs(self) -> list[tuple[int, Qt.SortOrder]]:
        return list(self._sort_specs)

    def set_sort_specs(
        self, specs: list[tuple[int, Qt.SortOrder]],
    ) -> None:
        self._sort_specs = list(specs)
        # Poke Qt into resorting.  ``invalidate()`` ensures the sort
        # actually re-runs even when the primary column / order hasn't
        # changed (e.g. adding a secondary sort column) — ``sort()``
        # alone will no-op in that case.
        self.invalidate()
        if specs:
            first_col, first_order = specs[0]
            self.sort(first_col, first_order)
        else:
            # No active sort → restore source order.
            self.sort(-1)
        src = self.sourceModel()
        if src is not None:
            self.headerDataChanged.emit(
                Qt.Orientation.Horizontal, 0, src.columnCount() - 1)

    # ── Search ──────────────────────────────────────────────────────────────

    def set_search_text(self, text: str) -> None:
        new = (text or "").strip().lower()
        if new == self._search:
            return
        self._search = new
        self.invalidateFilter()
        # Totals row recomputes from filter-visible rows; tell the
        # view to repaint it even when the filter itself didn't add
        # or remove the totals row.
        src = self.sourceModel()
        if src is not None and src.account_count() > 0:
            total_src_row = src.account_count()
            total_idx = self.mapFromSource(
                src.index(total_src_row, 0))
            if total_idx.isValid():
                self.dataChanged.emit(
                    self.index(total_idx.row(), 0),
                    self.index(total_idx.row(), src.columnCount() - 1),
                    [Qt.ItemDataRole.DisplayRole,
                     Qt.ItemDataRole.ForegroundRole],
                )

    # ── Totals-row recompute over filtered rows ─────────────────────────────

    def _sum_visible(self, col: int) -> float:
        """Sum ``col`` across rows currently passing the filter (i.e.,
        every proxy row except the totals footer itself)."""
        total = 0.0
        src = self.sourceModel()
        if src is None:
            return total
        # Iterate proxy rows directly — this naturally skips anything
        # filtered out by the search box.
        for proxy_row in range(self.rowCount()):
            src_idx = self.mapToSource(self.index(proxy_row, col))
            src_row = src_idx.row()
            if src_row >= src.account_count():
                continue  # skip the totals row itself
            raw = src._table_data[src_row][col]
            if not raw:
                continue
            try:
                v = float(raw)
            except (ValueError, TypeError):
                continue
            if abs(v) > 1e100:
                continue
            total += v
        return total

    # ── QSortFilterProxyModel overrides ─────────────────────────────────────

    def _is_total_row(self, source_row: int) -> bool:
        src = self.sourceModel()
        return src is not None and source_row >= src.account_count() > 0

    def filterAcceptsRow(  # noqa: N802
        self, source_row: int, source_parent: QModelIndex,
    ) -> bool:
        # Always keep the virtual Total row visible as the footer.
        if self._is_total_row(source_row):
            return True
        if not self._search:
            return True
        src = self.sourceModel()
        acct = str(src.data(src.index(source_row, COL_ACCOUNT)) or "")
        alias = str(src.data(src.index(source_row, COL_ALIAS)) or "")
        needle = self._search
        return needle in acct.lower() or needle in alias.lower()

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # noqa: N802
        # Pin the Total footer row to the bottom regardless of sort
        # direction.  Qt internally reverses the result when the
        # primary order is descending, so we return an order-aware
        # value that survives that flip.
        left_is_total = self._is_total_row(left.row())
        right_is_total = self._is_total_row(right.row())
        if left_is_total != right_is_total:
            primary_order = (
                self._sort_specs[0][1] if self._sort_specs
                else Qt.SortOrder.AscendingOrder)
            # Wanted display: data rows before total row.
            #  asc:  lessThan(data, total) → True   (data < total)
            #  desc: Qt reverses → we need lessThan(data, total) → False
            #                      so the flip puts data first.
            asc = primary_order == Qt.SortOrder.AscendingOrder
            if right_is_total:
                return asc      # data vs total
            return not asc       # total vs data

        src = self.sourceModel()
        primary = (self._sort_specs[0][1]
                   if self._sort_specs else Qt.SortOrder.AscendingOrder)
        # Qt reverses the display when the PRIMARY sort is descending
        # but calls ``lessThan`` with the indices untouched.  Secondary
        # / tertiary columns don't get that free flip, so we apply the
        # desc-specific flip here for spec positions > 0 whose order
        # differs from the primary.
        for i, (col, order) in enumerate(self._sort_specs):
            l_raw = src.data(src.index(left.row(), col))
            r_raw = src.data(src.index(right.row(), col))
            if l_raw == r_raw:
                continue
            if col in _NUMERIC_COLS:
                ln = _coerce_financial(l_raw)
                rn = _coerce_financial(r_raw)
                if ln == rn:
                    continue
                result = ln < rn
            else:
                ls = "" if l_raw is None else str(l_raw).lower()
                rs = "" if r_raw is None else str(r_raw).lower()
                if ls == rs:
                    continue
                result = ls < rs
            # Secondary columns need their own asc/desc handling.
            # Also handle a primary column whose order differs from
            # what Qt will flip (they always match by construction,
            # but the logic below is the general form).
            if i == 0:
                # Qt will flip based on primary_order; leave as-is.
                return result
            # Secondary: apply own order, and pre-invert if primary
            # is descending (Qt will flip it back on display).
            if order == Qt.SortOrder.DescendingOrder:
                result = not result
            if primary == Qt.SortOrder.DescendingOrder:
                result = not result
            return result
        return False

    def data(  # noqa: N802
        self, index: QModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if not index.isValid():
            return None
        src = self.sourceModel()
        if src is None:
            return None
        src_idx = self.mapToSource(index)
        if src._is_total_row(src_idx.row()):
            col = index.column()
            # Financial columns: sum only over rows the search box left
            # visible, then format with the source model's helpers so
            # display style stays consistent with data rows.
            if role == Qt.ItemDataRole.DisplayRole and col in _NUMERIC_COLS:
                total = self._sum_visible(col)
                if col == COL_DAILY_PNL:
                    return src._format_pnl(str(total))
                return src._format_financial(str(total))
            if role == Qt.ItemDataRole.ForegroundRole and col == COL_DAILY_PNL:
                total = self._sum_visible(col)
                if total > 0:
                    return QColor("#3FB950")
                if total < 0:
                    return QColor("#F85149")
                return None
            # Non-financial cells and other roles (font, bg) fall
            # through to the source model's ``_total_data``.
        return super().data(index, role)

    def headerData(  # noqa: N802
        self, section: int, orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        src = self.sourceModel()
        if src is None:
            return None
        base = src.headerData(section, orientation, role)
        if (role != Qt.ItemDataRole.DisplayRole
                or orientation != Qt.Orientation.Horizontal
                or not self._sort_specs):
            return base
        for rank, (col, order) in enumerate(self._sort_specs, start=1):
            if col == section:
                arrow = ("▲"    # ▲
                         if order == Qt.SortOrder.AscendingOrder
                         else "▼")  # ▼
                suffix = arrow if rank == 1 else f"{arrow}{rank}"
                return f"{base} {suffix}"
        return base


def _coerce_financial(raw) -> float:
    """Best-effort conversion of a formatted currency/P&L string to
    float.  Empty / "-" sentinels sort as 0 so they cluster together
    instead of crashing the comparator."""
    if raw is None:
        return 0.0
    s = str(raw).strip()
    if not s or s == "-":
        return 0.0
    try:
        return float(s.replace(",", "").replace("+", ""))
    except (ValueError, TypeError):
        return 0.0


class ConnectionsView(QWidget):
    """
    Full Connections page.

    Parameters
    ----------
    accounts_model : AccountsTableModel
        Shared model owned by MainWindow so that all workers funnel into it.
    active_threads : dict[str, IBWorkerThread]
        Shared thread pool dict owned by MainWindow.
    """

    def __init__(
        self,
        accounts_model: AccountsTableModel,
        active_threads: dict[str, IBWorkerThread],
        parent=None,
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._model = accounts_model
        self._active_threads = active_threads
        # Tracks the last-detected type per connection label.
        # Persisted to JSON independently of the visual label state so that
        # transient "blank" visuals (during connecting / disconnected) never
        # overwrite a previously-detected "Paper"/"Live" value in the config.
        self._detected_types: dict[str, str] = {}
        self._setup_ui()
        self._load_config()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        # Explicit background so the global QWidget rule doesn't bleed through
        self.setStyleSheet(
            f"ConnectionsView {{ background-color: {BG_PRIMARY}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.setHandleWidth(3)
        splitter.setChildrenCollapsible(False)
        splitter.setStyleSheet(f"""
            QSplitter {{
                background-color: {BG_PRIMARY};
            }}
            QSplitter::handle {{
                background-color: {BORDER};
            }}
        """)

        # ── Top pane: config table ────────────────────────────────────────────
        top_pane = QWidget()
        top_pane.setMinimumHeight(120)
        top_pane.setStyleSheet(
            f"QWidget {{ background-color: {BG_PRIMARY}; }}")
        top_layout = QVBoxLayout(top_pane)
        top_layout.setContentsMargins(12, 10, 12, 4)
        top_layout.setSpacing(6)

        # Section label
        lbl_conn = QLabel("Connections")
        lbl_conn.setFont(
            QFont("Inter, Helvetica Neue, Arial", 13, QFont.Weight.DemiBold))
        lbl_conn.setStyleSheet(
            f"QLabel {{ color: {TEXT}; background: transparent; padding: 0 0 2px 0; }}")
        top_layout.addWidget(lbl_conn)

        self._cfg_table = self._build_config_table()
        top_layout.addWidget(self._cfg_table)
        
        # Toolbar actions below table
        top_layout.addLayout(self._build_toolbar())

        # ── Bottom pane: accounts table view ──────────────────────────────────
        bottom_pane = QWidget()
        bottom_pane.setMinimumHeight(100)
        bottom_pane.setStyleSheet(
            f"QWidget {{ background-color: {BG_PRIMARY}; }}")
        bottom_layout = QVBoxLayout(bottom_pane)
        bottom_layout.setContentsMargins(12, 8, 12, 10)
        bottom_layout.setSpacing(6)

        # Section header: label on the left, search input on the right.
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        lbl_acct = QLabel("Accounts")
        lbl_acct.setFont(
            QFont("Inter, Helvetica Neue, Arial", 13, QFont.Weight.DemiBold))
        lbl_acct.setStyleSheet(
            f"QLabel {{ color: {TEXT}; background: transparent; padding: 0 0 2px 0; }}")
        header_row.addWidget(lbl_acct)
        header_row.addStretch(1)

        self._accounts_search = QLineEdit(self)
        self._accounts_search.setPlaceholderText("Search")
        self._accounts_search.setFixedWidth(220)
        self._accounts_search.setStyleSheet(
            "QLineEdit {"
            f" background: {BG_SECONDARY}; color: {TEXT};"
            f" border: 1px solid {BORDER}; border-radius: 4px;"
            " padding: 3px 8px; font-size: 11px;"
            "}"
            f"QLineEdit:focus {{ border-color: {BLUE}; }}"
        )
        self._accounts_search.textChanged.connect(self._on_accounts_search)
        header_row.addWidget(self._accounts_search)
        bottom_layout.addLayout(header_row)

        # Proxy sits between view and source model so sorting / filtering
        # happens without touching ``AccountsTableModel`` (which is shared
        # with the AccountListPanel).
        self._accounts_proxy = _AccountsSortFilterProxy(self)
        self._accounts_proxy.setSourceModel(self._model)

        self._accounts_view = QTableView(self)
        self._accounts_view.setModel(self._accounts_proxy)
        self._accounts_view.setAlternatingRowColors(True)
        self._accounts_view.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._accounts_view.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection)
        self._accounts_view.verticalHeader().setVisible(False)
        self._accounts_view.horizontalHeader().setStretchLastSection(True)
        self._accounts_view.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        # Leave Qt's own sort indicator off — ``_AccountsSortFilterProxy``
        # decorates the header text with ▲/▼ glyphs so it can show
        # multi-column rank (primary ▲, secondary ▼2, etc.).  Header
        # sections stay clickable so we can catch the click.
        header = self._accounts_view.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self._on_accounts_header_clicked)
        self._accounts_view.setStyleSheet(_TABLE_STYLE)
        bottom_layout.addWidget(self._accounts_view)

        splitter.addWidget(top_pane)
        splitter.addWidget(bottom_pane)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)

        # Restore the user's top/bottom split; the stretch factors above
        # remain the first-run default.  Persisted (debounced) on every
        # handle drag — same pattern as FlashTradeView's splitters.
        self._splitter = splitter
        saved = config_manager.load_layout().get(_K_CONNECT_SPLIT)
        if (isinstance(saved, list) and len(saved) == 2
                and all(isinstance(v, (int, float)) and v > 0
                        for v in saved)):
            splitter.setSizes([int(v) for v in saved])
        self._split_save_timer = QTimer(self)
        self._split_save_timer.setSingleShot(True)
        self._split_save_timer.setInterval(300)     # debounce 300 ms
        self._split_save_timer.timeout.connect(self._persist_splitter)
        splitter.splitterMoved.connect(
            lambda _pos, _idx: self._split_save_timer.start())

        layout.addWidget(splitter)

        # When a connection row is selected, highlight its accounts below.
        self._cfg_table.itemSelectionChanged.connect(
            self._on_connection_selected)

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> QHBoxLayout:
        tb = QHBoxLayout()
        tb.setSpacing(6)

        btn_add = QPushButton("Add")
        btn_add.setStyleSheet(_ADD_SS)
        btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_add.clicked.connect(self._on_add_row)
        tb.addWidget(btn_add)

        btn_del = QPushButton("Delete")
        btn_del.setStyleSheet(_DELETE_SS)
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.clicked.connect(self._on_delete_row)
        tb.addWidget(btn_del)

        btn_connect_all = QPushButton("Connect All")
        btn_connect_all.setStyleSheet(_CONNECT_SS)
        btn_connect_all.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_connect_all.clicked.connect(self._on_connect_all)
        tb.addWidget(btn_connect_all)

        btn_disconnect_all = QPushButton("Disconnect All")
        btn_disconnect_all.setStyleSheet(_DISCONNECT_SS)
        btn_disconnect_all.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_disconnect_all.clicked.connect(self._on_disconnect_all)
        tb.addWidget(btn_disconnect_all)

        tb.addStretch(1)
        return tb

    # ── Config table ──────────────────────────────────────────────────────────

    def _build_config_table(self) -> QTableWidget:
        t = QTableWidget(0, len(_CFG_COLS), self)
        t.setHorizontalHeaderLabels(_CFG_COLS)
        t.setAlternatingRowColors(True)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(42)  # Increase height to accommodate widgets
        t.horizontalHeader().setStretchLastSection(True)

        # Column widths
        t.setColumnWidth(_COL_LABEL, 140)
        t.setColumnWidth(_COL_HOST, 130)
        t.setColumnWidth(_COL_PORT, 60)
        t.setColumnWidth(_COL_CID, 70)
        t.setColumnWidth(_COL_TYPE, 90)
        t.setColumnWidth(_COL_PRIORITY, 70)
        t.setColumnWidth(_COL_AUTO, 80)
        t.setColumnWidth(_COL_AUTO_RECONNECT, 110)
        t.setColumnWidth(_COL_STATUS, 120)
        t.setColumnWidth(_COL_ACTION, 120)
        # Action column stretches (last)

        t.setStyleSheet(_TABLE_STYLE)

        # Auto-save on cell edits (only text cells fire this)
        t.cellChanged.connect(self._auto_save)
        return t

    # ── Row creation ──────────────────────────────────────────────────────────

    def _add_config_row(
        self,
        label: str = "",
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int | None = None,
        conn_type: str = "",
        priority: int = 1,
        auto: bool = False,
        auto_reconnect: bool = True,
    ) -> int:
        """Insert a new row into the config table and return its index."""
        if client_id is None:
            client_id = config_manager.get_next_client_id()

        # prevent cellChanged during bulk set
        self._cfg_table.blockSignals(True)

        row = self._cfg_table.rowCount()
        self._cfg_table.insertRow(row)

        # Label
        self._cfg_table.setItem(row, _COL_LABEL, QTableWidgetItem(label))
        # Host
        self._cfg_table.setItem(row, _COL_HOST, QTableWidgetItem(host))
        # Port
        self._cfg_table.setItem(row, _COL_PORT, QTableWidgetItem(str(port)))
        # Client ID (read-only)
        cid_item = QTableWidgetItem(str(client_id))
        cid_item.setFlags(cid_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        cid_item.setForeground(QColor(TEXT_DIM))
        self._cfg_table.setItem(row, _COL_CID, cid_item)

        # Type label (read-only, auto-detected from account numbers)
        type_lbl = QLabel(conn_type if conn_type in ("Paper", "Live") else "")
        type_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _type_color = GREEN if conn_type == "Live" else (BLUE if conn_type == "Paper" else TEXT_DIM)
        type_lbl.setStyleSheet(f"""
            QLabel {{
                color: {_type_color};
                background: transparent;
                font-size: 12px;
                font-weight: 600;
                padding: 3px 8px;
            }}
        """)
        type_lbl.setObjectName("type_label")
        type_container = QWidget()
        type_container.setStyleSheet("background: transparent;")
        type_layout = QHBoxLayout(type_container)
        type_layout.setContentsMargins(0, 0, 0, 0)
        type_layout.addWidget(type_lbl)
        self._cfg_table.setCellWidget(row, _COL_TYPE, type_container)

        # Priority dropdown (1–4)
        pri_combo = _CellComboBox()
        pri_combo.addItems(["1", "2", "3", "4"])
        pri_combo.setCurrentText(
            str(priority) if priority in (1, 2, 3, 4) else "1")
        pri_combo.setItemDelegate(QStyledItemDelegate(pri_combo))
        pri_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {BG_SECONDARY};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 4px;
                padding: 3px 8px;
                font-size: 12px;
                min-width: 36px;
            }}
            QComboBox:hover {{
                border: 1px solid {BLUE};
            }}
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: center right;
                width: 20px;
                border: none;
            }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {TEXT_DIM};
                margin-right: 6px;
            }}
        """)
        pri_popup_view = pri_combo.view()
        pri_popup_view.setStyleSheet(f"""
            QAbstractItemView {{
                background-color: {BG_SECONDARY};
                color: {TEXT};
                border: 1px solid {BORDER};
                padding: 4px 0;
                outline: 0;
                selection-background-color: #1C2740;
                selection-color: {TEXT};
            }}
            QAbstractItemView::item {{
                padding: 6px 12px;
                min-height: 22px;
            }}
            QAbstractItemView::item:hover,
            QAbstractItemView::item:selected {{
                background-color: #1C2740;
                color: {TEXT};
            }}
        """)
        pri_popup_container = pri_popup_view.window()
        pri_popup_container.setWindowFlags(
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
        )
        pri_popup_container.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, False)
        pri_popup_container.setStyleSheet(f"""
            QWidget {{
                background-color: {BG_SECONDARY};
                border: 1px solid {BORDER};
                border-radius: 4px;
            }}
        """)
        pri_combo.currentTextChanged.connect(self._auto_save)
        self._cfg_table.setCellWidget(row, _COL_PRIORITY, pri_combo)

        # Auto Connect toggle switch (centered)
        toggle = _ToggleSwitch(checked=auto)
        toggle.toggled.connect(self._auto_save)
        container = QWidget()
        tgl_layout = QHBoxLayout(container)
        tgl_layout.addWidget(toggle)
        tgl_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tgl_layout.setContentsMargins(0, 0, 0, 0)
        self._cfg_table.setCellWidget(row, _COL_AUTO, container)

        # Auto Reconnect toggle switch (centered)
        recon_toggle = _ToggleSwitch(checked=auto_reconnect)
        recon_toggle.toggled.connect(self._auto_save)
        recon_container = QWidget()
        recon_layout = QHBoxLayout(recon_container)
        recon_layout.addWidget(recon_toggle)
        recon_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        recon_layout.setContentsMargins(0, 0, 0, 0)
        self._cfg_table.setCellWidget(row, _COL_AUTO_RECONNECT, recon_container)

        # Action button (centered, same width as toolbar "Connect All")
        btn = QPushButton("Connect")
        btn.setStyleSheet(_CONNECT_SS)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedWidth(100)
        btn.clicked.connect(lambda checked, r=row: self._on_action_clicked(r))
        btn_container = QWidget()
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.addWidget(btn)
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        self._cfg_table.setCellWidget(row, _COL_ACTION, btn_container)

        # Status (read-only placeholder)
        status_lbl = QLabel("")
        status_lbl.setStyleSheet(f"color: {TEXT_DIM}; background: transparent; padding-left: 4px;")
        status_container = QWidget()
        status_container.setStyleSheet("background: transparent;")
        status_layout = QHBoxLayout(status_container)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.addWidget(status_lbl)
        self._cfg_table.setCellWidget(row, _COL_STATUS, status_container)

        self._cfg_table.blockSignals(False)
        return row

    # ── Toolbar handlers ──────────────────────────────────────────────────────

    def _on_add_row(self) -> None:
        self._add_config_row()
        self._auto_save()
        log.info("Added new connection row (total=%d",
                 self._cfg_table.rowCount())

    def _on_delete_row(self) -> None:
        row = self._cfg_table.currentRow()
        if row < 0:
            return
        label = self._row_label(row)
        log.info("Deleting connection row %d (label=%r)", row, label)

        # Block config-table signals during delete to prevent cellChanged
        # from _on_connection_status interfering mid-removal.
        self._cfg_table.blockSignals(True)

        # Disconnect if running
        if label and label in self._active_threads:
            self._disconnect_label(label)

        # Remove tracked type for this label
        self._detected_types.pop(label, None)

        self._cfg_table.removeRow(row)
        self._cfg_table.blockSignals(False)

        # Re-index action button row properties
        self._reindex_action_buttons()
        self._auto_save()

    def _on_connect_all(self) -> None:
        log.info("Connect All requested (%d rows)", self._cfg_table.rowCount())
        for row in range(self._cfg_table.rowCount()):
            label = self._row_label(row)
            if not label:
                continue
            # Clean up dead worker threads still sitting in the dict
            existing = self._active_threads.get(label)
            if existing is not None and not existing.isRunning():
                log.info("Removing dead worker for %s before reconnect", label)
                self._active_threads.pop(label, None)
                existing = None
            if existing is None:
                self._connect_row(row)

    def _on_disconnect_all(self) -> None:
        log.info("Disconnect All requested (%d active)",
                 len(self._active_threads))
        for row in range(self._cfg_table.rowCount()):
            label = self._row_label(row)
            if label and label in self._active_threads:
                self._disconnect_label(label)
                self._set_row_state(row, connected=False)

    # ── Per-row action button ─────────────────────────────────────────────────

    def _on_action_clicked(self, row: int) -> None:
        label = self._row_label(row)
        if not label:
            log.warning(
                "Action clicked on row %d but label is empty – ignoring", row)
            return

        # Clean up dead worker threads still sitting in the dict
        existing = self._active_threads.get(label)
        if existing is not None and not existing.isRunning():
            log.info("Removing dead worker for %s", label)
            self._active_threads.pop(label, None)
            existing = None

        if existing is not None:
            log.info("Disconnect requested for %r", label)
            self._disconnect_label(label)
            self._set_row_state(row, connected=False)
        else:
            self._connect_row(row)

    def _connect_row(self, row: int) -> None:
        label = self._row_label(row)
        host = self._row_text(row, _COL_HOST) or "127.0.0.1"
        try:
            port = int(self._row_text(row, _COL_PORT) or "7497")
        except ValueError:
            port = 7497
        try:
            client_id = int(self._row_text(row, _COL_CID) or "100")
        except ValueError:
            client_id = 100

        if not label:
            return

        # Read the Auto Reconnect toggle so the worker honours the flag
        recon_cb = self._get_reconnect_checkbox(row)
        auto_reconnect = recon_cb.isChecked() if recon_cb is not None else True

        log.info("Connecting %s (auto_reconnect=%s)", label, auto_reconnect)

        worker = IBWorkerThread(
            connection_label=label,
            host=host,
            port=port,
            client_id=client_id,
            auto_reconnect=auto_reconnect,
        )
        # The Funnel: worker → shared model
        worker.summary_batch_updated.connect(self._model.update_batch)
        worker.connection_status_changed.connect(self._on_connection_status)
        worker.accounts_detected.connect(self._on_accounts_detected)
        # Seed the model's per-label state with "not yet connected" so any
        # pre-existing rows for this label (re-connect after a stop)
        # immediately render dimmed; the worker's first
        # ``connection_status_changed(label, True)`` will flip it back.
        self._model.set_connection_status(label, False)
        self._active_threads[label] = worker
        worker.start()

        # Show a "Connecting…" state; the real Connected / Disconnected
        # will arrive via connection_status_changed signal.
        self._set_row_state(row, connected=False, connecting=True)
        log.info("Started worker for %s (%s:%d cid=%d)",
                 label, host, port, client_id)

    def _disconnect_label(self, label: str) -> None:
        worker = self._active_threads.pop(label, None)
        if worker is None:
            return

        # Disconnect signals first so no stale updates arrive after row removal.
        try:
            worker.summary_batch_updated.disconnect(self._model.update_batch)
        except RuntimeError:
            pass
        try:
            worker.connection_status_changed.disconnect(
                self._on_connection_status)
        except RuntimeError:
            pass
        try:
            worker.accounts_detected.disconnect(self._on_accounts_detected)
        except RuntimeError:
            pass

        worker.stop()
        worker.wait(5000)

        # Flush any queued cross-thread signals so they don't fire later.
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()

        self._model.remove_rows_by_label(label)
        log.info("Stopped worker for %s", label)
        self._auto_save()

    def _on_connection_status(self, label: str, connected: bool) -> None:
        """Update both Action button and Status cell for the matching row,
        and forward the live state into ``AccountsTableModel`` so the
        AccountListPanel and order fan-out can react too."""
        log.info("Connection status: %s → %s", label,
                 "Connected" if connected else "Disconnected")
        for row in range(self._cfg_table.rowCount()):
            if self._row_label(row) == label:
                self._set_row_state(row, connected=connected)
                break
        # Make every other consumer of the model aware of the state flip.
        self._model.set_connection_status(label, connected)

    def _on_accounts_detected(self, label: str, accounts: list) -> None:
        """Auto-populate the Type column based on detected account IDs.

        Rules:
        - Account starts with D/d → Paper
        - Account starts with U/u/F/f → Live
        If the connection has mixed types we prefer "Live".
        """
        detected_type = ""
        for acct in accounts:
            first = acct[0].upper() if acct else ""
            if first in ("U", "F"):
                detected_type = "Live"
                break  # Live takes precedence
            elif first == "D":
                if not detected_type:
                    detected_type = "Paper"

        if not detected_type:
            return

        log.info("Auto-detected type for %s: %s (accounts=%s)",
                 label, detected_type, accounts)

        # Persist in the dict – this is the authoritative value for JSON.
        self._detected_types[label] = detected_type

        for row in range(self._cfg_table.rowCount()):
            if self._row_label(row) == label:
                self._set_type_for_row(row, detected_type)
                self._auto_save()
                break

    def _set_type_for_row(self, row: int, type_text: str) -> None:
        """Update the read-only Type label for the given row."""
        lbl = self._get_type_label(row)
        if lbl is None:
            return
        lbl.setText(type_text)
        color = GREEN if type_text == "Live" else (BLUE if type_text == "Paper" else TEXT_DIM)
        lbl.setStyleSheet(f"""
            QLabel {{
                color: {color};
                background: transparent;
                font-size: 12px;
                font-weight: 600;
                padding: 3px 8px;
            }}
        """)

    # ── Connection ↔ Account highlight sync ───────────────────────────────────

    def _on_connection_selected(self) -> None:
        """When a connection row is selected, highlight its account rows below."""
        sel_model = self._accounts_view.selectionModel()
        sel_model.clearSelection()

        row = self._cfg_table.currentRow()
        if row < 0:
            return
        label = self._row_label(row)
        if not label:
            return

        # Find all account-model rows whose Connection column matches
        # this label.  The view is driven by ``_accounts_proxy`` so
        # translate each source index before passing to the selection
        # model; rows currently filtered out by the search box are
        # skipped automatically.
        from .accounts_model import COL_CONNECTION
        for acct_row, row_data in enumerate(self._model._table_data):
            if row_data[COL_CONNECTION] == label:
                src_idx = self._model.index(acct_row, 0)
                proxy_idx = self._accounts_proxy.mapFromSource(src_idx)
                if not proxy_idx.isValid():
                    continue
                sel_model.select(
                    proxy_idx,
                    sel_model.SelectionFlag.Select | sel_model.SelectionFlag.Rows,
                )

    # ── Accounts table sort + search ──────────────────────────────────────────

    def _on_accounts_search(self, text: str) -> None:
        """Live-filter the accounts table as the user types."""
        self._accounts_proxy.set_search_text(text)

    def _on_accounts_header_clicked(self, section: int) -> None:
        """Cycle the clicked column through asc → desc → unsorted.

        Multi-column sort: the click-order of the columns defines
        priority.  Clicking a column that's already in the sort list
        flips its direction; a third click removes it.  Clicking a
        new column appends it as a secondary (or tertiary…) sort.
        """
        specs = self._accounts_proxy.sort_specs()
        idx = next((i for i, (c, _) in enumerate(specs) if c == section),
                   None)
        if idx is None:
            specs.append((section, Qt.SortOrder.AscendingOrder))
        else:
            col, order = specs[idx]
            if order == Qt.SortOrder.AscendingOrder:
                specs[idx] = (col, Qt.SortOrder.DescendingOrder)
            else:
                # Third click → drop this column from the sort list.
                specs.pop(idx)
        self._accounts_proxy.set_sort_specs(specs)

    # ── Row state (Action + Status) ─────────────────────────────────────────────

    def _set_row_state(self, row: int, *, connected: bool, connecting: bool = False) -> None:
        """Update both the Action button and Status cell for *row* in one place."""
        # Clear Type label when not fully connected
        if not connected or connecting:
            self._set_type_for_row(row, "")

        # Action button (inside a container widget)
        btn = self._get_action_button(row)
        if btn is not None:
            if connecting:
                btn.setText("Disconnect")
                btn.setStyleSheet(_DISCONNECT_SS)
            elif connected:
                btn.setText("Disconnect")
                btn.setStyleSheet(_DISCONNECT_SS)
            else:
                btn.setText("Connect")
                btn.setStyleSheet(_CONNECT_SS)

        # Status cell
        status_container = self._cfg_table.cellWidget(row, _COL_STATUS)
        if status_container:
            status_item = status_container.findChild(QLabel)
            if status_item:
                if connecting:
                    status_item.setText("Connecting…")
                    status_item.setStyleSheet("color: #F2A13A; background: transparent; padding-left: 4px;")  # amber
                elif connected:
                    status_item.setText("Connected")
                    status_item.setStyleSheet(f"color: {GREEN}; background: transparent; padding-left: 4px;")
                else:
                    status_item.setText("Disconnected")
                    status_item.setStyleSheet(f"color: {RED}; background: transparent; padding-left: 4px;")

    def _reindex_action_buttons(self) -> None:
        """After row deletion, update each action button's captured row index."""
        for row in range(self._cfg_table.rowCount()):
            btn = self._get_action_button(row)
            if btn is not None:
                try:
                    btn.clicked.disconnect()
                except RuntimeError:
                    pass
                btn.clicked.connect(
                    lambda checked, r=row: self._on_action_clicked(r))

    def _get_action_button(self, row: int) -> QPushButton | None:
        """Return the QPushButton inside the Action column's container widget."""
        container = self._cfg_table.cellWidget(row, _COL_ACTION)
        if container is None:
            return None
        for child in container.children():
            if isinstance(child, QPushButton):
                return child
        return None

    # ── Config persistence ────────────────────────────────────────────────────

    def _auto_save(self, *_args: Any) -> None:
        rows = self._collect_config_rows()
        config_manager.save_connections(rows)
        
        # Real-time sync: if auto_reconnect was toggled, update running thread
        for r in rows:
            label = r.get("label")
            if label and label in self._active_threads:
                self._active_threads[label].set_auto_reconnect(r.get("auto_reconnect", True))

    def _persist_splitter(self) -> None:
        """Save the top/bottom split so the Connect page reopens with the
        same proportions next launch."""
        layout = config_manager.load_layout()
        layout[_K_CONNECT_SPLIT] = self._splitter.sizes()
        config_manager.save_layout(layout)
        log.debug("Connect splitter layout saved")

    def _load_config(self) -> None:
        rows = config_manager.load_connections()
        log.info("Loading %d connection row(s) from config", len(rows))
        for r in rows:
            raw_port = r.get("port", 7497)
            raw_cid = r.get("client_id", "")
            try:
                port = int(raw_port) if str(raw_port).strip() else 7497
            except (ValueError, TypeError):
                port = 7497
            try:
                client_id = int(raw_cid) if str(raw_cid).strip(
                ) else config_manager.get_next_client_id()
            except (ValueError, TypeError):
                client_id = config_manager.get_next_client_id()
            saved_type = r.get("type", "")
            label = r.get("label", "")
            # Re-hydrate the detected-types dict so the saved type is preserved
            # for future JSON writes — but do NOT show it in the visual label yet.
            # The label stays blank until the connection is actually established.
            if label and saved_type in ("Paper", "Live"):
                self._detected_types[label] = saved_type
            self._add_config_row(
                label=label,
                host=r.get("host", "127.0.0.1"),
                port=port,
                client_id=client_id,
                conn_type="",          # always blank on startup
                priority=int(r.get("priority", 1)),
                auto=bool(r.get("auto", False)),
                auto_reconnect=bool(r.get("auto_reconnect", True)),
            )

    def _collect_config_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in range(self._cfg_table.rowCount()):
            auto_cb = self._get_auto_checkbox(row)
            recon_cb = self._get_reconnect_checkbox(row)
            pri_combo = self._get_priority_combo(row)
            label = self._row_text(row, _COL_LABEL)
            # Use the authoritative detected type (not the visual label, which
            # can be transiently blank while connecting / disconnected).
            saved_type = self._detected_types.get(label, "")
            rows.append({
                "label": label,
                "host": self._row_text(row, _COL_HOST),
                "port": self._row_text(row, _COL_PORT),
                "client_id": self._row_text(row, _COL_CID),
                "type": saved_type,
                "priority": int(pri_combo.currentText()) if pri_combo else 1,
                "auto": auto_cb.isChecked() if auto_cb else False,
                "auto_reconnect": recon_cb.isChecked() if recon_cb else True,
            })
        return rows

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _row_label(self, row: int) -> str:
        return self._row_text(row, _COL_LABEL)

    def _row_text(self, row: int, col: int) -> str:
        item = self._cfg_table.item(row, col)
        return item.text().strip() if item else ""

    def _get_type_label(self, row: int) -> QLabel | None:
        """Return the QLabel inside the Type column's container widget."""
        container = self._cfg_table.cellWidget(row, _COL_TYPE)
        if container is None:
            return None
        lbl = container.findChild(QLabel, "type_label")
        return lbl

    def _get_priority_combo(self, row: int) -> QComboBox | None:
        widget = self._cfg_table.cellWidget(row, _COL_PRIORITY)
        return widget if isinstance(widget, QComboBox) else None

    def _get_auto_checkbox(self, row: int) -> _ToggleSwitch | None:
        container = self._cfg_table.cellWidget(row, _COL_AUTO)
        if container is None:
            return None
        for child in container.children():
            if isinstance(child, _ToggleSwitch):
                return child
        return None

    def _get_reconnect_checkbox(self, row: int) -> _ToggleSwitch | None:
        container = self._cfg_table.cellWidget(row, _COL_AUTO_RECONNECT)
        if container is None:
            return None
        for child in container.children():
            if isinstance(child, _ToggleSwitch):
                return child
        return None

    # ── Auto-connect on show ──────────────────────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # On first show, auto-connect rows with Auto checked
        if not hasattr(self, "_auto_connected"):
            self._auto_connected = True
            for row in range(self._cfg_table.rowCount()):
                cb = self._get_auto_checkbox(row)
                label = self._row_label(row)
                if cb and cb.isChecked() and label and label not in self._active_threads:
                    log.info("Auto-connecting %r on first show", label)
                    self._connect_row(row)
