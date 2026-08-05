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
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

# ── Theme tokens ──────────────────────────────────────────────────────────────
ACCENT     = "#E08A3C"
ACCENT_DIM = "#B8722F"
BG_PANEL   = "#151922"
BG_INPUT   = "#151922"
BORDER     = "#262E3F"
TEXT       = "#E6EBF2"
TEXT_DIM   = "#A3AFC2"

_BAR_HEIGHT = 32

_BAR_QSS = f"""
    QWidget#tradesControlBar {{
        background: transparent;
        border: none;
    }}
    QLabel#tbTradeCount {{
        color: {TEXT_DIM};
        font-family: Inter, "Helvetica Neue", Arial, sans-serif;
        font-size: 11px;
        font-weight: 500;
    }}
    QLabel#tbTradeCount[hasTrades="true"] {{
        color: {TEXT};
    }}
    QPushButton#tbSummaryPill {{
        background: transparent;
        color: {TEXT_DIM};
        border: 1px solid {BORDER};
        border-radius: 8px;
        padding: 0;
        font-size: 11px;
        font-weight: 600;
    }}
    QPushButton#tbSummaryPill:hover {{
        color: #ffffff;
        border-color: #5D8EF0;
    }}
    QPushButton#tbSummaryPill:checked {{
        background: #5D8EF0;
        color: #ffffff;
        border: none;
    }}
    QPushButton#tbSummaryPill:checked:hover {{
        background: #6E9AF2;
    }}
    QComboBox#tbDateCombo {{
        background: {BG_INPUT};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 2px 6px;
        font-family: Inter, "Helvetica Neue", Arial, sans-serif;
        font-size: 11px;
    }}
    QComboBox#tbDateCombo:focus {{
        border-color: {ACCENT};
    }}
    QComboBox#tbDateCombo QAbstractItemView {{
        background: {BG_INPUT};
        color: {TEXT};
        border: 1px solid {BORDER};
        selection-background-color: {ACCENT_DIM};
        selection-color: #FFFFFF;
        outline: none;
    }}
    QLineEdit#tbSearch {{
        background: {BG_INPUT};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 3px 8px;
        font-family: Inter, "Helvetica Neue", Arial, sans-serif;
        font-size: 11px;
        min-width: 60px;
        max-width: 60px;
    }}
    QLineEdit#tbSearch:focus {{
        border-color: {ACCENT};
    }}
    QPushButton#tbGear {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 4px;
        padding: 2px 6px;
        color: {TEXT_DIM};
        font-size: 14px;
    }}
    QPushButton#tbGear:hover {{
        color: {TEXT};
        border-color: {BORDER};
    }}
    QPushButton#tbGear:pressed {{
        background: {BG_INPUT};
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
        self.setFixedHeight(_BAR_HEIGHT)
        self.setStyleSheet(_BAR_QSS)
        self._build_ui()

    def _build_ui(self) -> None:
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 8, 2)
        lay.setSpacing(8)
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
        # (width sized to the wider of that view's two labels so the
        # Summary buttons line up when switching tabs).
        self._summary_toggle = QPushButton("Summary", self)
        self._summary_toggle.setObjectName("tbSummaryPill")
        self._summary_toggle.setCheckable(True)
        self._summary_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._summary_toggle.setFixedHeight(24)
        pill_font = QFont(self._summary_toggle.font())
        pill_font.setPixelSize(11)
        pill_font.setWeight(QFont.Weight.DemiBold)
        fm = QFontMetrics(pill_font)
        self._summary_toggle.setFixedWidth(
            max(fm.horizontalAdvance(t)
                for t in ("Summary", "Zero Pos")) + 22)
        self._summary_toggle.toggled.connect(self.summaryToggled.emit)
        lay.addWidget(self._summary_toggle)

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

        # Search field (plain text input; no leading icon).
        self._search = QLineEdit(self)
        self._search.setObjectName("tbSearch")
        self._search.setPlaceholderText("Search")
        self._search.textChanged.connect(self.searchTextChanged.emit)
        lay.addWidget(self._search)

        lay.addSpacing(4)

        # Gear settings button
        self._gear = QPushButton("\u2699", self)   # ⚙
        self._gear.setObjectName("tbGear")
        self._gear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._gear.setToolTip("Column preferences")
        self._gear.clicked.connect(self.settingsClicked.emit)
        lay.addWidget(self._gear)

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
