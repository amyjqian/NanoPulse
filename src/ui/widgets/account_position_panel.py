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
refresh_requested()                       – user clicked ⟳
position_close_requested(acct, cid, pct)  – row close control (pct=100 → flatten)
"""
from __future__ import annotations

import logging
import math

from PySide6.QtCore import (
    QEvent,
    QRect,
    QSettings,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from src.core import config_manager
from PySide6.QtGui import (
    QBrush, QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMenu,
    QPushButton,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from src.ui.theme import Theme
from . import table_theme as tt
from .account_position_model import (
    AccountPositionModel,
    IsFirstInGroupRole,
    RowTypeRole, FlattenRole, HeaderInfoRole, PositionKeyRole,
    PositionRole,
    ROW_TYPE_HEADER, ROW_TYPE_POSITION,
    COL_SYMBOL, COL_DAY_PNL, COL_POSITION,
    COL_AVG_PRICE, COL_LAST_PRICE,
    COL_REALIZED_PNL, COL_UNREALIZED_PNL, COL_MARKET_VALUE,
    COL_FLATTEN,
    HEADERS, NUM_COLS,
    BG_HEADER_ROW,
    _ACCT_FONT,
    _fmt_money, _pnl_color,
)
from .positions_summary_model import (
    PositionsSummaryModel,
    IsTotalRowRole,
    S_COL_KEYS as PS_COL_KEYS,
    S_COL_SYMBOL,
    S_HEADERS as PS_COL_HEADERS,
)

log = logging.getLogger(__name__)

# ── Theme tokens ──────────────────────────────────────────────────────────────
# The shared look lives in table_theme; only what this panel paints itself
# (the account band, the close control) is defined here.
BG_PAGE      = tt.BG_PAGE
BG_TABLE     = tt.BG_TABLE
BORDER       = tt.BORDER
OUTLINE      = tt.OUTLINE
OUTLINE_HOVER = tt.OUTLINE_HOVER
TEXT         = tt.TEXT
TEXT_2       = tt.TEXT_2
TEXT_MUTED   = tt.TEXT_MUTED
LABEL        = tt.LABEL
CLR_OPEN     = tt.ACCENT
ROW_HOVER    = tt.ROW_HOVER
SELECTION_BG = tt.SELECTION_BG
BG_FIELD     = tt.BG_FIELD

# Every row is the same height — the account band runs straight into the
# previous account's last position row, and its lighter background is
# what marks the group break.
ROW_H        = tt.ROW_H
ACCT_ROW_H   = ROW_H

_TITLE_FONT = QFont("Inter, Helvetica Neue, Arial", 11)
_TITLE_FONT.setWeight(QFont.Weight.Bold)

_TABLE_FONT = tt.TABLE_FONT
# Both tables here render captions as written rather than uppercased.
_COL_HEADER_FONT_MIXED = tt.COL_HEADER_FONT_MIXED
_INFO_LABEL_FONT  = QFont("JetBrains Mono, Menlo, Consolas, monospace", 10)
_INFO_VALUE_FONT  = QFont("JetBrains Mono, Menlo, Consolas, monospace", 10, QFont.Weight.Bold)
tt.enable_tabular_figures(_INFO_LABEL_FONT, _INFO_VALUE_FONT)

POSITIONS_TABLE_QSS = tt.TABLE_QSS
CONTROL_BTN_QSS     = tt.CONTROL_BTN_QSS
_ICON_BTN_QSS       = tt.ICON_BTN_QSS

_BG_HEADER_ROW_BRUSH = QBrush(QColor(BG_HEADER_ROW))
_ROW_HOVER_BRUSH     = tt.ROW_HOVER_BRUSH


def _draw_row_separator(painter: QPainter, option: QStyleOptionViewItem,
                        index) -> None:
    """1px rule under a position row — but only between two position rows.

    Neither edge of an account band gets a line: the band's own lighter
    background already marks the group break, and a rule there would read
    as a second, competing divider.  Called by every delegate on the table
    so the rule runs the full width of the row.
    """
    if index.data(RowTypeRole) != ROW_TYPE_POSITION:
        return
    below = index.sibling(index.row() + 1, index.column())
    if not below.isValid() or below.data(RowTypeRole) != ROW_TYPE_POSITION:
        return
    tt.paint_separator_line(painter, option.rect)


def _apply_row_background(option: QStyleOptionViewItem, index,
                          hover_row: int | None,
                          hover_brush=_ROW_HOVER_BRUSH) -> None:
    """Give *option* the account-band or row-hover background.

    Shared by every delegate on the table so a hovered row fills edge to
    edge — each column is painted by a different delegate, and any one
    that skipped this would leave a gap in the highlight.
    """
    if index.data(RowTypeRole) == ROW_TYPE_HEADER:
        option.backgroundBrush = _BG_HEADER_ROW_BRUSH
        option.features = (
            option.features
            & ~QStyleOptionViewItem.ViewItemFeature.Alternate
        )
    elif (index.row() == hover_row
            and not option.state & QStyle.StateFlag.State_Selected):
        option.backgroundBrush = hover_brush


# ══════════════════════════════════════════════════════════════════════════════
#  PortfolioRowDelegate  – row backgrounds + the table's hover tracking
# ══════════════════════════════════════════════════════════════════════════════

class PortfolioRowDelegate(tt.RowHoverDelegate):
    """Default delegate for the portfolio table.

    Also the table's single source of truth for which row the cursor is
    on — the band and close-control delegates read ``hover_row`` and
    ``hover_brush`` from it so a hovered row highlights across every
    column.
    """

    @staticmethod
    def _is_hoverable(index) -> bool:
        # Account bands don't highlight — they aren't a row you act on.
        return index.data(RowTypeRole) == ROW_TYPE_POSITION

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:
        super().initStyleOption(option, index)
        _apply_row_background(option, index, self._hover_row,
                              self.hover_brush)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index) -> None:
        super().paint(painter, option, index)
        _draw_row_separator(painter, option, index)


# The Summary table's TOTAL footer is that table's account band, which is
# exactly the shared delegate's contract — no subclass needed.
SummaryRowDelegate = tt.FooterAwareRowDelegate


# ══════════════════════════════════════════════════════════════════════════════
#  AccountHeaderInfoDelegate – the account band (whole row spanned)
# ══════════════════════════════════════════════════════════════════════════════

class AccountHeaderInfoDelegate(QStyledItemDelegate):
    """For HEADER rows: paints the account band — account name followed by
    "Daily P&L · Net · EX · BP" — across the whole spanned row, with a
    page-coloured gap above it so account groups read as separate blocks.
    For POSITION rows: normal symbol rendering."""

    _LABEL_COLOR  = QColor(LABEL)
    _GAP_LBL_VAL  = 5    # px between label and value
    _GAP_SEGMENTS = 18   # px between categories
    _GAP_AFTER_ACCT = 22  # px between the account name and the first label

    def __init__(self, parent=None, *, hover_source=None):
        super().__init__(parent)
        self._hover_source = hover_source

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:
        super().initStyleOption(option, index)
        src = self._hover_source
        _apply_row_background(option, index,
                              src.hover_row if src else None,
                              src.hover_brush if src else _ROW_HOVER_BRUSH)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index) -> None:
        if index.data(RowTypeRole) != ROW_TYPE_HEADER:
            # Option symbols stack root+expiry over strike+right at a
            # smaller font — same treatment as the shared row delegates.
            if not tt.paint_two_line_cell(self, painter, option, index):
                super().paint(painter, option, index)
            _draw_row_separator(painter, option, index)
            return

        painter.save()

        # The band runs edge to edge with no gap above it — the lighter
        # background alone marks the break between account groups.
        band = option.rect
        painter.fillRect(band, QColor(BG_HEADER_ROW))

        ad           = index.data(HeaderInfoRole) or {}
        daily_pnl    = ad.get("daily_pnl",    float("nan"))
        net_liq      = ad.get("net_liq",      0.0)
        excess_liq   = ad.get("excess_liq",   0.0)
        buying_power = ad.get("buying_power", 0.0)

        # Header-specific format: always show a value (including +0)
        if math.isfinite(daily_pnl):
            v = int(round(daily_pnl))
            daily_str = f"+{v:,}" if v >= 0 else f"-{abs(v):,}"
        else:
            daily_str = "–"
        net_str    = _fmt_money(net_liq)
        excess_str = _fmt_money(excess_liq)
        bp_str     = _fmt_money(buying_power)

        # (label, value, value_color)
        segments = [
            ("Daily P&L", daily_str,  _pnl_color(daily_pnl)),
            ("Net",       net_str,    QColor(TEXT_2)),
            ("EX",        excess_str, QColor(TEXT_2)),
            ("BP",        bp_str,     QColor(TEXT_2)),
        ]

        x = band.left() + 8
        y, h = band.top(), band.height()

        # Account name — the band's identity, so it leads the line.
        acct_name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        if acct_name:
            painter.setFont(_ACCT_FONT)
            painter.setPen(QColor(Theme.POS_ACCT_NAME))
            fm = painter.fontMetrics()
            baseline = y + (h + fm.ascent() - fm.descent()) // 2
            painter.drawText(x, baseline, acct_name)
            x += fm.horizontalAdvance(acct_name) + self._GAP_AFTER_ACCT

        for i, (lbl, val, v_color) in enumerate(segments):
            # Label — use painter.fontMetrics() for accurate device metrics
            painter.setFont(_INFO_LABEL_FONT)
            painter.setPen(self._LABEL_COLOR)
            fm = painter.fontMetrics()
            baseline = y + (h + fm.ascent() - fm.descent()) // 2
            painter.drawText(x, baseline, lbl)
            x += fm.horizontalAdvance(lbl) + self._GAP_LBL_VAL

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


# ── Row close control ─────────────────────────────────────────────────────────
# One segmented group, four flat segments: [ 25 │ 50 │ 75 │ 100% ].
# 100% is red at rest on purpose — it marks the full close as the
# destructive stop at the end of the run.
_SEG_BTNS = [("25", 25), ("50", 50), ("75", 75), ("100%", 100)]

_SEG_H        = 20    # control height
_SEG_RADIUS   = 5
_SEG_PAD      = 9     # horizontal padding inside each segment
_SEG_MARGIN_L = 8     # gap between the cell's left edge and the control
_SEG_MARGIN_R = 8     # trailing room kept when sizing the column

_SEG_FONT = QFont("Inter, Helvetica Neue, Arial")
_SEG_FONT.setPixelSize(11)
_SEG_FONT.setWeight(QFont.Weight.DemiBold)

# The font never changes, so its metrics are computed once — lazily,
# because QFontMetrics needs a live QGuiApplication.  Constructing it
# per segment-rect computation / cell paint was measurable on hover.
_SEG_FM: QFontMetrics | None = None


def _seg_metrics() -> QFontMetrics:
    global _SEG_FM
    if _SEG_FM is None:
        _SEG_FM = QFontMetrics(_SEG_FONT)
    return _SEG_FM

# ── Position column definitions (configurable via Column Management) ───────────
_POS_COL_KEYS = [
    "symbol", "day_pnl", "qty", "unrealized_pnl",
    "avg_price", "last_price", "realized_pnl", "market_value",
]
# Mirrors ``account_position_model.HEADERS`` minus the close control —
# the Columns page reads this one, the table draws that one.
_POS_COL_HEADERS = [
    "Symbol", "Daily P&L", "Qty", "U.P&L",
    "Avg price", "Last price", "R.P&L", "Mkt value",
]
_POS_KEY_TO_COL: dict[str, int] = {k: i for i, k in enumerate(_POS_COL_KEYS)}


# ══════════════════════════════════════════════════════════════════════════════
#  CloseControlDelegate
#    • Header rows  → nothing (the account band spans over this column)
#    • Position rows → segmented [ 25 │ 50 │ 75 │ 100% ] close control
# ══════════════════════════════════════════════════════════════════════════════

class CloseControlDelegate(QStyledItemDelegate):
    """Row-end close control for the portfolio table.

    Four flat segments joined inside one rounded outline.  Hover is
    tracked per segment (not per cell) via an event filter on the view's
    viewport, because ``State_MouseOver`` is only cell-granular and would
    light all four segments at once.

    Every segment sends a live order, so a click only *arms* it: the
    segment fills solid and its label flips to "Sure?", and a second
    click within :attr:`_CONFIRM_MS` sends.  Anything else — the timer
    expiring, arming a different segment, the pointer leaving the row —
    quietly disarms.  Same misclick guard the Account List's Flatten
    uses (``account_list_panel._arm_flatten``).
    """

    # (account, con_id, pct)  pct=100 → full flatten
    position_close_clicked = Signal(str, int, int)

    _CONFIRM_MS = 3000
    _ARMED_TEXT = "Sure?"

    def __init__(self, parent=None, *, hover_source=None):
        super().__init__(parent)
        self._pressed_row: int | None = None
        self._pressed_seg: int | None = None   # index into _SEG_BTNS
        self._hover_row: int | None = None
        self._hover_seg: int | None = None
        # (row, segment) currently armed, or None.
        self._armed: tuple[int, int] | None = None
        self._confirm_timer = QTimer(self)
        self._confirm_timer.setSingleShot(True)
        self._confirm_timer.setInterval(self._CONFIRM_MS)
        self._confirm_timer.timeout.connect(self._disarm)
        # Row-level hover comes from PortfolioRowDelegate; this delegate
        # only tracks which *segment* the cursor is on.
        self._hover_source = hover_source
        view = self.parent()
        if view is not None and hasattr(view, "viewport"):
            view.viewport().installEventFilter(self)

    # ── arm / disarm ──────────────────────────────────────────────────────────

    def _repaint(self) -> None:
        view = self.parent()
        if view is not None and hasattr(view, "viewport"):
            view.viewport().update()

    def _arm(self, row: int, seg: int) -> None:
        self._armed = (row, seg)
        self._confirm_timer.start()
        self._repaint()

    def _disarm(self) -> None:
        self._confirm_timer.stop()
        if self._armed is not None:
            self._armed = None
            self._repaint()

    def _is_armed(self, row: int, seg: int) -> bool:
        return self._armed == (row, seg)

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _is_header(index) -> bool:
        return index.data(RowTypeRole) == ROW_TYPE_HEADER

    @staticmethod
    def _is_position(index) -> bool:
        return index.data(RowTypeRole) == ROW_TYPE_POSITION

    @classmethod
    def _seg_widths(cls, fm: QFontMetrics) -> list[int]:
        """Segment widths, each wide enough for its own label *and* the
        armed one — otherwise the control would resize under the cursor
        the moment you armed it, moving the very target you're aiming at."""
        armed = fm.horizontalAdvance(cls._ARMED_TEXT)
        return [max(fm.horizontalAdvance(lbl), armed) + 2 * _SEG_PAD
                for lbl, _pct in _SEG_BTNS]

    @classmethod
    def _control_width(cls, fm: QFontMetrics) -> int:
        return sum(cls._seg_widths(fm))

    def _seg_rects(self, cell_rect: QRect) -> list[QRect]:
        """One QRect per segment, left-aligned in the cell.

        Left rather than right so the control sits just past Mkt value at
        a fixed offset: the column is the stretch column, so a
        right-aligned control drifts with the panel's width and lands a
        long way from the numbers it acts on.
        """
        fm      = _seg_metrics()
        widths  = self._seg_widths(fm)
        h       = min(_SEG_H, cell_rect.height() - 2)
        x       = cell_rect.left() + _SEG_MARGIN_L
        y       = cell_rect.y() + (cell_rect.height() - h) // 2

        rects = []
        for w in widths:
            rects.append(QRect(x, y, w, h))
            x += w
        return rects

    # ── paint ─────────────────────────────────────────────────────────────────

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index) -> None:
        painter.save()

        # ── background ───────────────────────────────────────────────────
        if self._is_header(index):
            # The band spans the whole row, so this only fires if a span
            # is momentarily missing (between a rebuild and _apply_spans).
            painter.fillRect(option.rect, QColor(BG_HEADER_ROW))
            painter.restore()
            return

        src = self._hover_source
        hover_row = src.hover_row if src else None
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor(SELECTION_BG))
        elif index.row() == hover_row:
            painter.fillRect(option.rect,
                             src.hover_brush if src else _ROW_HOVER_BRUSH)
        else:
            painter.fillRect(option.rect, QColor(BG_TABLE))

        _draw_row_separator(painter, option, index)

        # ── segmented control ────────────────────────────────────────────
        # A flat row has nothing to close, so it gets no control.
        if not self._is_position(index) or index.data(PositionRole) == 0:
            painter.restore()
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rects = self._seg_rects(option.rect)
        outer = QRect(rects[0].left(), rects[0].top(),
                      sum(r.width() for r in rects), rects[0].height())

        row = index.row()
        painter.setFont(_SEG_FONT)

        for i, ((lbl, pct), rect) in enumerate(zip(_SEG_BTNS, rects)):
            armed = self._is_armed(row, i)
            active = armed \
                or (self._hover_row == row and self._hover_seg == i) \
                or (self._pressed_row == row and self._pressed_seg == i)
            destructive = pct >= 100
            if armed:
                lbl = self._ARMED_TEXT

            if active:
                # 25/50/75 lighten whatever is beneath rather than paint a
                # fixed grey: the row underneath may be the table base, a
                # hover fill or a selection fill, and a fixed colour ends up
                # darker than the row it sits on — the segment then reads as
                # a hole instead of a raised button.  An armed segment goes
                # solid red whatever its percentage — it is one click from
                # sending an order.
                fill = (QColor(Theme.POS_LOSS_HOVER)
                        if (destructive or armed)
                        else QColor(255, 255, 255, 38))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill)
                # Round only the outer corners so the fill stays inside the
                # group's rounded outline.
                if i == 0:
                    painter.drawRoundedRect(rect, _SEG_RADIUS, _SEG_RADIUS)
                    painter.drawRect(rect.adjusted(_SEG_RADIUS, 0, 0, 0))
                elif i == len(_SEG_BTNS) - 1:
                    painter.drawRoundedRect(rect, _SEG_RADIUS, _SEG_RADIUS)
                    painter.drawRect(rect.adjusted(0, 0, -_SEG_RADIUS, 0))
                else:
                    painter.drawRect(rect)

            if armed:
                fg = QColor("#ffffff")
            elif destructive:
                fg = QColor("#ffffff") if active else QColor(Theme.POS_LOSS)
            else:
                fg = QColor(TEXT) if active else QColor(TEXT_MUTED)
            painter.setPen(fg)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, lbl)

        # Outline + internal dividers, drawn last so they sit on top of fills.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(OUTLINE)))
        painter.drawRoundedRect(
            QRect(outer.left(), outer.top(),
                  outer.width() - 1, outer.height() - 1),
            _SEG_RADIUS, _SEG_RADIUS)
        for rect in rects[:-1]:
            painter.drawLine(rect.right(), rect.top() + 1,
                             rect.right(), rect.bottom() - 1)

        painter.restore()

    # ── interaction ───────────────────────────────────────────────────────────

    @staticmethod
    def _update_rows(view, *rows) -> None:
        """Repaint just the given rows — a full viewport update here
        re-ran every visible cell's data() chain per mouse move."""
        vp = view.viewport()
        for r in rows:
            if r is not None and r >= 0:
                vp.update(QRect(0, view.rowViewportPosition(r),
                                vp.width(), view.rowHeight(r)))

    def eventFilter(self, obj, event) -> bool:
        """Track which segment the cursor is over, so hover is per-segment.

        Purely visual — never consumes the event.
        """
        etype = event.type()
        if etype == QEvent.Type.MouseMove:
            view = self.parent()
            pos  = event.position().toPoint() if hasattr(event, "position") \
                   else event.pos()
            row = seg = None
            idx = view.indexAt(pos)
            if (idx.isValid() and idx.column() == COL_FLATTEN
                    and self._is_position(idx)
                    and idx.data(PositionRole) != 0):
                for i, rect in enumerate(self._seg_rects(view.visualRect(idx))):
                    if rect.contains(pos):
                        row, seg = idx.row(), i
                        break
            if (row, seg) != (self._hover_row, self._hover_seg):
                prev_row = self._hover_row
                self._hover_row, self._hover_seg = row, seg
                # Moving off an armed segment cancels it — an armed control
                # must never be left waiting somewhere you aren't looking.
                if self._armed is not None and (row, seg) != self._armed:
                    self._disarm()
                self._update_rows(view, prev_row, row)
        elif etype == QEvent.Type.Leave:
            self._disarm()
            if self._hover_row is not None or self._hover_seg is not None:
                prev_row = self._hover_row
                self._hover_row = self._hover_seg = None
                self._update_rows(self.parent(), prev_row, None)
        return False

    def editorEvent(self, event: QEvent, model,
                    option: QStyleOptionViewItem, index) -> bool:
        etype = event.type()

        if not self._is_position(index) or index.data(PositionRole) == 0:
            return False

        rects = self._seg_rects(option.rect)
        pos   = event.position().toPoint() if hasattr(event, "position") \
                else event.pos()

        if etype == QEvent.Type.MouseButtonPress:
            for i, rect in enumerate(rects):
                if rect.contains(pos):
                    self._pressed_row = index.row()
                    self._pressed_seg = i
                    return True
            return False

        if etype == QEvent.Type.MouseButtonRelease:
            was_row = self._pressed_row == index.row()
            was_seg = self._pressed_seg
            self._pressed_row = None
            self._pressed_seg = None
            if was_row and was_seg is not None:
                for i, rect in enumerate(rects):
                    if i == was_seg and rect.contains(pos):
                        row = index.row()
                        if not self._is_armed(row, i):
                            # First click only arms — nothing is sent.
                            self._arm(row, i)
                            return True
                        self._disarm()
                        key = index.data(PositionKeyRole)
                        if key:
                            acct, con_id = key
                            _lbl, pct = _SEG_BTNS[i]
                            self.position_close_clicked.emit(
                                acct, con_id, pct)
                        return True
            return False

        return False

    # ── size hint ─────────────────────────────────────────────────────────────

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        fm = _seg_metrics()
        return QSize(self._control_width(fm) + _SEG_MARGIN_R + 8, _SEG_H + 6)


# ══════════════════════════════════════════════════════════════════════════════
#  AccountPositionPanel
# ══════════════════════════════════════════════════════════════════════════════

class AccountPositionPanel(QWidget):
    """Grouped portfolio positions table with per-account header rows."""

    refresh_requested         = Signal()
    position_close_requested  = Signal(str, int, int)  # account, con_id, pct (100=flatten)
    # Row click on either table — full position dict (summary rows carry
    # account="" since they aggregate across accounts).  FlashTradeView
    # routes this to BookTraderView.focus_position.
    position_row_clicked      = Signal(dict)

    def __init__(self, accounts_model=None, parent=None, *, open_preferences=None):
        super().__init__(parent)
        self._accounts_model = accounts_model
        self._open_preferences_callback = open_preferences
        self._model = AccountPositionModel(
            get_alias=self._get_alias, parent=self)
        self._summary_model = PositionsSummaryModel(
            self, get_alias=self._get_alias)
        # Batches parked while the summary view is hidden, deduped per
        # (account, con_id); folded in one go when the view is shown.
        self._pending_summary_batch: dict[tuple[str, int], dict] = {}
        self._setup_ui()
        # Both pills start unchecked every launch (no persistence):
        # flat account-grouped view, zero positions hidden.
        zero_pos = self._zero_pos_toggle.isChecked()
        self._model.set_show_zero(zero_pos)
        self._summary_model.set_show_zero(zero_pos)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setObjectName("AccountPositionPanel")
        self.setStyleSheet(f"""
            QWidget#AccountPositionPanel {{
                background: {BG_PAGE};
                border: 1px solid {BORDER};
                border-radius: 6px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # Build the utility bar (Refresh + Zero Pos toggle).  It is NOT
        # added to our own layout — PositionsTradesTabs parks it in the
        # QTabWidget's top-right corner so the controls sit on the same
        # row as the POSITIONS / TRADES tabs.  The tab bar already shows
        # the view name, so no in-panel title is needed anymore.
        self._control_bar = self._build_control_bar()

        # ── Table ────────────────────────────────────────────────────────
        self._table = QTableView()
        self._table.setModel(self._model)
        # Shared chrome: no zebra, no gridlines, and viewport mouse
        # tracking for the row-hover delegates.  Captions read as written
        # ("Daily P&L", "U.P&L") rather than uppercased — the summary
        # table below keeps the shared uppercase strip.
        tt.apply_table_chrome(self._table,
                              header_font=tt.COL_HEADER_FONT_MIXED)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Rows aren't click-selectable — hover is the only pointer cue.
        tt.disable_click_selection(self._table)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        # Global delegate – account-band background + row-hover tracking.
        # The other two delegates read their hover state from it so a
        # hovered row highlights across every column.
        self._row_delegate = PortfolioRowDelegate(self._table)
        self._table.setItemDelegate(self._row_delegate)

        # Account band delegate (COL_SYMBOL — the span origin on header rows)
        self._info_delegate = AccountHeaderInfoDelegate(
            self._table, hover_source=self._row_delegate)
        self._table.setItemDelegateForColumn(COL_SYMBOL, self._info_delegate)

        # 25|50|75|100% close control on the last column
        self._flatten_delegate = CloseControlDelegate(
            self._table, hover_source=self._row_delegate)
        self._flatten_delegate.position_close_clicked.connect(
            self._on_position_close_clicked)
        # A rebuild reorders rows, so an armed segment could end up over a
        # different instrument — drop the arming whenever that happens.
        self._model.modelReset.connect(self._flatten_delegate._disarm)
        self._table.setItemDelegateForColumn(COL_FLATTEN, self._flatten_delegate)

        # Header
        h = self._table.horizontalHeader()
        h.setDefaultAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setStretchLastSection(True)

        self._restore_col_widths()
        self._apply_column_visibility(self._load_visible_pos_keys())
        h.sectionResized.connect(self._on_section_resized)
        self._table.clicked.connect(self._on_table_row_clicked)

        # Apply column spans whenever model is rebuilt
        self._model.modelReset.connect(self._apply_spans)
        # Realized is empty on most days — keep it out of the way until a
        # row actually has one.
        self._model.modelReset.connect(self._update_realized_visibility)
        self._model.dataChanged.connect(self._on_model_data_changed)

        # ── Summary table (one row per con_id, totals footer) ─────────────
        self._summary_table = QTableView()
        self._summary_table.setModel(self._summary_model)
        # Captions read as written ("Qty", "U.P&L"), matching the flat
        # Positions table above rather than the uppercase strip.
        tt.apply_table_chrome(self._summary_table,
                              header_font=tt.COL_HEADER_FONT_MIXED)
        self._summary_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        tt.disable_click_selection(self._summary_table)
        self._summary_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._summary_row_delegate = SummaryRowDelegate(self._summary_table)
        self._summary_table.setItemDelegate(self._summary_row_delegate)

        sh = self._summary_table.horizontalHeader()
        sh.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        sh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        sh.setStretchLastSection(True)
        self._apply_summary_default_col_widths()
        self._restore_summary_col_widths()
        self._apply_summary_column_visibility(
            self._load_visible_summary_keys())
        sh.sectionResized.connect(self._on_summary_section_resized)
        self._summary_table.clicked.connect(self._on_summary_row_clicked)

        # Context menu + Delete shortcut for hide/restore (summary only).
        self._summary_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._summary_table.customContextMenuRequested.connect(
            self._on_summary_context_menu)

        delete_sc = QShortcut(
            QKeySequence(Qt.Key.Key_Delete), self._summary_table)
        delete_sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_sc.activated.connect(self._hide_current_summary_row)

        # ── Stack: index 0 = flat account-grouped; index 1 = summary ──
        self._stack = QStackedWidget()
        self._stack.addWidget(self._table)          # index 0
        self._stack.addWidget(self._summary_table)  # index 1

        root.addWidget(self._stack, stretch=1)

    # ── Column spans ─────────────────────────────────────────────────────────

    def _apply_spans(self) -> None:
        """Merge the whole row into one band cell for every account header row,
        and give those rows the extra height of the group gap above the band.

        clearSpans() is called first to remove all previous spans in one shot.
        Resetting individual cells with setSpan(row, col, 1, 1) triggers
        Qt's "single cell span won't be added" warning for every position row.
        """
        self._table.clearSpans()
        for row in range(self._model.rowCount()):
            idx = self._model.index(row, COL_SYMBOL)
            if idx.data(RowTypeRole) == ROW_TYPE_HEADER:
                self._table.setSpan(row, COL_SYMBOL, 1, NUM_COLS)
                self._table.setRowHeight(row, ACCT_ROW_H)
            else:
                self._table.setRowHeight(row, ROW_H)

    # ── Column-width persistence ──────────────────────────────────────────────

    _COL_SAMPLES = {
        COL_SYMBOL:         "WWWWWWWW",
        COL_DAY_PNL:        "-999,999",
        COL_POSITION:       "999999",
        COL_UNREALIZED_PNL: "-999,999",
        COL_AVG_PRICE:      "999,999.99",
        COL_LAST_PRICE:     "999,999.99",
        COL_REALIZED_PNL:   "-999,999",
        COL_MARKET_VALUE:   "-9,999,999",
    }

    def _apply_default_col_widths(self) -> None:
        fm     = QFontMetrics(self._table.font())
        hdr_fm = QFontMetrics(_COL_HEADER_FONT_MIXED)
        for col, sample in self._COL_SAMPLES.items():
            self._table.setColumnWidth(
                col, tt.default_column_width(fm, hdr_fm, sample, HEADERS[col]))
        # The close control sizes itself from its own font, not the table's.
        seg_fm = _seg_metrics()
        self._table.setColumnWidth(
            COL_FLATTEN,
            CloseControlDelegate._control_width(seg_fm) + _SEG_MARGIN_R + 8)

    # v2: the column set changed (Account → Symbol/Daily P&L split), so a
    # state saved under the old key would apply the wrong widths to the
    # wrong columns.  A new key retires those silently.
    _SETTINGS_KEY = "AccountPositionPanel/headerState_v2"

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
        # The summary model re-folds every position per batch (~4×/s).
        # While the flat view is showing, park the batches in a pending
        # dict (deduped per position) and fold them once on switch.
        if self._stack.currentIndex() == 1:
            self._summary_model.update_positions(batch)
        else:
            pend = self._pending_summary_batch
            for pos in batch:
                account = pos.get("account", "")
                con_id = pos.get("con_id")
                if account and con_id is not None:
                    pend[(account, int(con_id))] = pos

    def _flush_pending_summary(self) -> None:
        if self._pending_summary_batch:
            batch = list(self._pending_summary_batch.values())
            self._pending_summary_batch.clear()
            self._summary_model.update_positions(batch)

    def update_pnl_single(self, account: str, con_id: int,
                          daily_pnl: float, realized_pnl: float,
                          unrealized_pnl: float) -> None:
        # Intentionally NOT fanned out to the summary model.  The
        # pnl_single channel is a mid-tick per-(account, con_id) refresh
        # that makes the flat view feel snappy; the summary rebuilds
        # wholesale on the next portfolio_updated batch anyway, so
        # threading deltas through aggregate state isn't worth the
        # complexity.
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
        self._summary_model.clear()

    def get_position_keys(self) -> list[tuple[str, int]]:
        return self._model.get_position_keys()

    def get_symbol_for_position(self, account: str, con_id: int) -> str:
        """Return localSymbol (or root symbol) for the given (account, conId)."""
        return self._model.get_symbol_for_position(account, con_id)

    def position_for_symbol(self, account: str, symbol: str) -> dict | None:
        """Full STK position dict for (account, symbol), or None."""
        return self._model.position_for_symbol(account, symbol)

    def stk_positions_for_accounts(self, accounts) -> list[dict]:
        """Non-flat STK positions across *accounts*, one entry per symbol
        (model delegate — FlashTradeView hands this to the Options
        Income ticket for its position chips)."""
        return self._model.stk_positions_for_accounts(accounts)

    def held_shares(self, account: str, symbol: str) -> float:
        """Shares *account* currently holds in *symbol* (0.0 if none/unknown)."""
        return self._model.position_qty_for_symbol(account, symbol)

    def set_selected_accounts(self, accounts: set) -> None:
        """Filter positions view to only the given selected accounts.

        Delegates to the model; an empty set means show all accounts.
        Called whenever the Account List panel's checked state changes.
        """
        self._model.set_selected_accounts(accounts)
        self._summary_model.set_selected_accounts(accounts)

    def set_offline(self, labels, accounts=()) -> None:
        """Hide everything served by these (disconnected) connections.

        Positions and account bands for those accounts disappear from
        both the flat and summary views until the connection returns.
        Nothing is discarded — see ``offline_connections``."""
        self._model.set_offline(labels, accounts)
        self._summary_model.set_offline_labels(labels)

    def hidden_accounts(self) -> set:
        """Accounts currently hidden because their connection is down."""
        return self._model.hidden_accounts()

    # ── Control-bar builder ──────────────────────────────────────────────────

    def _build_control_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("PositionsControlBar")
        bar.setStyleSheet(f"QWidget#PositionsControlBar {{ background: transparent; border: none; }}")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(*tt.CTRL_MARGINS)
        lay.setSpacing(tt.CTRL_SPACING)
        # Stretch first so the buttons cluster on the right side of the
        # tab-bar corner (the stack can be much wider than our content).
        lay.addStretch(1)

        # Summary pill button — sits leftmost in the controls cluster.
        # Checked = per-contract aggregate view; unchecked = flat
        # account-grouped view.
        self._summary_toggle = QPushButton("Summary")
        self._summary_toggle.setCheckable(True)
        self._summary_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._summary_toggle.setFixedHeight(tt.CTRL_H)
        self._summary_toggle.setStyleSheet(CONTROL_BTN_QSS)
        self._summary_toggle.toggled.connect(self._on_summary_toggled)
        lay.addWidget(self._summary_toggle)
        # Alignment slack the tab container widens so this bar's anchored
        # controls line up with the Trades bar's — see
        # ``table_theme.align_control_bars``.  One pad sits right of each
        # anchor; both bars keep their own control order.
        pad_summary = tt.align_pad()
        lay.addItem(pad_summary)
        lay.addSpacing(6)

        self._zero_pos_toggle = QPushButton("Zero Pos")
        self._zero_pos_toggle.setCheckable(True)
        self._zero_pos_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._zero_pos_toggle.setFixedHeight(tt.CTRL_H)
        self._zero_pos_toggle.setStyleSheet(CONTROL_BTN_QSS)
        self._zero_pos_toggle.toggled.connect(self._on_zero_pos_toggled)
        lay.addWidget(self._zero_pos_toggle)
        pad_filter = tt.align_pad()
        lay.addItem(pad_filter)

        # Search field — filters both position views by account alias /
        # account id / symbol.  Styled to match the Trades tab's search box.
        self._search_field = QLineEdit()
        self._search_field.setObjectName("posSearch")
        self._search_field.setPlaceholderText("Search...")
        self._search_field.setFixedHeight(tt.CTRL_H)
        self._search_field.setStyleSheet(f"""
            QLineEdit#posSearch {{
                background: {BG_FIELD};
                color: {TEXT};
                border: 1px solid {OUTLINE};
                border-radius: 6px;
                padding: 2px 8px;
                font-family: Inter, "Helvetica Neue", Arial, sans-serif;
                font-size: 11px;
                min-width: {tt.CTRL_SEARCH_W}px;
                max-width: {tt.CTRL_SEARCH_W}px;
            }}
            QLineEdit#posSearch:hover {{ border-color: {OUTLINE_HOVER}; }}
            QLineEdit#posSearch:focus {{ border-color: {CLR_OPEN}; }}
        """)
        self._search_field.textChanged.connect(self._on_search_text_changed)
        # Esc clears the filter (event filter — QLineEdit ignores Esc).
        self._search_field.installEventFilter(self)
        lay.addWidget(self._search_field)
        pad_search = tt.align_pad()
        lay.addItem(pad_search)

        btn = QPushButton("⟳")
        btn.setToolTip("Refresh positions")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedSize(tt.CTRL_ICON, tt.CTRL_ICON)
        btn.setStyleSheet(_ICON_BTN_QSS)
        btn.clicked.connect(self._on_refresh_clicked)
        lay.addWidget(btn)

        # Both pills share one width — the wider label plus breathing
        # room — so Summary and Zero Pos render as equal-sized buttons,
        # and Summary matches the Trades bar's pill (same helper).
        pill_w = tt.pill_width(self._summary_toggle.font())
        self._summary_toggle.setFixedWidth(pill_w)
        self._zero_pos_toggle.setFixedWidth(pill_w)

        gear = QPushButton("\u2699")   # ⚙
        gear.setToolTip("Column preferences")
        gear.setCursor(Qt.CursorShape.PointingHandCursor)
        gear.setFixedSize(tt.CTRL_ICON, tt.CTRL_ICON)
        gear.setStyleSheet(_ICON_BTN_QSS)
        gear.clicked.connect(self._on_gear_clicked)
        lay.addWidget(gear)

        # Hand-off for the tab container's alignment pass.  Keys pair with
        # the Trades bar's: its date combo is this view's "Zero Pos" slot.
        bar.align_anchors = [
            ("summary", self._summary_toggle, pad_summary),
            ("filter",  self._zero_pos_toggle, pad_filter),
            ("search",  self._search_field,    pad_search),
        ]
        return bar

    @property
    def control_bar(self) -> QWidget:
        """Hand-off point for ``PositionsTradesTabs`` to install these
        utility controls in the tab-bar corner."""
        return self._control_bar

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_refresh_clicked(self) -> None:
        log.info("Portfolio: refresh requested")
        self.clear_positions()
        self.refresh_requested.emit()

    def _on_zero_pos_toggled(self, checked: bool) -> None:
        self._model.set_show_zero(checked)
        self._summary_model.set_show_zero(checked)

    def _on_search_text_changed(self, text: str) -> None:
        self._model.set_search_text(text)
        self._summary_model.set_search_text(text)

    def eventFilter(self, obj, event) -> bool:
        if (obj is self._search_field
                and event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape
                and self._search_field.text()):
            self._search_field.clear()
            return True
        return super().eventFilter(obj, event)

    def hideEvent(self, event) -> None:
        """Drop any selection when this tab stops being the visible one —
        same as the Trades blotter, so a stale highlight isn't waiting
        when you come back."""
        tt.clear_selections(getattr(self, "_table", None),
                            getattr(self, "_summary_table", None))
        super().hideEvent(event)

    def _on_summary_toggled(self, on: bool) -> None:
        self._stack.setCurrentIndex(1 if on else 0)
        if on:
            self._flush_pending_summary()

    def _on_gear_clicked(self) -> None:
        if self._open_preferences_callback:
            kind = ("positions_summary"
                    if self._stack.currentIndex() == 1 else "positions")
            self._open_preferences_callback(kind)

    def _on_position_close_clicked(self, account: str, con_id: int,
                                   pct: int) -> None:
        log.info("Portfolio: close %d%% requested – %s conId=%d",
                 pct, account, con_id)
        self.position_close_requested.emit(account, con_id, pct)

    def _on_table_row_clicked(self, idx) -> None:
        """Flat-table SYMBOL-cell click → ``position_row_clicked``.
        Only the Symbol cell jumps the Book Trader ladder — clicking any
        other cell (to inspect P&L, select a row, or near-miss the
        Flatten buttons) must not retarget it."""
        if not idx.isValid() or idx.column() != COL_SYMBOL:
            return
        if idx.data(RowTypeRole) != ROW_TYPE_POSITION:
            return
        key = idx.data(PositionKeyRole)
        if not key:
            return
        account, con_id = key
        row = self._model.position_data(account, con_id)
        if not row:
            return
        payload = dict(row)
        payload["account"] = account
        self.position_row_clicked.emit(payload)

    def _on_summary_row_clicked(self, idx) -> None:
        """Summary-table SYMBOL-cell click → ``position_row_clicked``
        (mirrors the flat table: only the Symbol cell jumps the ladder).
        The TOTAL footer returns None from row_data and is ignored.  The
        aggregate's ``position`` is the net across accounts — the right
        qty to prefill."""
        if not idx.isValid() or idx.column() != S_COL_SYMBOL:
            return
        agg = self._summary_model.row_data(idx.row())
        if agg is None:
            return
        payload = dict(agg)
        payload.pop("accounts", None)   # set() — keep the payload uniform
        payload["account"] = ""
        self.position_row_clicked.emit(payload)

    # ── Column management (ColumnsProvider protocol) ──────────────────────────

    def column_state(self, kind: str) -> tuple[list, list, list]:
        if kind == "positions_summary":
            return (list(PS_COL_KEYS), list(PS_COL_HEADERS),
                    self._load_visible_summary_keys())
        return (list(_POS_COL_KEYS), list(_POS_COL_HEADERS),
                self._load_visible_pos_keys())

    def apply_columns(self, kind: str, visible_keys: list[str]) -> None:
        prefs = config_manager.load_preferences()
        self._migrate_positions_columns(prefs)
        sub = "summary" if kind == "positions_summary" else "flat"
        prefs.setdefault("positions_columns", {}).setdefault(sub, {})[
            "visible"] = visible_keys
        config_manager.save_preferences(prefs)
        if kind == "positions_summary":
            self._apply_summary_column_visibility(visible_keys)
        else:
            self._apply_column_visibility(visible_keys)

    def _load_visible_pos_keys(self) -> list[str]:
        prefs = config_manager.load_preferences()
        self._migrate_positions_columns(prefs)
        return prefs.get("positions_columns", {}).get("flat", {}).get(
            "visible", list(_POS_COL_KEYS))

    def _load_visible_summary_keys(self) -> list[str]:
        prefs = config_manager.load_preferences()
        self._migrate_positions_columns(prefs)
        return prefs.get("positions_columns", {}).get("summary", {}).get(
            "visible", list(PS_COL_KEYS))

    def _apply_column_visibility(self, visible_keys: list[str]) -> None:
        visible = set(visible_keys)
        self._visible_pos_keys = visible
        for key, col in _POS_KEY_TO_COL.items():
            self._table.setColumnHidden(col, key not in visible)
        hdr = self._table.horizontalHeader()
        for target_visual, key in enumerate(visible_keys):
            logical = _POS_KEY_TO_COL.get(key)
            if logical is None:
                continue
            current_visual = hdr.visualIndex(logical)
            if current_visual != target_visual:
                hdr.moveSection(current_visual, target_visual)
        # Keep the close-control column visually last regardless of order.
        flatten_visual = hdr.visualIndex(COL_FLATTEN)
        if flatten_visual != hdr.count() - 1:
            hdr.moveSection(flatten_visual, hdr.count() - 1)
        self._update_realized_visibility()

    def _update_realized_visibility(self) -> None:
        """Hide the Realized column while every row's realized P&L is zero.

        A user who explicitly unchecked Realized in Column Management stays
        hidden either way — this only ever takes the column away, never
        overrides the preference to show it.
        """
        wanted = ("realized_pnl" in getattr(self, "_visible_pos_keys", set())
                  and self._model.has_realized())
        if self._table.isColumnHidden(COL_REALIZED_PNL) == wanted:
            self._table.setColumnHidden(COL_REALIZED_PNL, not wanted)

    def _on_model_data_changed(self, top_left, bottom_right, _roles=None) -> None:
        """Realized P&L arrives via pnlSingle after the rows are built, so the
        column's visibility has to be re-checked on data updates too.
        Coalesced to one check per event-loop turn: a 250 ms pnl flush
        lands one dataChanged per position, and ``has_realized`` scans
        every row — running it per position was O(N²) per flush."""
        if top_left.column() <= COL_REALIZED_PNL <= bottom_right.column():
            if getattr(self, "_realized_check_pending", False):
                return
            self._realized_check_pending = True
            QTimer.singleShot(0, self._realized_check_now)

    def _realized_check_now(self) -> None:
        self._realized_check_pending = False
        self._update_realized_visibility()

    def _apply_summary_column_visibility(self, visible_keys: list[str]) -> None:
        visible = set(visible_keys)
        for col, key in enumerate(PS_COL_KEYS):
            self._summary_table.setColumnHidden(col, key not in visible)
        hdr = self._summary_table.horizontalHeader()
        key_to_col = {k: i for i, k in enumerate(PS_COL_KEYS)}
        for target_visual, key in enumerate(visible_keys):
            logical = key_to_col.get(key)
            if logical is None:
                continue
            current_visual = hdr.visualIndex(logical)
            if current_visual != target_visual:
                hdr.moveSection(current_visual, target_visual)

    @staticmethod
    def _migrate_positions_columns(prefs: dict) -> None:
        """Bring saved column prefs up to the current shape.  Mutates
        *prefs* in place; idempotent for already-migrated configs.

        1. Old shape stored ``positions_columns["visible"] = [...]``
           directly; new shape is nested under ``["flat"|"summary"]`` so
           the two views can persist independently.
        2. The flat view's old ``"account"`` column doubled as the per-row
           Daily P&L; it is now a plain ``"day_pnl"`` column sitting after
           Symbol.
        """
        pc = prefs.get("positions_columns")
        if not isinstance(pc, dict):
            return
        if "visible" in pc and "flat" not in pc:
            old_visible = pc.pop("visible")
            pc.setdefault("flat", {})["visible"] = old_visible
            # Also accept a top-level "widths" that belonged to flat.
            if "widths" in pc:
                pc["flat"]["widths"] = pc.pop("widths")

        flat = pc.get("flat")
        if isinstance(flat, dict) and isinstance(flat.get("visible"), list):
            keys = [("day_pnl" if k == "account" else k)
                    for k in flat["visible"]]
            # Symbol leads the row now; anyone whose saved order still puts
            # Daily P&L first gets the two swapped rather than reset.
            if "symbol" in keys and "day_pnl" in keys \
                    and keys.index("day_pnl") < keys.index("symbol"):
                keys.remove("symbol")
                keys.insert(0, "symbol")
            flat["visible"] = keys

    # ── Summary table column widths (QSettings, same pattern as flat) ─────────

    _SUMMARY_COL_SAMPLES = {
        0: "WWWWWWW",     # symbol
        1: "FUT",         # sec_type
        2: "999999",      # position
        3: "999,999.99",  # avg_cost
        4: "999,999.99",  # market_price
        5: "-999,999",    # unrealized
        6: "-999,999",    # realized
        7: "-9,999,999",  # market_value
        8: "99",          # accounts count
        9: "NASDAQW",     # exchange
    }

    # v2: the restyle widened cell padding and uppercased the captions, so
    # widths saved under the old key now truncate their own values.
    _SUMMARY_SETTINGS_KEY = "AccountPositionPanel/summaryHeaderState_v2"

    def _apply_summary_default_col_widths(self) -> None:
        fm     = QFontMetrics(self._summary_table.font())
        hdr_fm = QFontMetrics(_COL_HEADER_FONT_MIXED)
        for col, sample in self._SUMMARY_COL_SAMPLES.items():
            self._summary_table.setColumnWidth(
                col,
                tt.default_column_width(fm, hdr_fm, sample,
                                        PS_COL_HEADERS[col]))

    def _restore_summary_col_widths(self) -> None:
        settings = QSettings("NanoPulse", "Dashboard")
        state = settings.value(self._SUMMARY_SETTINGS_KEY)
        if state is not None:
            if self._summary_table.horizontalHeader().restoreState(state):
                return
        self._apply_summary_default_col_widths()

    def _save_summary_col_widths(self) -> None:
        settings = QSettings("NanoPulse", "Dashboard")
        state = self._summary_table.horizontalHeader().saveState()
        settings.setValue(self._SUMMARY_SETTINGS_KEY, state)

    def _on_summary_section_resized(self, _logical: int, _old: int,
                                    _new: int) -> None:
        self._save_summary_col_widths()

    # ── Summary hide / restore ────────────────────────────────────────────────

    def _con_id_at_summary_index(self, idx) -> tuple[int | None, str]:
        """Map a summary-table view index to ``(con_id, label)``.
        Returns ``(None, "")`` for invalid indices or the TOTAL row."""
        if not idx.isValid():
            return None, ""
        agg = self._summary_model.row_data(idx.row())
        if agg is None:
            return None, ""
        con_id = agg.get("con_id")
        label = (agg.get("local_symbol") or agg.get("symbol") or "").strip()
        if con_id is None:
            return None, label
        return int(con_id), label

    def _on_summary_context_menu(self, pos) -> None:
        menu = QMenu(self._summary_table)
        idx = self._summary_table.indexAt(pos)
        con_id, label = self._con_id_at_summary_index(idx)

        hide_action = None
        if con_id is not None:
            hide_action = menu.addAction(
                f"Hide {label}" if label else "Hide row")

        restore_action = None
        n_hidden = self._summary_model.hidden_count()
        if n_hidden:
            suffix = "s" if n_hidden > 1 else ""
            restore_action = menu.addAction(
                f"Restore {n_hidden} hidden row{suffix}")

        if not menu.actions():
            return
        chosen = menu.exec(
            self._summary_table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is hide_action and con_id is not None:
            self._summary_model.hide_con_id(con_id)
        elif chosen is restore_action:
            self._summary_model.restore_hidden()

    def _hide_current_summary_row(self) -> None:
        """Delete-key handler: hide the focused summary row."""
        con_id, _ = self._con_id_at_summary_index(
            self._summary_table.currentIndex())
        if con_id is not None:
            self._summary_model.hide_con_id(con_id)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_alias(self, account: str) -> str:
        if self._accounts_model is not None:
            return self._accounts_model._aliases.get(account, "")
        return ""
