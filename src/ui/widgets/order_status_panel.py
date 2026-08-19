"""
OrderStatusPanel – live order blotter (refactored to proper MVC).

Uses ``OrderStatusModel`` (QAbstractTableModel) for data,
``OrderStatusFilterProxy`` for status filtering, a ``QTableView``
for presentation, and a ``CancelButtonDelegate`` for the inline
Cancel button column.

Public API (unchanged from the original QTableWidget version)
─────────────────────────────────────────────────────────────
add_order(order_id, data)    – insert or update a row
update_order(order_id, data) – alias for add_order
remove_order(order_id)       – delete a row
clear_orders()               – remove all rows
save_table_state()           – persist column widths to QSettings

Signals
───────
cancel_requested(str)        – order_id when Cancel button is clicked
refresh_requested()          – user clicked ⟳ Refresh
"""
from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, Signal, QEvent, QModelIndex, QSettings, QRect, QSize
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.ui.theme import Theme
from . import table_theme as tt
from .order_status_model import (
    # Column constants
    COL_SYMBOL, COL_ACCOUNT, COL_ACTION, COL_TYPE, COL_QTY,
    COL_FILL, COL_STATUS, COL_CANCEL, COL_TIME, COL_LMT, COL_STP, COL_TIF,
    HEADERS,
    # Roles
    OrderIdRole, IsInternalRole, StatusCategoryRole,
    # Filter constants
    FILTER_ALL, FILTER_OPEN,
    FILTERS,
    # Model + Proxy
    OrderStatusModel, OrderStatusFilterProxy,
    # Colours
    CLR_SELL,
)

log = logging.getLogger(__name__)

# ── Theme ─────────────────────────────────────────────────────────────────────
# The table look is shared with the Positions and Trades blotters — see
# table_theme; only what this panel paints itself lives here.
BG_PANEL = tt.BG_PAGE
BG_TABLE = tt.BG_TABLE
BORDER = tt.BORDER
TEXT = tt.TEXT
TEXT_DIM = tt.TEXT_MUTED
SELECTION_BG = tt.SELECTION_BG

CLR_OPEN = tt.ACCENT
# Cancel button — the destructive action in this table, so it borrows the
# close control's red rather than inventing another one.
CLR_CANCEL_BTN = "#3A1A1A"
CLR_CANCEL_BTN_HOVER = Theme.POS_LOSS_HOVER
CLR_CANCEL_BTN_PRESSED = "#8B2A28"
CLR_CANCEL_BTN_BORDER = "#4A2A2A"

_TITLE_FONT = QFont("Inter, Helvetica Neue, Arial", 11)
_TITLE_FONT.setWeight(QFont.Weight.Bold)

_TABLE_FONT = tt.TABLE_FONT
_TABLE_STYLE = tt.TABLE_QSS

# ══════════════════════════════════════════════════════════════════════════════
#  CancelButtonDelegate
# ══════════════════════════════════════════════════════════════════════════════

class CancelButtonDelegate(QStyledItemDelegate):
    """Paints an inline Cancel button for internal open orders.

    Emits ``cancel_clicked(order_id)`` on mouse-button release.
    """

    cancel_clicked = Signal(str)

    _BTN_TEXT = "Cancel"
    _BTN_H_PAD = 8
    _BTN_V_PAD = 2
    _BTN_RADIUS = 3

    def __init__(self, parent=None, *, hover_source=None):
        super().__init__(parent)
        self._pressed_row: int | None = None
        # Row-hover state lives on the table's default delegate; this one
        # reads it so the hovered row's fill runs through this column too.
        self._hover_source = hover_source

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _should_show_button(index: QModelIndex) -> bool:
        """Return True if this cell should display a Cancel button."""
        internal = index.data(IsInternalRole)
        cat = index.data(StatusCategoryRole)
        return bool(internal) and cat == FILTER_OPEN

    def _button_rect(self, cell_rect: QRect, fm: QFontMetrics) -> QRect:
        tw = fm.horizontalAdvance(self._BTN_TEXT) + 2 * self._BTN_H_PAD
        th = fm.height() + 2 * self._BTN_V_PAD
        x = cell_rect.x() + (cell_rect.width() - tw) // 2
        y = cell_rect.y() + (cell_rect.height() - th) // 2
        return QRect(x, y, tw, th)

    # ── paint ─────────────────────────────────────────────────────────────────

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex) -> None:
        painter.save()

        # Fill the cell background ourselves — this column is painted from
        # scratch, so it has to reproduce the row's state (selection, hover,
        # the external-order tint) or the row would break here.
        src = self._hover_source
        hovered_row = src.hover_row if src else None
        bg = index.data(Qt.ItemDataRole.BackgroundRole)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor(SELECTION_BG))
        elif index.row() == hovered_row:
            painter.fillRect(option.rect,
                             src.hover_brush if src else tt.ROW_HOVER_BRUSH)
        elif bg:
            painter.fillRect(option.rect, bg)
        else:
            painter.fillRect(option.rect, QColor(tt.BG_TABLE))

        tt.paint_separator_line(painter, option.rect)

        if not self._should_show_button(index):
            painter.restore()
            return

        fm = QFontMetrics(option.font)
        btn_rect = self._button_rect(option.rect, fm)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        pressed = (self._pressed_row is not None
                   and index.row() == self._pressed_row)

        if pressed:
            bg_color = QColor(CLR_CANCEL_BTN_PRESSED)
        elif hovered:
            bg_color = QColor(CLR_CANCEL_BTN_HOVER)
        else:
            bg_color = QColor(CLR_CANCEL_BTN)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(CLR_CANCEL_BTN_BORDER), 1))
        painter.setBrush(bg_color)
        painter.drawRoundedRect(btn_rect, self._BTN_RADIUS, self._BTN_RADIUS)

        painter.setPen(QColor(CLR_SELL))
        painter.setFont(QFont("Inter, Helvetica Neue, Arial", 10, QFont.Weight.Bold))
        painter.drawText(btn_rect, Qt.AlignmentFlag.AlignCenter, self._BTN_TEXT)

        painter.restore()

    # ── interaction ───────────────────────────────────────────────────────────

    def editorEvent(self, event: QEvent, model, option: QStyleOptionViewItem,
                    index: QModelIndex) -> bool:
        if not self._should_show_button(index):
            return False

        etype = event.type()

        if etype == QEvent.Type.MouseButtonPress:
            self._pressed_row = index.row()
            return True

        if etype == QEvent.Type.MouseButtonRelease:
            was_pressed = self._pressed_row == index.row()
            self._pressed_row = None
            if was_pressed:
                order_id = index.data(OrderIdRole)
                if order_id:
                    self.cancel_clicked.emit(order_id)
                    log.info("CancelButtonDelegate: clicked → %s", order_id)
                return True

        return False

    # ── size hint ─────────────────────────────────────────────────────────────

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        fm = QFontMetrics(option.font)
        w = fm.horizontalAdvance(self._BTN_TEXT) + 2 * self._BTN_H_PAD + 8
        h = fm.height() + 2 * self._BTN_V_PAD + 4
        return QSize(w, h)


# ══════════════════════════════════════════════════════════════════════════════
#  OrderStatusPanel
# ══════════════════════════════════════════════════════════════════════════════

class OrderStatusPanel(QWidget):
    """Live order blotter with MVC architecture.

    Public API
    ----------
    add_order(order_id, data)    – insert or update a row
    update_order(order_id, data) – alias for add_order
    remove_order(order_id)       – delete a row
    clear_orders()               – remove all rows
    save_table_state()           – persist column widths to QSettings

    Signals
    -------
    cancel_requested(str)        – emitted with the *order_id*
    refresh_requested()          – user clicked Refresh
    symbol_clicked(dict)         – user clicked a row's Symbol cell
    """

    cancel_requested = Signal(str)   # order_id
    refresh_requested = Signal()     # user clicked Refresh
    # Symbol-cell click — full order dict.  FlashTradeView routes this
    # to BookTraderView.focus_order (jump the ladder to the instrument).
    symbol_clicked = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)

        # ── Model layer ──────────────────────────────────────────────────
        self._model = OrderStatusModel(self)
        self._proxy = OrderStatusFilterProxy(self)
        self._proxy.setSourceModel(self._model)

        self._setup_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setObjectName("OrderStatusPanel")
        # Same card as the Positions and Trades panels.
        self.setStyleSheet(tt.panel_card_qss("OrderStatusPanel"))

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # ── Header row: title + refresh + filter buttons ─────────────────
        header = QHBoxLayout()
        header.setSpacing(0)

        title = QLabel("Orders")
        title.setFont(_TITLE_FONT)
        title.setStyleSheet("color: #ffffff; border: none;")
        header.addWidget(title)

        # Refresh button
        refresh_btn = QPushButton("⟳")
        refresh_btn.setToolTip("Refresh orders from TWS")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setFixedSize(28, 28)
        refresh_btn.setStyleSheet(tt.ICON_BTN_QSS)
        refresh_btn.clicked.connect(self._on_refresh_clicked)
        header.addSpacing(10)
        header.addWidget(refresh_btn)

        header.addStretch(1)
        header.setSpacing(tt.CTRL_SPACING)

        # Filter buttons
        self._filter_btns: dict[str, QPushButton] = {}
        for label in FILTERS:
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setCheckable(True)
            btn.setChecked(label == FILTER_ALL)
            btn.clicked.connect(
                lambda checked, lbl=label: self._on_filter_clicked(lbl))
            self._filter_btns[label] = btn
            header.addWidget(btn)

        self._apply_filter_styles()
        root.addLayout(header)

        # ── Table view ───────────────────────────────────────────────────
        self._table = QTableView()
        self._table.setModel(self._proxy)
        # Shared chrome: no zebra, no gridlines, uppercase captions, row
        # height, and the viewport mouse tracking the hover delegate needs.
        tt.apply_table_chrome(self._table)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        # Rows aren't click-selectable — hover is the only pointer cue,
        # matching the Positions and Trades blotters.
        tt.disable_click_selection(self._table)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)

        # Row hover + the 1px rule between rows.  No group banding here:
        # bracket parents and their TP/SL children are deliberately
        # opposite sides placed a moment apart, so any submission-based
        # grouping striped this table rather than clarifying it.
        # Also the source of truth for hover, which the Cancel column reads.
        self._row_delegate = tt.FooterAwareRowDelegate(self._table)
        self._table.setItemDelegate(self._row_delegate)

        # Cancel button delegate
        self._cancel_delegate = CancelButtonDelegate(
            self._table, hover_source=self._row_delegate)
        self._cancel_delegate.cancel_clicked.connect(self._on_cancel_clicked)
        self._table.setItemDelegateForColumn(COL_CANCEL, self._cancel_delegate)

        # Symbol-cell click → jump Book Trader to the order's instrument
        self._table.clicked.connect(self._on_cell_clicked)

        # Header setup
        hdr = self._table.horizontalHeader()
        hdr.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        # Restore saved column widths or apply defaults
        self._restore_table_state()

        root.addWidget(self._table, stretch=1)

    # ── Column state persistence (QSettings) ──────────────────────────────────

    # v2: the restyle widened cell padding and uppercased the captions, so
    # widths saved under the old key now truncate their own values.
    _SETTINGS_KEY = "OrderStatusPanel/headerState_v2"

    def _restore_table_state(self) -> None:
        settings = QSettings("NanoPulse", "Dashboard")
        state = settings.value(self._SETTINGS_KEY)
        if state is not None:
            if self._table.horizontalHeader().restoreState(state):
                log.debug("OrderStatus: restored header state from QSettings")
                return
        self._apply_font_metrics_widths()
        log.debug("OrderStatus: applied default QFontMetrics column widths")

    def save_table_state(self) -> None:
        settings = QSettings("NanoPulse", "Dashboard")
        state = self._table.horizontalHeader().saveState()
        settings.setValue(self._SETTINGS_KEY, state)
        log.debug("OrderStatus: saved header state to QSettings")

    def _apply_font_metrics_widths(self) -> None:
        fm = QFontMetrics(self._table.font())
        hdr_fm = QFontMetrics(tt.COL_HEADER_FONT)

        col_samples = {
            COL_SYMBOL:  "WWWWWW",
            COL_ACCOUNT: "WWWWWWWWW",
            COL_ACTION:  "WWWWW",
            COL_TYPE:    "STP LMT",
            COL_QTY:     "9999",
            COL_FILL:    "999999999",
            # Longest status TWS sends, plus the ⚠ an error row appends.
            COL_STATUS:  "PreSubmitted ⚠",
            COL_CANCEL:  "WWWWWWWW",
            COL_TIME:    "00:00:00",
            COL_LMT:     "999999999",
            COL_STP:     "999999999",
            COL_TIF:     "WWWW",
        }

        for col, sample in col_samples.items():
            self._table.setColumnWidth(
                col,
                tt.default_column_width(fm, hdr_fm, sample, HEADERS[col]))

    # ── Filter bar ────────────────────────────────────────────────────────────

    def _apply_filter_styles(self) -> None:
        """Filter pills use the shared outlined-control style, so the
        checked one fills with the accent exactly like the Summary /
        Zero Pos toggles in the other two blotters."""
        active = self._proxy.active_filter
        for label, btn in self._filter_btns.items():
            btn.setFixedHeight(26)
            btn.setChecked(label == active)
            btn.setStyleSheet(tt.CONTROL_BTN_QSS)

    def _on_filter_clicked(self, label: str) -> None:
        for lbl, btn in self._filter_btns.items():
            btn.setChecked(lbl == label)
        self._proxy.set_filter(label)
        self._apply_filter_styles()

    # ── Public data API (pass-through to model) ──────────────────────────────

    def add_order(self, order_id: str, data: dict[str, Any]) -> None:
        # New rows insert at the top (newest-first sort) and fills recolour
        # a row that may be scrolled out of view — snap back to the top so
        # the fresh activity is visible.
        is_new = not self._model.has_order(order_id)
        newly_filled = (
            not is_new
            and data.get("status") == "Filled"
            and self._model.order_status(order_id) != "Filled")
        self._model.add_order(order_id, data)
        if is_new or newly_filled:
            self._table.scrollToTop()

    def update_order(self, order_id: str, data: dict[str, Any]) -> None:
        self.add_order(order_id, data)

    def remove_order(self, order_id: str) -> None:
        self._model.remove_order(order_id)

    def clear_orders(self) -> None:
        self._model.clear()

    # ── Signal slots ──────────────────────────────────────────────────────────

    def _on_cancel_clicked(self, order_id: str) -> None:
        log.info("OrderStatus: cancel requested for order %s", order_id)
        self.cancel_requested.emit(order_id)

    def _on_refresh_clicked(self) -> None:
        log.info("OrderStatus: refresh requested by user")
        self.clear_orders()
        self.refresh_requested.emit()

    def _on_cell_clicked(self, idx: QModelIndex) -> None:
        """Symbol-column click → ``symbol_clicked`` with the order dict.
        Restricted to the Symbol cell so row selection elsewhere (and the
        Cancel button column) never retargets the Book Trader ladder."""
        if not idx.isValid() or idx.column() != COL_SYMBOL:
            return
        src = self._proxy.mapToSource(idx)
        order = self._model.order_at(src.row())
        if not order or not (order.get("symbol") or "").strip():
            return
        self.symbol_clicked.emit(dict(order))

