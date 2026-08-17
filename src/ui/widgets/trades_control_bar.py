"""
TradesControlBar – toolbar that lives above the Trades table.

Hosts:
  * ``Summary`` pill button (checkable) — flips QStackedWidget pages.
  * Date range combo (Today / Last N Days, up to 7).
  * Search line-edit — filters by symbol / side / account / sec-type.
  * Gear button — opens the columns preferences dialog.

Pure view layer: emits Qt signals; holds no state beyond widget values.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from src.ui.theme import Theme
from . import table_theme as tt

# ── Theme tokens ──────────────────────────────────────────────────────────────
# Mirrors the Positions control bar (account_position_panel.CONTROL_BTN_QSS)
# — the two toolbars sit in the same tab-bar corner and swap on tab change,
# so any divergence reads as a glitch.
ACCENT     = Theme.POS_ACCENT
BG_INPUT   = "#161a24"
BORDER     = Theme.POS_OUTLINE
BORDER_HOV = "#4a5464"
TEXT       = Theme.POS_TEXT
TEXT_DIM   = Theme.POS_TEXT_2
TEXT_CTRL  = "#b8bdc7"
ROW_HOVER  = Theme.POS_ROW_HOVER


_BAR_QSS = f"""
    QWidget#tradesControlBar {{
        background: transparent;
        border: none;
    }}
    QLabel#tbTradeCount {{
        color: {Theme.POS_LABEL};
        font-family: Inter, "Helvetica Neue", Arial, sans-serif;
        font-size: 11px;
        font-weight: 500;
    }}
    QLabel#tbTradeCount[hasTrades="true"] {{
        color: {TEXT_DIM};
    }}
    QPushButton#tbSummaryPill {{
        background: transparent;
        color: {TEXT_CTRL};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 0 10px;
        font-size: 11px;
        font-weight: 600;
    }}
    QPushButton#tbSummaryPill:hover {{
        color: {TEXT};
        border-color: {BORDER_HOV};
    }}
    QPushButton#tbSummaryPill:checked {{
        background: {ACCENT};
        color: #ffffff;
        border-color: {ACCENT};
    }}
    QPushButton#tbSummaryPill:checked:hover {{
        background: #4a91fa;
    }}
    QComboBox#tbDateCombo {{
        background: {BG_INPUT};
        color: {TEXT_CTRL};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 2px 6px;
        font-family: Inter, "Helvetica Neue", Arial, sans-serif;
        font-size: 11px;
    }}
    QComboBox#tbDateCombo:hover {{
        border-color: {BORDER_HOV};
        color: {TEXT};
    }}
    QComboBox#tbDateCombo:focus {{
        border-color: {ACCENT};
    }}
    QComboBox#tbDateCombo QAbstractItemView {{
        background: {BG_INPUT};
        color: {TEXT};
        border: 1px solid {BORDER};
        selection-background-color: {ACCENT};
        selection-color: #FFFFFF;
        outline: none;
    }}
    QLineEdit#tbSearch {{
        background: {BG_INPUT};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 3px 8px;
        font-family: Inter, "Helvetica Neue", Arial, sans-serif;
        font-size: 11px;
        min-width: {tt.CTRL_SEARCH_W}px;
        max-width: {tt.CTRL_SEARCH_W}px;
    }}
    QLineEdit#tbSearch:hover {{
        border-color: {BORDER_HOV};
    }}
    QLineEdit#tbSearch:focus {{
        border-color: {ACCENT};
    }}
    QPushButton#tbGear {{
        background: transparent;
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 0;
        color: {TEXT_CTRL};
        font-size: 14px;
    }}
    QPushButton#tbGear:hover {{
        color: {TEXT};
        border-color: {BORDER_HOV};
    }}
    QPushButton#tbGear:pressed {{
        background: {ROW_HOVER};
    }}
"""


class TradesControlBar(QWidget):
    """Top toolbar for the Trades tab.

    Signals
    -------
    summaryToggled(bool)
    dateFilterChanged(str)   # preset key: evening / afternoon / morning
                              # / today / last_2 … last_7
    searchTextChanged(str)
    settingsClicked()
    """

    summaryToggled       = Signal(bool)
    dateFilterChanged    = Signal(str)
    searchTextChanged    = Signal(str)
    settingsClicked      = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("tradesControlBar")
        # No fixed height: the tab-bar corner is only as tall as the tab
        # row, and a taller-than-available bar gets its rows nudged down
        # relative to the Positions bar — a 2px vertical hop on the
        # Summary pill when you switch tabs.
        self.setStyleSheet(_BAR_QSS)
        self._build_ui()

    def _build_ui(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(*tt.CTRL_MARGINS)
        lay.setSpacing(tt.CTRL_SPACING)
        # Stretch first so the controls cluster on the right side of the
        # tab-bar corner (the stack can be wider than our content).
        lay.addStretch(1)

        # Visible-trade count.  Host panel updates this whenever the
        # flat proxy's rowCount changes (fills arrive, filter/search
        # changes).  Sits left of the Summary toggle.
        self._trades_count_label = QLabel("0 trds", self)
        self._trades_count_label.setObjectName("tbTradeCount")
        self._trades_count_label.setProperty("hasTrades", False)
        lay.addWidget(self._trades_count_label)

        lay.addSpacing(10)

        # Summary pill button — same style/size as the Pos view's pills
        # (shared width helper).  Where it *lands* is settled by
        # ``table_theme.align_summary_pills``, which pads whichever bar
        # carries less to its right; both bars keep their own control
        # order.
        self._summary_toggle = QPushButton("Summary", self)
        self._summary_toggle.setObjectName("tbSummaryPill")
        self._summary_toggle.setCheckable(True)
        self._summary_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._summary_toggle.setFixedHeight(tt.CTRL_H)
        self._summary_toggle.setFixedWidth(
            tt.pill_width(self._summary_toggle.font()))
        self._summary_toggle.toggled.connect(self.summaryToggled.emit)
        lay.addWidget(self._summary_toggle)
        pad_summary = tt.align_pad()
        lay.addItem(pad_summary)

        # Spacer
        lay.addSpacing(12)

        # Date / time-of-day filter.  Session-of-day presets sit at the
        # top because they're the most common quick filters during live
        # trading; the multi-day windows follow.  Items store string
        # keys as itemData — the panel maps them to (days_back,
        # time_of_day) when applying the filter.
        self._date_combo = QComboBox(self)
        self._date_combo.setObjectName("tbDateCombo")
        # Width tracks the widest item ("Last N Days") instead of a
        # hard-coded min-width.
        self._date_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._date_combo.addItem("Evening",   "evening")
        self._date_combo.addItem("Noon",      "afternoon")
        self._date_combo.addItem("Morning",   "morning")
        self._date_combo.addItem("Today",     "today")
        for n in range(2, 8):
            self._date_combo.addItem(f"Last {n} Days", f"last_{n}")
        # Default to Today so first-run doesn't hide fills behind a
        # time-of-day cutoff.  Set before wiring the signal to avoid
        # firing a spurious dateFilterChanged during construction.
        self._date_combo.setCurrentIndex(
            self._date_combo.findData("today"))
        self._date_combo.currentIndexChanged.connect(self._on_date_changed)
        lay.addWidget(self._date_combo)

        lay.addSpacing(8)

        # Alignment slack — see the pads in the Positions bar.
        pad_filter = tt.align_pad()
        lay.addItem(pad_filter)

        # Search field (plain text input; no leading icon).
        self._search = QLineEdit(self)
        self._search.setObjectName("tbSearch")
        self._search.setPlaceholderText("Search...")
        self._search.setFixedHeight(tt.CTRL_H)
        self._search.textChanged.connect(self.searchTextChanged.emit)
        lay.addWidget(self._search)
        # This one ends up carrying the Positions bar's refresh button —
        # the searches can't line up unless this bar leaves room for it.
        pad_search = tt.align_pad()
        lay.addItem(pad_search)

        lay.addSpacing(4)

        # Gear settings button
        self._gear = QPushButton("\u2699", self)   # ⚙
        self._gear.setObjectName("tbGear")
        self._gear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._gear.setFixedSize(tt.CTRL_ICON, tt.CTRL_ICON)
        self._gear.setToolTip("Column preferences")
        self._gear.clicked.connect(self.settingsClicked.emit)
        lay.addWidget(self._gear)

        # Hand-off for the tab container's alignment pass.  The date combo
        # takes the "filter" slot the Positions bar fills with Zero Pos.
        self.align_anchors = [
            ("summary", self._summary_toggle, pad_summary),
            ("filter",  self._date_combo,     pad_filter),
            ("search",  self._search,         pad_search),
        ]

    # ── Public getters / setters ────────────────────────────────────────────

    def isSummary(self) -> bool:
        return self._summary_toggle.isChecked()

    def setSummary(self, on: bool) -> None:
        self._summary_toggle.setChecked(bool(on))

    def setTradeCount(self, n: int) -> None:
        n = max(0, int(n))
        self._trades_count_label.setText(
            f"{n:,} trd" + ("s" if n != 1 else ""))
        # Trigger QSS re-evaluation so the color pops when there are
        # trades and dims back to muted grey when the view is empty.
        self._trades_count_label.setProperty("hasTrades", n > 0)
        self._trades_count_label.style().unpolish(self._trades_count_label)
        self._trades_count_label.style().polish(self._trades_count_label)

    def filterKey(self) -> str:
        return str(self._date_combo.currentData() or "today")

    def filterLabel(self) -> str:
        """Display text of the active date filter ("Today", "Noon", …)."""
        return self._date_combo.currentText()

    def setFilterKey(self, key: str) -> None:
        idx = self._date_combo.findData(key)
        if idx >= 0:
            self._date_combo.setCurrentIndex(idx)

    def searchText(self) -> str:
        return self._search.text()

    # ── Internal slots ──────────────────────────────────────────────────────

    def _on_date_changed(self, _idx: int) -> None:
        self.dateFilterChanged.emit(self.filterKey())
