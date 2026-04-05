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

from .order_status_model import (
    # Column constants
    COL_SYMBOL, COL_ACCOUNT, COL_ACTION, COL_TYPE, COL_QTY,
    COL_FILL, COL_STATUS, COL_CANCEL, COL_TIME, COL_LMT, COL_STP, COL_TIF,
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

# ── Theme tokens ──────────────────────────────────────────────────────────────
BG_PANEL = "#1e2230"
BG_TABLE = "#1A1D24"
BG_TABLE_ALT = "#1E222A"
BG_HEADER = "#14171D"
BORDER = "#30363D"
TEXT = "#C9D1D9"
TEXT_DIM = "#8B949E"

CLR_OPEN = "#58A6FF"
CLR_CANCEL_BTN = "#3A1A1A"
CLR_CANCEL_BTN_HOVER = "#4A2424"
CLR_CANCEL_BTN_PRESSED = "#5A2A2A"
CLR_CANCEL_BTN_BORDER = "#4A2A2A"

# ── Fonts ─────────────────────────────────────────────────────────────────────
_TITLE_FONT = QFont("Helvetica Neue, Arial", 11)
_TITLE_FONT.setWeight(QFont.Weight.Bold)

_TABLE_FONT = QFont("Menlo, Consolas, monospace", 11)

# ── Table QSS (applied to QTableView) ────────────────────────────────────────
_TABLE_STYLE = f"""
    QTableView {{
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
    QTableView::item {{
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pressed_row: int | None = None

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

        # Fill cell background (external-order bg, selection, alternating rows)
        bg = index.data(Qt.ItemDataRole.BackgroundRole)
        if bg:
            painter.fillRect(option.rect, bg)
        elif option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor("#253545"))
        elif option.features & QStyleOptionViewItem.ViewItemFeature.Alternate:
            painter.fillRect(option.rect, QColor(BG_TABLE_ALT))

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
        painter.setFont(QFont("Helvetica Neue, Arial", 10, QFont.Weight.Bold))
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
    """

    cancel_requested = Signal(str)   # order_id
    refresh_requested = Signal()     # user clicked Refresh

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
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(True)
        self._table.setFont(_TABLE_FONT)
        self._table.setStyleSheet(_TABLE_STYLE)

        # Enable mouse tracking so the delegate receives hover states
        self._table.viewport().setMouseTracking(True)

        # Cancel button delegate
        self._cancel_delegate = CancelButtonDelegate(self._table)
        self._cancel_delegate.cancel_clicked.connect(self._on_cancel_clicked)
        self._table.setItemDelegateForColumn(COL_CANCEL, self._cancel_delegate)

        # Header setup
        hdr = self._table.horizontalHeader()
        hdr.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        # Restore saved column widths or apply defaults
        self._restore_table_state()

        self._table.verticalHeader().setDefaultSectionSize(30)
        root.addWidget(self._table, stretch=1)

    # ── Column state persistence (QSettings) ──────────────────────────────────

    _SETTINGS_KEY = "OrderStatusPanel/headerState"

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
        pad = fm.horizontalAdvance("W")

        col_samples = {
            COL_SYMBOL:  "WWWWWW",
            COL_ACCOUNT: "WWWWWWWWW",
            COL_ACTION:  "WWWWW",
            COL_TYPE:    "WWWWW",
            COL_QTY:     "9999",
            COL_FILL:    "999999999",
            COL_STATUS:  "WWWWWWWWWW",
            COL_CANCEL:  "WWWWWWWW",
            COL_TIME:    "00:00:00",
            COL_LMT:     "999999999",
            COL_STP:     "999999999",
            COL_TIF:     "WWWW",
        }

        for col, sample in col_samples.items():
            w = fm.horizontalAdvance(sample) + pad
            self._table.setColumnWidth(col, w)

    # ── Filter bar ────────────────────────────────────────────────────────────

    def _apply_filter_styles(self) -> None:
        active = self._proxy.active_filter
        for label, btn in self._filter_btns.items():
            if label == active:
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
        for lbl, btn in self._filter_btns.items():
            btn.setChecked(lbl == label)
        self._proxy.set_filter(label)
        self._apply_filter_styles()

    # ── Public data API (pass-through to model) ──────────────────────────────

    def add_order(self, order_id: str, data: dict[str, Any]) -> None:
        self._model.add_order(order_id, data)

    def update_order(self, order_id: str, data: dict[str, Any]) -> None:
        self._model.update_order(order_id, data)

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

