"""
AccountPositionPanel – grouped portfolio positions table (QTableView).

Public API
──────────
update_positions(batch)                  – ingest position dicts from worker
update_pnl_single(account, con_id, ...) – per-position P&L
set_account_data(account, ...)           – account-level data (Net Liq, etc.)
clear_positions()                        – remove everything
get_position_keys()                      – active (account, conId) keys

Signals
───────
refresh_requested()           – user clicked ⟳
flatten_requested(str)        – user clicked Flatten (account id)
"""
from __future__ import annotations

import logging
import math

from PySide6.QtCore import QEvent, QRect, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from .account_position_model import (
    AccountPositionModel,
    IsFirstInGroupRole,
    RowTypeRole, FlattenRole, HeaderInfoRole, PositionKeyRole,
    PositionRole, AccountHasPositionsRole,
    ROW_TYPE_HEADER, ROW_TYPE_POSITION,
    COL_ACCOUNT, COL_SYMBOL, COL_POSITION,
    COL_AVG_PRICE, COL_LAST_PRICE,
    COL_REALIZED_PNL, COL_UNREALIZED_PNL, COL_MARKET_VALUE,
    COL_FLATTEN,
    NUM_COLS, TEXT_DIM,
    BG_HEADER_ROW,
    _fmt_pnl, _fmt_money, _pnl_color,
)
from .connections_view import _ToggleSwitch

log = logging.getLogger(__name__)

# ── Theme tokens ──────────────────────────────────────────────────────────────
BG_PANEL     = "#1e2230"
BG_TABLE     = "#1A1D24"
BG_TABLE_ALT = "#1E222A"
BG_HEADER    = "#14171D"
BORDER       = "#30363D"
TEXT         = "#C9D1D9"
TEXT_DIM_PNL = "#8B949E"
CLR_OPEN     = "#58A6FF"

# Flatten button colours
CLR_FLATTEN_BG      = "#1A2A1A"
CLR_FLATTEN_HOVER   = "#1F3820"
CLR_FLATTEN_PRESSED = "#254825"
CLR_FLATTEN_BORDER  = "#2A4A2A"
CLR_FLATTEN_TEXT    = "#FF9800"   # orange – caution action

_TITLE_FONT = QFont("Helvetica Neue, Arial", 11)
_TITLE_FONT.setWeight(QFont.Weight.Bold)

_TABLE_FONT = QFont("Menlo, Consolas, monospace", 11)
_INFO_LABEL_FONT  = QFont("Menlo, Consolas, monospace", 10)
_INFO_VALUE_FONT  = QFont("Menlo, Consolas, monospace", 10, QFont.Weight.Bold)

# ── QSS ───────────────────────────────────────────────────────────────────────
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

# ── Column-width persistence ───────────────────────────────────────────────────
_BG_HEADER_ROW_BRUSH = QBrush(QColor(BG_HEADER_ROW))


# ══════════════════════════════════════════════════════════════════════════════
#  PortfolioRowDelegate  – forces header-row background on all plain columns
# ══════════════════════════════════════════════════════════════════════════════

class PortfolioRowDelegate(QStyledItemDelegate):
    """Forces the BG_HEADER_ROW background on account header rows for any
    column not handled by a specialised per-column delegate."""

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:
        super().initStyleOption(option, index)
        if index.data(RowTypeRole) == ROW_TYPE_HEADER:
            option.backgroundBrush = _BG_HEADER_ROW_BRUSH
            option.features = (
                option.features
                & ~QStyleOptionViewItem.ViewItemFeature.Alternate
            )


# ══════════════════════════════════════════════════════════════════════════════
#  AccountHeaderInfoDelegate – merged info cell (cols 1-7 spanned)
# ══════════════════════════════════════════════════════════════════════════════

class AccountHeaderInfoDelegate(QStyledItemDelegate):
    """For HEADER rows: paints "Daily P&L · Net Liq · Excess Liq" inline
    across the spanned cell.  For POSITION rows: normal symbol rendering."""

    _LABEL_COLOR  = QColor(TEXT_DIM)
    _GAP_LBL_VAL  = 4    # px between "Label:" and value
    _GAP_SEGMENTS = 20    # px between categories

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:
        super().initStyleOption(option, index)
        if index.data(RowTypeRole) == ROW_TYPE_HEADER:
            option.backgroundBrush = _BG_HEADER_ROW_BRUSH
            option.features = (
                option.features
                & ~QStyleOptionViewItem.ViewItemFeature.Alternate
            )

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index) -> None:
        if index.data(RowTypeRole) != ROW_TYPE_HEADER:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.fillRect(option.rect, QColor(BG_HEADER_ROW))

        ad           = index.data(HeaderInfoRole) or {}
        daily_pnl    = ad.get("daily_pnl",    float("nan"))
        net_liq      = ad.get("net_liq",      0.0)
        excess_liq   = ad.get("excess_liq",   0.0)
        buying_power = ad.get("buying_power", 0.0)

        # Header-specific format: always show a value (including +$0)
        if math.isfinite(daily_pnl):
            v = int(round(daily_pnl))
            daily_str = f"+${v:,}" if v >= 0 else f"-${abs(v):,}"
        else:
            daily_str = "–"
        net_str    = _fmt_money(net_liq)
        excess_str = _fmt_money(excess_liq)
        bp_str     = _fmt_money(buying_power)

        # (label, value, value_color)
        segments = [
            ("Daily P&L", daily_str,  _pnl_color(daily_pnl)),
            ("Net",       net_str,    QColor(TEXT)),
            ("Excess",    excess_str, QColor(TEXT)),
            ("BP",        bp_str,     QColor(TEXT)),
        ]

        rect = option.rect
        x = rect.left() + 8
        y, h = rect.top(), rect.height()

        for i, (lbl, val, v_color) in enumerate(segments):
            # "Label:" — use painter.fontMetrics() for accurate device metrics
            painter.setFont(_INFO_LABEL_FONT)
            painter.setPen(self._LABEL_COLOR)
            fm = painter.fontMetrics()
            baseline = y + (h + fm.ascent() - fm.descent()) // 2
            lbl_text = lbl + ":"
            painter.drawText(x, baseline, lbl_text)
            x += fm.horizontalAdvance(lbl_text) + self._GAP_LBL_VAL

            # Value
            painter.setFont(_INFO_VALUE_FONT)
            painter.setPen(v_color)
            fm = painter.fontMetrics()
            baseline = y + (h + fm.ascent() - fm.descent()) // 2
            painter.drawText(x, baseline, val)
            x += fm.horizontalAdvance(val)

            if i < len(segments) - 1:
                x += self._GAP_SEGMENTS

        painter.restore()


# ── Position-row action buttons definition ────────────────────────────────────
# (label, bg_normal, bg_hover, bg_pressed, pct)   pct=100 → full flatten
_POS_BTNS = [
    ("Flatten", "#cc6600", "#d97a00", "#b35800", 100),
    ("25%",     "#8E8E93", "#9E9EA3", "#7E7E83",  25),
    ("50%",     "#8E8E93", "#9E9EA3", "#7E7E83",  50),
    ("75%",     "#8E8E93", "#9E9EA3", "#7E7E83",  75),
]
_POS_BTN_H_PAD  = 7
_POS_BTN_V_PAD  = 2
_POS_BTN_GAP    = 4
_POS_BTN_RADIUS = 3


# ══════════════════════════════════════════════════════════════════════════════
#  FlattenButtonDelegate
#    • Header rows  → single orange "Flatten" button
#    • Position rows → four buttons: Flatten · 25% · 50% · 75%
# ══════════════════════════════════════════════════════════════════════════════

class FlattenButtonDelegate(QStyledItemDelegate):
    """Action-button column delegate for the portfolio table."""

    # header-row signal
    flatten_clicked          = Signal(str)        # account
    # position-row signal  (account, con_id, pct)  pct=100 → full flatten
    position_close_clicked   = Signal(str, int, int)

    # ── header-row button geometry ────────────────────────────────────────────
    _HDR_TEXT    = "Flatten"
    _HDR_H_PAD   = 8
    _HDR_V_PAD   = 2
    _HDR_RADIUS  = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pressed_row: int | None = None
        self._pressed_btn: int | None = None   # index into _POS_BTNS (position rows)

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _is_header(index) -> bool:
        return index.data(RowTypeRole) == ROW_TYPE_HEADER

    @staticmethod
    def _is_position(index) -> bool:
        return index.data(RowTypeRole) == ROW_TYPE_POSITION

    def _hdr_button_rect(self, cell_rect: QRect, fm: QFontMetrics) -> QRect:
        tw = fm.horizontalAdvance(self._HDR_TEXT) + 2 * self._HDR_H_PAD
        th = int((fm.height() + 2 * self._HDR_V_PAD) * 1.4)
        x  = cell_rect.x() + 6
        y  = cell_rect.y() + (cell_rect.height() - th) // 2
        return QRect(x, y, tw, th)

    def _pos_button_rects(self, cell_rect: QRect,
                          fm: QFontMetrics) -> list[QRect]:
        """Return a list of QRect for each position-row action button.
        All buttons share the same width (widest label wins)."""
        btn_w   = max(fm.horizontalAdvance(lbl) + 2 * _POS_BTN_H_PAD
                      for lbl, *_ in _POS_BTNS)
        n       = len(_POS_BTNS)
        total_w = btn_w * n + _POS_BTN_GAP * (n - 1)
        btn_h   = min(int((fm.height() + 2 * _POS_BTN_V_PAD) * 1.4),
                      cell_rect.height() - 4)

        x = cell_rect.x() + 6
        y = cell_rect.y() + (cell_rect.height() - btn_h)  // 2

        rects = []
        for _ in _POS_BTNS:
            rects.append(QRect(x, y, btn_w, btn_h))
            x += btn_w + _POS_BTN_GAP
        return rects

    # ── paint ─────────────────────────────────────────────────────────────────

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index) -> None:
        painter.save()

        # ── background ───────────────────────────────────────────────────
        if self._is_header(index):
            painter.fillRect(option.rect, QColor(BG_HEADER_ROW))
        elif option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor("#253545"))
        elif option.features & QStyleOptionViewItem.ViewItemFeature.Alternate:
            painter.fillRect(option.rect, QColor(BG_TABLE_ALT))
        else:
            painter.fillRect(option.rect, QColor(BG_TABLE))

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fm = QFontMetrics(option.font)

        # ── HEADER ROW: single Flatten button ────────────────────────────
        if self._is_header(index):
            # Don't show Flatten when the account has no open positions
            if not index.data(AccountHasPositionsRole):
                painter.restore()
                return

            btn_rect = self._hdr_button_rect(option.rect, fm)
            hovered  = bool(option.state & QStyle.StateFlag.State_MouseOver)
            pressed  = (self._pressed_row == index.row()
                        and self._pressed_btn == -1)

            # Reuse the same colours as the position-row Flatten button
            _lbl, bg_n, bg_h, bg_p, _pct = _POS_BTNS[0]
            bg = (QColor(bg_p) if pressed
                  else QColor(bg_h) if hovered
                  else QColor(bg_n))

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(btn_rect, self._HDR_RADIUS, self._HDR_RADIUS)

            painter.setPen(QColor("#ffffff"))
            painter.setFont(QFont("Helvetica Neue, Arial", 10, QFont.Weight.Bold))
            painter.drawText(btn_rect, Qt.AlignmentFlag.AlignCenter,
                             self._HDR_TEXT)

            painter.restore()
            return

        # ── POSITION ROW: Flatten · 25% · 50% · 75% ─────────────────────
        if self._is_position(index):
            # Don't show action buttons when position is flat
            if index.data(PositionRole) == 0:
                painter.restore()
                return

            rects   = self._pos_button_rects(option.rect, fm)
            hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
            btn_font = QFont("Helvetica Neue, Arial", 10, QFont.Weight.Bold)

            for i, ((lbl, bg_n, bg_h, bg_p, _pct), rect) in enumerate(
                    zip(_POS_BTNS, rects)):
                pressed_this = (self._pressed_row == index.row()
                                and self._pressed_btn == i)
                bg = (QColor(bg_p) if pressed_this
                      else QColor(bg_h) if hovered
                      else QColor(bg_n))

                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(bg)
                painter.drawRoundedRect(rect, _POS_BTN_RADIUS, _POS_BTN_RADIUS)

                painter.setPen(QColor("#ffffff"))
                painter.setFont(btn_font)
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, lbl)

        painter.restore()

    # ── interaction ───────────────────────────────────────────────────────────

    def editorEvent(self, event: QEvent, model,
                    option: QStyleOptionViewItem, index) -> bool:
        etype = event.type()

        # ── header row ────────────────────────────────────────────────────
        if self._is_header(index):
            if not index.data(AccountHasPositionsRole):
                return False
            if etype == QEvent.Type.MouseButtonPress:
                self._pressed_row = index.row()
                self._pressed_btn = -1
                return True
            if etype == QEvent.Type.MouseButtonRelease:
                was = (self._pressed_row == index.row()
                       and self._pressed_btn == -1)
                self._pressed_row = None
                self._pressed_btn = None
                if was:
                    acct = index.data(FlattenRole)
                    if acct:
                        self.flatten_clicked.emit(acct)
                return was

        # ── position row ──────────────────────────────────────────────────
        if self._is_position(index):
            if index.data(PositionRole) == 0:
                return False
            fm    = QFontMetrics(option.font)
            rects = self._pos_button_rects(option.rect, fm)
            pos   = event.position().toPoint() if hasattr(event, "position") \
                    else event.pos()

            if etype == QEvent.Type.MouseButtonPress:
                for i, rect in enumerate(rects):
                    if rect.contains(pos):
                        self._pressed_row = index.row()
                        self._pressed_btn = i
                        return True
                return False

            if etype == QEvent.Type.MouseButtonRelease:
                was_row = self._pressed_row == index.row()
                was_btn = self._pressed_btn
                self._pressed_row = None
                self._pressed_btn = None
                if was_row and was_btn is not None:
                    for i, rect in enumerate(rects):
                        if i == was_btn and rect.contains(pos):
                            key = index.data(PositionKeyRole)
                            if key:
                                acct, con_id = key
                                _, _, _, _, pct = _POS_BTNS[i]
                                self.position_close_clicked.emit(
                                    acct, con_id, pct)
                            return True
                return False

        return False

    # ── size hint ─────────────────────────────────────────────────────────────

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        fm = QFontMetrics(option.font)
        if self._is_position(index):
            btn_w   = max(fm.horizontalAdvance(lbl) + 2 * _POS_BTN_H_PAD
                          for lbl, *_ in _POS_BTNS)
            total_w = btn_w * len(_POS_BTNS) + _POS_BTN_GAP * (len(_POS_BTNS) - 1) + 16
            btn_h   = int((fm.height() + 2 * _POS_BTN_V_PAD) * 1.5)
            return QSize(total_w, btn_h + 4)
        # header row
        tw = fm.horizontalAdvance(self._HDR_TEXT) + 2 * self._HDR_H_PAD + 8
        th = int((fm.height() + 2 * self._HDR_V_PAD) * 1.4) + 4
        return QSize(tw, th)


# ══════════════════════════════════════════════════════════════════════════════
#  AccountPositionPanel
# ══════════════════════════════════════════════════════════════════════════════

class AccountPositionPanel(QWidget):
    """Grouped portfolio positions table with per-account header rows."""

    position_action_requested = Signal(dict)
    account_action_requested  = Signal(dict)
    refresh_requested         = Signal()
    flatten_requested         = Signal(str)          # account id (header row Flatten)
    position_close_requested  = Signal(str, int, int)  # account, con_id, pct (100=flatten)

    def __init__(self, accounts_model=None, parent=None):
        super().__init__(parent)
        self._accounts_model = accounts_model
        self._model = AccountPositionModel(
            get_alias=self._get_alias, parent=self)
        self._setup_ui()
        # Sync model with persisted toggle state (model defaults to True)
        self._model.set_show_zero(self._zero_pos_toggle.isChecked())

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setObjectName("AccountPositionPanel")
        self.setStyleSheet(f"""
            QWidget#AccountPositionPanel {{
                background: {BG_PANEL};
                border: 1px solid {BORDER};
                border-radius: 4px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # ── Title bar ────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.setSpacing(0)
        title = QLabel("Positions")
        title.setFont(_TITLE_FONT)
        title.setStyleSheet("color: #ffffff; border: none;")
        hdr.addWidget(title)

        btn = QPushButton("⟳")
        btn.setToolTip("Refresh positions")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(26, 26)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {TEXT_DIM_PNL};
                border: 1px solid {BORDER}; border-radius: 4px;
                font-size: 14px; font-weight: bold;
            }}
            QPushButton:hover {{ color: #ffffff; border-color: {CLR_OPEN}; background: #253545; }}
            QPushButton:pressed {{ background: #1a2535; }}
        """)
        btn.clicked.connect(self._on_refresh_clicked)
        hdr.addWidget(btn)

        hdr.addStretch(1)

        # Zero-position toggle  (right corner, default ON)
        zero_lbl = QLabel("Zero Pos")
        zero_lbl.setStyleSheet(
            f"color: {TEXT_DIM_PNL}; font-size: 10px; border: none;")
        hdr.addWidget(zero_lbl)
        hdr.addSpacing(4)

        _saved_zero = QSettings("NanoPulse", "Dashboard").value(
            "AccountPositionPanel/showZeroPos", True, type=bool)
        self._zero_pos_toggle = _ToggleSwitch(checked=_saved_zero)
        self._zero_pos_toggle.toggled.connect(self._on_zero_pos_toggled)
        hdr.addWidget(self._zero_pos_toggle)

        root.addLayout(hdr)

        # ── Table ────────────────────────────────────────────────────────
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setFont(_TABLE_FONT)
        self._table.setStyleSheet(_TABLE_STYLE)
        self._table.verticalHeader().setDefaultSectionSize(34)
        self._table.viewport().setMouseTracking(True)

        # Global delegate – forces header-row background on ordinary columns
        self._row_delegate = PortfolioRowDelegate(self._table)
        self._table.setItemDelegate(self._row_delegate)

        # Merged info cell delegate (COL_SYMBOL, spanned 1→7 on header rows)
        self._info_delegate = AccountHeaderInfoDelegate(self._table)
        self._table.setItemDelegateForColumn(COL_SYMBOL, self._info_delegate)

        # Flatten button on the last column
        self._flatten_delegate = FlattenButtonDelegate(self._table)
        self._flatten_delegate.flatten_clicked.connect(self._on_flatten_clicked)
        self._flatten_delegate.position_close_clicked.connect(
            self._on_position_close_clicked)
        self._table.setItemDelegateForColumn(COL_FLATTEN, self._flatten_delegate)

        # Header
        h = self._table.horizontalHeader()
        h.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setStretchLastSection(True)

        self._restore_col_widths()
        h.sectionResized.connect(self._on_section_resized)

        # Apply column spans whenever model is rebuilt
        self._model.modelReset.connect(self._apply_spans)

        root.addWidget(self._table, stretch=1)

    # ── Column spans ─────────────────────────────────────────────────────────

    def _apply_spans(self) -> None:
        """Merge columns 1-7 into one cell for every account header row.

        clearSpans() is called first to remove all previous spans in one shot.
        Resetting individual cells with setSpan(row, col, 1, 1) triggers
        Qt's "single cell span won't be added" warning for every position row.
        """
        self._table.clearSpans()
        span_cols = COL_FLATTEN - COL_SYMBOL  # = 7
        for row in range(self._model.rowCount()):
            idx = self._model.index(row, COL_ACCOUNT)
            if idx.data(RowTypeRole) == ROW_TYPE_HEADER:
                self._table.setSpan(row, COL_SYMBOL, 1, span_cols)

    # ── Column-width persistence ──────────────────────────────────────────────

    _COL_SAMPLES = {
        COL_ACCOUNT:        "WWWWWWWWW",
        COL_SYMBOL:         "WWWWWWW",
        COL_POSITION:       "999999",
        COL_UNREALIZED_PNL: "-9999.99",
        COL_AVG_PRICE:      "99999.99",
        COL_LAST_PRICE:     "99999.99",
        COL_REALIZED_PNL:   "-9999.99",
        COL_MARKET_VALUE:   "$999,999",
        COL_FLATTEN:        "Flatten  25%  50%  75% __",
    }

    def _apply_default_col_widths(self) -> None:
        fm  = QFontMetrics(self._table.font())
        pad = fm.horizontalAdvance("W")
        for col, sample in self._COL_SAMPLES.items():
            self._table.setColumnWidth(col, fm.horizontalAdvance(sample) + pad)

    _SETTINGS_KEY = "AccountPositionPanel/headerState"

    def _restore_col_widths(self) -> None:
        settings = QSettings("NanoPulse", "Dashboard")
        state = settings.value(self._SETTINGS_KEY)
        if state is not None:
            if self._table.horizontalHeader().restoreState(state):
                log.debug("Position: restored header state from QSettings")
                return
        self._apply_default_col_widths()
        log.debug("Position: applied default column widths")

    def save_col_widths(self) -> None:
        settings = QSettings("NanoPulse", "Dashboard")
        state = self._table.horizontalHeader().saveState()
        settings.setValue(self._SETTINGS_KEY, state)
        log.debug("Position: saved header state to QSettings")

    def _on_section_resized(self, _logical: int, _old: int, _new: int) -> None:
        self.save_col_widths()

    # ── Public API ────────────────────────────────────────────────────────────

    def update_positions(self, batch: list[dict]) -> None:
        self._model.update_positions(batch)

    def update_pnl_single(self, account: str, con_id: int,
                          daily_pnl: float, realized_pnl: float,
                          unrealized_pnl: float) -> None:
        self._model.update_pnl_single(
            account, con_id, daily_pnl, realized_pnl, unrealized_pnl)

    def set_account_data(self, account: str, balance: float = 0,
                         pnl: float = 0, **kwargs) -> None:
        self._model.set_account_data(
            account,
            net_liq=kwargs.get("net_liq", balance),
            excess_liq=kwargs.get("excess_liq", 0),
            buying_power=kwargs.get("buying_power", 0),
            daily_pnl=kwargs.get("daily_pnl", float("nan")),
            realized_pnl=kwargs.get("realized_pnl", float("nan")),
            unrealized_pnl=kwargs.get("unrealized_pnl", float("nan")),
        )

    def clear_positions(self) -> None:
        self._model.clear()

    def get_position_keys(self) -> list[tuple[str, int]]:
        return self._model.get_position_keys()

    def get_symbol_for_position(self, account: str, con_id: int) -> str:
        """Return localSymbol (or root symbol) for the given (account, conId)."""
        return self._model.get_symbol_for_position(account, con_id)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_refresh_clicked(self) -> None:
        log.info("Portfolio: refresh requested")
        self.clear_positions()
        self.refresh_requested.emit()

    def _on_zero_pos_toggled(self, checked: bool) -> None:
        self._model.set_show_zero(checked)
        QSettings("NanoPulse", "Dashboard").setValue(
            "AccountPositionPanel/showZeroPos", checked)

    def _on_flatten_clicked(self, account: str) -> None:
        log.info("Portfolio: flatten requested for %s", account)
        self.flatten_requested.emit(account)

    def _on_position_close_clicked(self, account: str, con_id: int,
                                   pct: int) -> None:
        log.info("Portfolio: close %d%% requested – %s conId=%d",
                 pct, account, con_id)
        self.position_close_requested.emit(account, con_id, pct)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_alias(self, account: str) -> str:
        if self._accounts_model is not None:
            return self._accounts_model._aliases.get(account, "")
        return ""
