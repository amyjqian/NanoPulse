"""Shared look for the data tables in the Positions and Trades tabs.

Four tables use this treatment — Positions flat + Summary, Trades flat +
Summary — and they sit behind the same tab bar, so any divergence reads as
a glitch when you switch tabs.  Everything they have in common lives here:
the stylesheet, the fonts, row height, the row-hover delegate and the row
separator.  Panel-specific painting (the account band, the close control)
stays in the panel that owns it.

The visual language comes from ``NanoPulse-Positions-Restyle.md``:
no vertical gridlines, no zebra striping, uppercase tracked-out captions,
a 1px rule between data rows, and a lighter band for group/footer rows.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QItemSelectionModel, QObject, QRect, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QSizePolicy,
    QSpacerItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
)

from src.ui.theme import Theme

# ── Custom roles ──────────────────────────────────────────────────────────────
# bool: this row is a totals/footer band — it carries its own background,
# so it never takes a hover fill or a separator on either edge.
IsFooterRowRole = Qt.ItemDataRole.UserRole + 120
# str: identity of the group a row belongs to.  GroupBandDelegate tints
# consecutive runs of equal key, alternating per run.
GroupKeyRole = Qt.ItemDataRole.UserRole + 130

# ── Theme tokens ──────────────────────────────────────────────────────────────
BG_PAGE      = Theme.POS_BG_PAGE
BG_TABLE     = Theme.POS_BG_TABLE
BG_HEADER    = Theme.POS_BG_TABLE
BG_BAND      = Theme.POS_BG_ACCT_BAND
BORDER       = Theme.POS_BORDER
OUTLINE      = Theme.POS_OUTLINE
OUTLINE_HOVER = "#4a5464"
TEXT         = Theme.POS_TEXT
TEXT_2       = Theme.POS_TEXT_2
TEXT_MUTED   = Theme.POS_TEXT_MUTED
LABEL        = Theme.POS_LABEL
ACCENT       = Theme.POS_ACCENT
ROW_HOVER    = Theme.POS_ROW_HOVER
ROW_GROUP_ALT = Theme.POS_ROW_GROUP_ALT
ROW_SEP      = Theme.POS_ROW_SEP
SELECTION_BG = Theme.POS_ROW_SELECTED
# Control-bar field background — one notch above the table so a search box
# reads as an input, not a gap in the toolbar.
BG_FIELD     = "#161a24"

ROW_H = 32

# Horizontal room a cell loses before it can draw text: the stylesheet's
# padding each side, the style's own item margin, and a few px of slack so
# the widest value never elides to "…".
CELL_PAD_H = 8
CELL_PAD   = 2 * CELL_PAD_H + 14

# ── Control-bar geometry ──────────────────────────────────────────────────────
# Both tabs park their utility controls in the same tab-bar corner and the
# corner stack right-aligns them, so the Summary pill's on-screen position
# is decided entirely by the total width of what sits to its RIGHT.  Each
# bar keeps its own control order; ``align_summary_pills`` measures both
# and widens one bar's ``align_pad`` to make those two widths equal, so
# the pill doesn't jump when you switch tabs.
CTRL_MARGINS = (8, 2, 8, 2)
CTRL_SPACING = 8
CTRL_H       = 26          # pills and text fields
CTRL_ICON    = 28          # square icon buttons (gear, refresh)
CTRL_SEARCH_W = 110
# Pill labels the two bars size against, so "Summary" is the same width in
# both (and matches "Zero Pos" next to it in the Positions bar).
CTRL_PILL_LABELS = ("Summary", "Zero Pos")
CTRL_PILL_PAD = 22


def pill_width(font: QFont) -> int:
    """Shared width for the Summary / Zero Pos pills.

    *font* must match the QSS the pill is styled with (11px, DemiBold) —
    the pills carry a fixed width so both bars agree to the pixel.
    """
    f = QFont(font)
    f.setPixelSize(11)
    f.setWeight(QFont.Weight.DemiBold)
    fm = QFontMetrics(f)
    return max(fm.horizontalAdvance(t) for t in CTRL_PILL_LABELS) + CTRL_PILL_PAD


def align_pad() -> QSpacerItem:
    """Zero-width spacer a control bar parks to the right of an anchor.

    ``align_control_bars`` widens it by however much that bar's anchor is
    short of the rightmost bar's, which slides the anchor (and everything
    left of it) into line.
    """
    return QSpacerItem(0, 0, QSizePolicy.Policy.Fixed,
                       QSizePolicy.Policy.Minimum)


def _set_pad(pad: QSpacerItem, px: int) -> None:
    pad.changeSize(max(0, px), 0, QSizePolicy.Policy.Fixed,
                   QSizePolicy.Policy.Minimum)


# Extra room the alignment pass measures with, so the bars' leading
# stretch — never a control — takes up the slack.
_ALIGN_SLACK = 200


def _layout_at(bar, width: int) -> None:
    lay = bar.layout()
    lay.invalidate()
    lay.setGeometry(QRect(0, 0, width, lay.sizeHint().height()))


def align_control_bars(*bars) -> None:
    """Line up the anchored controls of several right-aligned control bars.

    Each bar exposes ``align_anchors``: ``(key, widget, pad)`` triples in
    left-to-right order, one per control that has to sit at the same x in
    every bar (the pad is an :func:`align_pad` parked immediately right of
    that widget).  The bars share a right edge — they're swapped in and
    out of one tab-bar corner — so the anchors are matched by their
    distance from that edge, and each bar keeps its own control order.

    Measured off real layout passes rather than size-hint arithmetic:
    QBoxLayout drops its spacing next to the fixed spacers these bars use
    and the stylesheets cap the search fields' widths, so hand-summing the
    trailing items lands a dozen px out.  Anchors are resolved right to
    left because widening a pad only moves what sits left of it.

    Call once after the bars are built (the tab container does it).  Bars
    without ``align_anchors`` are skipped.
    """
    bars = [b for b in bars if getattr(b, "align_anchors", None)]
    if len(bars) < 2:
        return
    count = min(len(b.align_anchors) for b in bars)
    # Zero the pads first so re-running this is idempotent.
    for bar in bars:
        for _, _, pad in bar.align_anchors:
            _set_pad(pad, 0)
    width = max(b.layout().sizeHint().width() for b in bars) + _ALIGN_SLACK
    for i in reversed(range(count)):
        gaps = []
        for bar in bars:
            _layout_at(bar, width)
            gaps.append(width - bar.align_anchors[i][1].x())
        target = max(gaps)
        for bar, gap in zip(bars, gaps):
            _set_pad(bar.align_anchors[i][2], target - gap)
    for bar in bars:
        bar.layout().invalidate()
        bar.updateGeometry()

# ── Fonts ─────────────────────────────────────────────────────────────────────
TABLE_FONT = QFont("JetBrains Mono, Menlo, Consolas, monospace", 11)

# Cell fonts every table shares.  Deliberately carry **no size** — a font
# returned from FontRole is merged over the view's font with
# ``QFont::resolve``, and any size set here would override the
# stylesheet's, which is how the two tabs drifted a pixel apart.  Family
# and weight are all these say; CELL_FONT_PX below is the single size.
CELL_FONT_PX = 11

_MONO_FAMILY = "JetBrains Mono, Menlo, Consolas, monospace"
CELL_FONT      = QFont(_MONO_FAMILY)
CELL_FONT_BOLD = QFont(_MONO_FAMILY)
CELL_FONT_BOLD.setWeight(QFont.Weight.Bold)

# Column captions: uppercase and tracked out, so the header reads as a
# label strip rather than a first data row.
COL_HEADER_FONT = QFont("Inter, Helvetica Neue, Arial")
COL_HEADER_FONT.setPixelSize(11)
COL_HEADER_FONT.setWeight(QFont.Weight.DemiBold)
COL_HEADER_FONT.setCapitalization(QFont.Capitalization.AllUppercase)
COL_HEADER_FONT.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)


def enable_tabular_figures(*fonts: QFont) -> None:
    """Turn on the OpenType 'tnum' feature where Qt supports it.

    Qt 6.7+ only; on older Qt the mono fallbacks are tabular anyway.
    """
    for f in fonts:
        try:
            f.setFeature(QFont.Tag("tnum"), 1)
        except (AttributeError, TypeError):
            pass


enable_tabular_figures(TABLE_FONT, CELL_FONT, CELL_FONT_BOLD)

# ── QSS ───────────────────────────────────────────────────────────────────────
# No vertical gridlines and no zebra striping — alignment carries the
# column structure, and the 1px row separator (painted by the delegates
# below) carries the row structure.
TABLE_QSS = f"""
    QTableView {{
        background-color: {BG_TABLE};
        color: {TEXT};
        gridline-color: transparent;
        border: none;
        font-size: {CELL_FONT_PX}px;
        selection-background-color: {SELECTION_BG};
        selection-color: {TEXT};
    }}
    QHeaderView::section {{
        background-color: {BG_HEADER};
        color: {LABEL};
        border: none;
        border-bottom: 1px solid {BORDER};
        padding: 2px {CELL_PAD_H}px;
        font-weight: 600;
        font-size: 11px;
    }}
    QTableView::item {{
        padding: 0px {CELL_PAD_H}px;
    }}
    QScrollBar:vertical {{
        background: {BG_TABLE};
        width: 8px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: #334058;
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: #334058;
    }}
    QScrollBar::handle:vertical:pressed {{
        background: #6E7380;
    }}
    QScrollBar:horizontal {{
        background: {BG_TABLE};
        height: 10px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background: #334058;
        border-radius: 4px;
        min-width: 40px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: #334058;
    }}
    QScrollBar::handle:horizontal:pressed {{
        background: #6E7380;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
    }}
"""

# The panel card both tabs sit in.  Callers pass their own objectName so
# the rule can't leak into child widgets.
def panel_card_qss(object_name: str) -> str:
    return f"""
        QWidget#{object_name} {{
            background: {BG_PAGE};
            border: 1px solid {BORDER};
            border-radius: 6px;
        }}
    """


# Shared style for a toolbar's outlined controls (pills, search, icons).
CONTROL_BTN_QSS = f"""
    QPushButton {{
        background: transparent; color: #b8bdc7;
        border: 1px solid {OUTLINE}; border-radius: 6px;
        padding: 0 10px; font-size: 11px; font-weight: 600;
    }}
    QPushButton:hover {{ color: {TEXT}; border-color: {OUTLINE_HOVER}; }}
    QPushButton:pressed {{ background: {ROW_HOVER}; }}
    QPushButton:checked {{
        background: {ACCENT}; color: #ffffff; border-color: {ACCENT};
    }}
    QPushButton:checked:hover {{ background: #4a91fa; }}
"""

# Square icon buttons (⟳ / ⚙) — same outline, no horizontal padding.
ICON_BTN_QSS = f"""
    QPushButton {{
        background: transparent; color: #b8bdc7;
        border: 1px solid {OUTLINE}; border-radius: 6px;
        padding: 0; font-size: 14px;
    }}
    QPushButton:hover {{ color: {TEXT}; border-color: {OUTLINE_HOVER}; }}
    QPushButton:pressed {{ background: {ROW_HOVER}; }}
"""

# ── Brushes / pens ────────────────────────────────────────────────────────────
BAND_BRUSH           = QBrush(QColor(BG_BAND))
ROW_HOVER_BRUSH      = QBrush(QColor(ROW_HOVER))
ROW_GROUP_ALT_BRUSH  = QBrush(QColor(ROW_GROUP_ALT))
SELECTION_BRUSH      = QBrush(QColor(SELECTION_BG))
ROW_SEP_PEN          = QPen(QColor(ROW_SEP))


# ── Helpers ───────────────────────────────────────────────────────────────────

def apply_table_chrome(view: QTableView) -> None:
    """Give *view* the shared table look.

    Also turns on viewport mouse tracking: without it the hover delegates
    below only see a MouseMove while a button is held, so row hover
    silently does nothing.  Every table must go through here.
    """
    view.setAlternatingRowColors(False)
    view.setShowGrid(False)
    # Elide rather than wrap: a value too wide for its column must not
    # silently double the row's height and break the row rhythm.
    view.setWordWrap(False)
    view.setFont(TABLE_FONT)
    view.setStyleSheet(TABLE_QSS)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(ROW_H)
    view.viewport().setMouseTracking(True)
    view.horizontalHeader().setFont(COL_HEADER_FONT)


def default_column_width(fm: QFontMetrics, hdr_fm: QFontMetrics,
                         sample: str, caption: str = "") -> int:
    """Width that fits a column's widest value *and* its caption.

    The uppercase tracked-out captions are wider than they look, so sizing
    from the sample value alone truncates the header.
    """
    return max(fm.horizontalAdvance(sample),
               hdr_fm.horizontalAdvance(caption.upper())) + CELL_PAD


def header_alignment(section: int, right_align_cols) -> int:
    """TextAlignmentRole for a column caption — captions sit over their
    values, so numeric columns read right and text columns left."""
    align = (Qt.AlignmentFlag.AlignRight if section in right_align_cols
             else Qt.AlignmentFlag.AlignLeft)
    return int(align | Qt.AlignmentFlag.AlignVCenter)


def disable_click_selection(view: QTableView) -> None:
    """Stop mouse clicks from selecting rows — hover is the only row cue.

    ``NoSelection`` blocks *user* selection only: the current index still
    follows clicks (so Delete-key handlers that fall back to it keep
    working) and programmatic ``selectionModel().select()`` still works,
    which is how the Trades hide flows target rows.
    """
    view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)


def clear_selections(*views: QTableView) -> None:
    """Drop any selection in *views* — used when a tab stops being visible."""
    for view in views:
        if view is not None and view.selectionModel() is not None:
            view.selectionModel().clearSelection()


def paint_separator_line(painter: QPainter, rect: QRect) -> None:
    """The 1px rule along a cell's bottom edge."""
    painter.save()
    painter.setPen(ROW_SEP_PEN)
    y = rect.bottom()
    painter.drawLine(rect.left(), y, rect.right(), y)
    painter.restore()


# ══════════════════════════════════════════════════════════════════════════════
#  RowHoverDelegate – shared row-hover tracking
# ══════════════════════════════════════════════════════════════════════════════

class RowHoverDelegate(QStyledItemDelegate):
    """Tracks which row the cursor is on, for whole-row hover highlights.

    Qt's ``State_MouseOver`` is set only on the cell under the pointer,
    never the whole row, so hover is tracked here from the viewport's
    mouse events instead.  Subclasses say which rows may highlight via
    :meth:`_is_hoverable`.
    """

    #: Fill for the hovered row.  Overridable — the Trades blotter uses the
    #: selection fill so hover and selection read the same there.
    hover_brush = ROW_HOVER_BRUSH

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hover_row: int | None = None
        view = self.parent()
        if view is not None and hasattr(view, "viewport"):
            view.viewport().installEventFilter(self)

    @property
    def hover_row(self) -> int | None:
        return self._hover_row

    def _set_hover_row(self, row: int | None) -> None:
        if row == self._hover_row:
            return
        self._hover_row = row
        view = self.parent()
        if view is not None and hasattr(view, "viewport"):
            view.viewport().update()

    def eventFilter(self, obj, event) -> bool:
        """Track the hovered row.  Purely visual — never consumes the event."""
        etype = event.type()
        if etype == QEvent.Type.MouseMove:
            view = self.parent()
            pos = event.position().toPoint() if hasattr(event, "position") \
                else event.pos()
            idx = view.indexAt(pos)
            self._set_hover_row(
                idx.row() if idx.isValid() and self._is_hoverable(idx)
                else None)
        elif etype == QEvent.Type.Leave:
            self._set_hover_row(None)
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  FooterAwareRowDelegate – hover + separators, footer band excluded
# ══════════════════════════════════════════════════════════════════════════════

class FooterAwareRowDelegate(RowHoverDelegate):
    """Row hover plus the 1px rule between adjacent data rows.

    A row answering True to ``IsFooterRowRole`` is a totals band: it
    carries its own background, so it neither highlights on hover nor
    takes a rule on either edge.  A model with no footer row degrades to
    plain hover + separators, which is what the flat trades table wants.
    """

    @staticmethod
    def _is_footer(index) -> bool:
        return bool(index.data(IsFooterRowRole))

    @classmethod
    def _is_hoverable(cls, index) -> bool:
        return not cls._is_footer(index)

    def _wants_separator(self, index, below) -> bool:
        """Whether a rule belongs under *index*, given the row below it.

        Subclasses that carry their own row structure (grouped rows, say)
        narrow this down; by default every pair of data rows gets one.
        """
        return True

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:
        super().initStyleOption(option, index)
        if (index.row() == self._hover_row
                and not option.state & QStyle.StateFlag.State_Selected):
            option.backgroundBrush = self.hover_brush

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index) -> None:
        super().paint(painter, option, index)
        if self._is_footer(index):
            return
        below = index.sibling(index.row() + 1, index.column())
        if not below.isValid() or self._is_footer(below):
            return
        if not self._wants_separator(index, below):
            return
        paint_separator_line(painter, option.rect)


# ══════════════════════════════════════════════════════════════════════════════
#  GroupBandDelegate – alternating band per run of equal group key
# ══════════════════════════════════════════════════════════════════════════════

class GroupBandDelegate(FooterAwareRowDelegate):
    """Tints consecutive rows sharing a group key, alternating per run.

    Rows inside a run share a background and carry no rule between them;
    the rule falls on the run's last row instead, so the tint and the line
    say the same thing.  Hover and selection still win over the band.

    The parity is computed in *view* coordinates — the proxy filters, so
    rows adjacent in the source may not be adjacent on screen.
    """

    #: Role the rows answer with their group identity.  Subclass to point
    #: at a different one.
    group_key_role: int = GroupKeyRole

    def __init__(self, parent=None):
        super().__init__(parent)
        self._parity: list[int] = []
        self._dirty = True
        model = parent.model() if parent is not None else None
        if model is not None:
            # Structural changes only.  A dataChanged (a commission
            # arriving, say) rewrites a row but never its order key.
            for sig in (model.modelReset, model.layoutChanged,
                        model.rowsInserted, model.rowsRemoved):
                sig.connect(self._invalidate)

    def _invalidate(self, *_args) -> None:
        self._dirty = True

    def _key_at(self, row: int):
        model = self.parent().model()
        return model.index(row, 0).data(self.group_key_role)

    def _rebuild(self) -> None:
        model = self.parent().model()
        parity: list[int] = []
        prev_key = object()   # sentinel: never equal to a real key
        band = 1              # flipped to 0 by the first row
        for row in range(model.rowCount()):
            key = self._key_at(row)
            if key != prev_key:
                band ^= 1
                prev_key = key
            parity.append(band)
        self._parity = parity
        self._dirty = False

    def _band_for(self, row: int) -> int:
        if self._dirty:
            self._rebuild()
        return self._parity[row] if 0 <= row < len(self._parity) else 0

    def _wants_separator(self, index, below) -> bool:
        # Only where the group changes.
        return (index.data(self.group_key_role)
                != below.data(self.group_key_role))

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:
        super().initStyleOption(option, index)
        if (index.row() != self._hover_row
                and not option.state & QStyle.StateFlag.State_Selected
                and self._band_for(index.row())):
            option.backgroundBrush = ROW_GROUP_ALT_BRUSH
