"""
TradesControlBar – toolbar that lives above the Trades table.

Hosts:
  * ``Summary`` toggle (pill-style checkbox) — flips QStackedWidget pages.
  * Date range combo (Today / Last N Days, up to 7).
  * Search line-edit — filters by symbol / side / account / sec-type.
  * Gear button — opens the columns preferences dialog.

Pure view layer: emits Qt signals; holds no state beyond widget values.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStyle,
    QWidget,
)

# ── Theme tokens ──────────────────────────────────────────────────────────────
ACCENT     = "#CC6600"
ACCENT_DIM = "#8A4500"
BG_PANEL   = "#1e2230"
BG_INPUT   = "#1A1D24"
BORDER     = "#30363D"
TEXT       = "#C9D1D9"
TEXT_DIM   = "#8B949E"

_BAR_HEIGHT = 38

_BAR_QSS = f"""
    QWidget#tradesControlBar {{
        background: {BG_PANEL};
        border: none;
        border-bottom: 1px solid {BORDER};
    }}
    QLabel#tbSummaryLabel {{
        color: {TEXT};
        font-family: "Helvetica Neue", Arial, sans-serif;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.04em;
    }}
    QCheckBox#tbSummaryToggle {{
        spacing: 0;
    }}
    QCheckBox#tbSummaryToggle::indicator {{
        width: 34px;
        height: 18px;
        border-radius: 9px;
        background: #30363D;
        border: 1px solid {BORDER};
    }}
    QCheckBox#tbSummaryToggle::indicator:hover {{
        border-color: #4A4E58;
    }}
    QCheckBox#tbSummaryToggle::indicator:checked {{
        background: {ACCENT};
        border-color: {ACCENT};
    }}
    QComboBox#tbDateCombo {{
        background: {BG_INPUT};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 2px 8px;
        min-width: 120px;
        font-family: "Helvetica Neue", Arial, sans-serif;
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
        padding: 3px 8px 3px 24px;
        font-family: "Helvetica Neue", Arial, sans-serif;
        font-size: 11px;
        min-width: 180px;
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
    dateFilterChanged(int)   # 1 = Today, 2..7 = Last N Days
    searchTextChanged(str)
    settingsClicked()
    """

    summaryToggled       = Signal(bool)
    dateFilterChanged    = Signal(int)
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
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(8)

        # Summary toggle + label (pair, label after switch)
        self._summary_toggle = QCheckBox(self)
        self._summary_toggle.setObjectName("tbSummaryToggle")
        self._summary_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._summary_toggle.toggled.connect(self.summaryToggled.emit)

        self._summary_label = QLabel("Summary", self)
        self._summary_label.setObjectName("tbSummaryLabel")

        # Clicking the label toggles the switch too
        self._summary_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._summary_label.mousePressEvent = self._toggle_from_label  # type: ignore[assignment]

        lay.addStretch(1)
        lay.addWidget(self._summary_toggle)
        lay.addWidget(self._summary_label)

        # Spacer
        lay.addSpacing(12)

        # Date filter
        self._date_combo = QComboBox(self)
        self._date_combo.setObjectName("tbDateCombo")
        self._date_combo.addItem("Today", 1)
        for n in range(2, 8):
            self._date_combo.addItem(f"Last {n} Days", n)
        self._date_combo.currentIndexChanged.connect(self._on_date_changed)
        lay.addWidget(self._date_combo)

        lay.addSpacing(8)

        # Search with overlaid magnifier
        self._search = QLineEdit(self)
        self._search.setObjectName("tbSearch")
        self._search.setPlaceholderText("Search")
        self._search.textChanged.connect(self.searchTextChanged.emit)
        # Icon inside the left padding
        style = self.style()
        try:
            search_icon = style.standardIcon(
                QStyle.StandardPixmap.SP_FileDialogContentsView)
        except Exception:
            search_icon = QIcon()
        if not search_icon.isNull():
            self._search.addAction(search_icon, QLineEdit.ActionPosition.LeadingPosition)
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

    def daysBack(self) -> int:
        return int(self._date_combo.currentData() or 1)

    def setDaysBack(self, days: int) -> None:
        for i in range(self._date_combo.count()):
            if self._date_combo.itemData(i) == days:
                self._date_combo.setCurrentIndex(i)
                return

    def searchText(self) -> str:
        return self._search.text()

    # ── Internal slots ──────────────────────────────────────────────────────

    def _on_date_changed(self, _idx: int) -> None:
        self.dateFilterChanged.emit(self.daysBack())

    def _toggle_from_label(self, _event) -> None:
        self._summary_toggle.toggle()
