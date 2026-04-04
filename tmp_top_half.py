# top_half.py - The new UI layout and styles for contracts_view.py
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QButtonGroup,
)

from src.core import config_manager
from src.core.ib_worker import IBWorkerThread
from src.ui.theme import Theme

log = logging.getLogger(__name__)

# ── Theme tokens mapping ──────────────────────────────────────────────────────
BG_PRIMARY = Theme.BG_MAIN
BG_SECONDARY = Theme.BG_PANEL
BG_HEADER = Theme.BG_INPUT
BORDER = Theme.BORDER
TEXT = Theme.TEXT
TEXT_DIM = Theme.TEXT_DIM

_TOOLBAR_BTN = f"""
    QPushButton {{
        background-color: {BG_SECONDARY};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 6px 14px;
        font-weight: 500;
        font-size: 13px;
    }}
    QPushButton:hover {{ background-color: #2A2E38; border: 1px solid #484F58; }}
    QPushButton:pressed {{ background-color: {BG_PRIMARY}; }}
    QPushButton:disabled {{ background-color: {BG_PRIMARY}; color: #484F58; border-color: {BORDER}; }}
    QPushButton:checked {{ background-color: {Theme.ACCENT}; color: #ffffff; border: none; font-weight: bold; }}
"""

_REMOVE_SS = f"""
    QPushButton {{
        background-color: #2A1A1A;
        color: {Theme.CLR_SELL};
        border: 1px solid #3A2A2A;
        border-radius: 4px;
        padding: 6px 14px;
        font-weight: 500;
        font-size: 12px;
    }}
    QPushButton:hover {{ background-color: #3A2424; }}
    QPushButton:pressed {{ background-color: #2A1A1A; }}
"""
_NEUTRAL_SS = _TOOLBAR_BTN

_ACTION_BTN = _TOOLBAR_BTN.replace(BG_SECONDARY, Theme.ACCENT).replace(TEXT, "#ffffff").replace(BORDER, Theme.ACCENT)

_INPUT_SS = f"""
    QLineEdit {{
        background-color: {BG_PRIMARY};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 5px 10px;
        font-size: 13px;
    }}
    QLineEdit:focus {{
        border-color: {Theme.ACCENT};
    }}
    QLineEdit:disabled {{
        background-color: {BG_HEADER};
        color: #484F58;
    }}
"""

_COMBO_SS = f"""
    QComboBox {{
        background-color: {BG_PRIMARY};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 5px 10px;
        font-size: 13px;
        min-width: 60px;
    }}
    QComboBox:focus {{
        border-color: {Theme.ACCENT};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 20px;
    }}
    QComboBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {TEXT_DIM};
        margin-right: 6px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {BG_SECONDARY};
        color: {TEXT};
        border: 1px solid {BORDER};
        selection-background-color: #253545;
        selection-color: {TEXT};
        outline: none;
    }}
"""

_TABLE_STYLE = f"""
    QTableWidget {{
        background-color: {BG_PRIMARY};
        alternate-background-color: {BG_PRIMARY};
        color: {TEXT};
        gridline-color: transparent;
        border: 1px solid {BORDER};
        border-radius: 4px;
        font-size: 13px;
        selection-background-color: #253545;
        selection-color: {TEXT};
    }}
    QHeaderView::section {{
        background-color: {BG_HEADER};
        color: {TEXT_DIM};
        border: none;
        border-bottom: 1px solid {BORDER};
        padding: 8px 12px;
        font-weight: bold;
        font-size: 12px;
        text-transform: uppercase;
    }}
    QTableWidget::item {{
        padding: 8px 12px;
        border-bottom: 1px solid #22262E;
    }}
    QScrollBar:vertical {{ background: {BG_PRIMARY}; width: 8px; }}
    QScrollBar::handle:vertical {{ background: #3A3E48; border-radius: 4px; min-height: 20px; }}
    QScrollBar:horizontal {{ background: {BG_PRIMARY}; height: 8px; }}
    QScrollBar::handle:horizontal {{ background: #3A3E48; border-radius: 4px; min-width: 20px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
"""

# Saved-contracts table columns
_COL_CONID = 0
_COL_SYMBOL = 1
_COL_SECTYPE = 2
_COL_EXCHANGE = 3
_COL_CURRENCY = 4
_COL_EXPIRY = 5
_COL_STRIKE = 6
_COL_RIGHT = 7
_COL_LOCAL = 8
_COL_MULTIPLIER = 9
_SAVED_COLS = [
    "ConID", "Symbol", "SecType", "Exchange", "Currency",
    "Expiry", "Strike", "Right", "LocalSymbol", "Multiplier",
]


# ── Futures symbol → exchange lookup ──────────────────────────────────────────
_FUT_EXCHANGE: dict[str, str] = {
    "ES": "CME",   "MES": "CME",
    "NQ": "CME",   "MNQ": "CME",
    "RTY": "CME",  "M2K": "CME",
    "YM": "CBOT",  "MYM": "CBOT",
    "CL": "NYMEX", "MCL": "NYMEX",
    "GC": "COMEX", "MGC": "COMEX",
}


# ── Main view ─────────────────────────────────────────────────────────────────

class ContractsView(QWidget):
    """
    Contract Universe page – find stocks / futures / options and
    persist them for quick lookup during order entry.
    """
    contracts_changed = Signal()

    def __init__(
        self,
        active_threads: dict[str, IBWorkerThread],
        parent=None,
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._active_threads = active_threads
        self._connected_worker: IBWorkerThread | None = None
        self._active_btn: QPushButton | None = None
        self._active_btn_label: str = ""
        self._setup_ui()
        self._load_saved_contracts()

    def _setup_ui(self) -> None:
        self.setStyleSheet(f"ContractsView {{ background-color: {BG_PRIMARY}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # ── Top pane: Search Card ─────────────────────────────────────────────
        top_pane = QWidget()
        top_pane.setStyleSheet(f"QWidget {{ background-color: {BG_PRIMARY}; }}")
        top_layout = QVBoxLayout(top_pane)
        top_layout.setContentsMargins(0, 0, 0, 0)
        
        lbl_title = QLabel("Find Contracts")
        lbl_title.setFont(QFont("Helvetica Neue, Arial", 18, QFont.Weight.DemiBold))
        lbl_title.setStyleSheet(f"QLabel {{ color: {TEXT}; background: transparent; }}")
        top_layout.addWidget(lbl_title)

        search_card = self._build_search_card()
        top_layout.addWidget(search_card)
        top_layout.addStretch()

        # ── Bottom pane: Saved Contracts ───────────────────────────────────────
        bottom_pane = QWidget()
        bottom_pane.setStyleSheet(f"QWidget {{ background-color: {BG_PRIMARY}; }}")
        bottom_layout = QVBoxLayout(bottom_pane)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(10)

        lbl_saved = QLabel("Saved Contracts (available for Order Entry)")
        lbl_saved.setFont(QFont("Helvetica Neue, Arial", 14, QFont.Weight.DemiBold))
        lbl_saved.setStyleSheet(f"QLabel {{ color: {TEXT}; background: transparent; }}")
        bottom_layout.addWidget(lbl_saved)

        self._saved_table = self._build_saved_table()
        bottom_layout.addWidget(self._saved_table)

        bot_bar = QHBoxLayout()
        bot_bar.setSpacing(12)

        btn_remove = QPushButton("Remove Selected")
        btn_remove.setStyleSheet(_REMOVE_SS)
        btn_remove.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_remove.clicked.connect(self._on_remove_selected)
        bot_bar.addWidget(btn_remove)

        btn_clear = QPushButton("Clear All")
        btn_clear.setStyleSheet(_NEUTRAL_SS)
        btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_clear.clicked.connect(self._on_clear_all)
        bot_bar.addWidget(btn_clear)

        bot_bar.addStretch()
        bottom_layout.addLayout(bot_bar)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)
        splitter.setStyleSheet(f"""
            QSplitter {{ background-color: transparent; }}
            QSplitter::handle {{ background-color: transparent; }}
        """)
        splitter.addWidget(top_pane)
        splitter.addWidget(bottom_pane)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)

        layout.addWidget(splitter)

    # ── Search Card Builder ───────────────────────────────────────────────────

    def _build_search_card(self) -> QFrame:
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background-color: {BG_SECONDARY};
                border: 1px solid {BORDER};
                border-radius: 8px;
            }}
        """)
        vbox = QVBoxLayout(card)
        vbox.setContentsMargins(20, 20, 20, 20)
        vbox.setSpacing(16)

        # Segmented Control
        seg_layout = QHBoxLayout()
        seg_layout.setSpacing(0)
        self._btn_grp = QButtonGroup(self)
        
        btn_stk = QPushButton("Stock")
        btn_stk.setCheckable(True)
        btn_stk.setChecked(True)
        btn_stk.setStyleSheet(_TOOLBAR_BTN + "QPushButton { border-top-right-radius: 0; border-bottom-right-radius: 0; }")
        
        btn_fut = QPushButton("Future")
        btn_fut.setCheckable(True)
        btn_fut.setStyleSheet(_TOOLBAR_BTN + "QPushButton { border-radius: 0; border-left: none; border-right: none; }")
        
        btn_opt = QPushButton("Option")
        btn_opt.setCheckable(True)
        btn_opt.setStyleSheet(_TOOLBAR_BTN + "QPushButton { border-top-left-radius: 0; border-bottom-left-radius: 0; }")

        self._btn_grp.addButton(btn_stk, 0)
        self._btn_grp.addButton(btn_fut, 1)
        self._btn_grp.addButton(btn_opt, 2)
        
        seg_layout.addWidget(btn_stk)
        seg_layout.addWidget(btn_fut)
        seg_layout.addWidget(btn_opt)
        seg_layout.addStretch()
        
        vbox.addLayout(seg_layout)

        # Stacked Widget for Inputs
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("QStackedWidget { background: transparent; border: none; }")
        
        # Page 0: STK
        stk_page = QWidget()
        stk_layout = QHBoxLayout(stk_page)
        stk_layout.setContentsMargins(0, 0, 0, 0)
        self._stk = {}
        self._stk['symbol'] = self._make_inline_field(stk_layout, "Symbol", "e.g. AAPL")
        self._stk['exchange'] = self._make_inline_field(stk_layout, "Exchange", "SMART")
        self._stk['currency'] = self._make_inline_field(stk_layout, "Currency", "USD")
        self._stk['symbol'].returnPressed.connect(self._on_find_stk)
        stk_layout.addStretch()
        self._stack.addWidget(stk_page)

        # Page 1: FUT
        fut_page = QWidget()
        fut_layout = QHBoxLayout(fut_page)
        fut_layout.setContentsMargins(0, 0, 0, 0)
        self._fut = {}
        self._fut['symbol'] = self._make_inline_field(fut_layout, "Symbol", "e.g. NQ")
        self._fut['exchange'] = self._make_inline_field(fut_layout, "Exchange", "CME")
        self._fut['currency'] = self._make_inline_field(fut_layout, "Currency", "USD")
        self._fut['symbol'].returnPressed.connect(self._on_find_fut)
        self._fut['symbol'].textChanged.connect(lambda text: self._on_fut_symbol_changed(text, self._fut["exchange"]))
        fut_layout.addStretch()
        self._stack.addWidget(fut_page)

        # Page 2: OPT
        opt_page = QWidget()
        opt_layout = QHBoxLayout(opt_page)
        opt_layout.setContentsMargins(0, 0, 0, 0)
        self._opt = {}
        self._opt['symbol'] = self._make_inline_field(opt_layout, "Symbol", "e.g. AAPL")
        self._opt['exchange'] = self._make_inline_field(opt_layout, "Exchange", "SMART")
        self._opt['currency'] = self._make_inline_field(opt_layout, "Currency", "USD")
        self._opt['expiry'] = self._make_inline_field(opt_layout, "Expiry", f"{date.today().year}{date.today().month:02d}")
        self._opt['strike'] = self._make_inline_field(opt_layout, "Strike", "0.00")
        
        opt_right_layout = QVBoxLayout()
        lbl = QLabel("Right")
        lbl.setStyleSheet(f"QLabel {{ color: {TEXT_DIM}; font-size: 11px; background: transparent; border: none; font-weight: bold; text-transform: uppercase; }}")
        combo = QComboBox()
        combo.addItems(["", "C", "P"])
        combo.setStyleSheet(_COMBO_SS)
        opt_right_layout.addWidget(lbl)
        opt_right_layout.addWidget(combo)
        opt_layout.addLayout(opt_right_layout)
        
        self._opt['right'] = combo
        self._opt['symbol'].returnPressed.connect(self._on_find_opt)
        opt_layout.addStretch()
        self._stack.addWidget(opt_page)

        vbox.addWidget(self._stack)
        
        # Search Button Bar
        btn_bar = QHBoxLayout()
        btn_bar.addStretch()
        
        self._btn_search = QPushButton("Search Contracts")
        self._btn_search.setStyleSheet(_ACTION_BTN)
        self._btn_search.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_search.setMinimumWidth(150)
        self._btn_search.clicked.connect(self._on_search_clicked)
        btn_bar.addWidget(self._btn_search)
        
        vbox.addLayout(btn_bar)

        # Hook up button group
        self._btn_grp.idClicked.connect(self._stack.setCurrentIndex)
        
        # Expose active buttons for logic reuse
        self._stk['btn'] = self._btn_search
        self._fut['btn'] = self._btn_search
        self._opt['btn'] = self._btn_search

        return card

    def _make_inline_field(self, layout: QHBoxLayout, label: str, default: str = "") -> QLineEdit:
        col = QVBoxLayout()
        col.setSpacing(4)
        lbl = QLabel(label)
        lbl.setStyleSheet(f"QLabel {{ color: {TEXT_DIM}; font-size: 11px; background: transparent; border: none; font-weight: bold; text-transform: uppercase; }}")
        inp = QLineEdit()
        inp.setStyleSheet(_INPUT_SS)
        if default:
            if "e.g." in default:
                inp.setPlaceholderText(default)
            else:
                inp.setText(default)
        col.addWidget(lbl)
        col.addWidget(inp)
        layout.addLayout(col)
        return inp

    def _on_search_clicked(self):
        idx = self._stack.currentIndex()
        if idx == 0:
            self._on_find_stk()
        elif idx == 1:
            self._on_find_fut()
        elif idx == 2:
            self._on_find_opt()

    @staticmethod
    def _on_fut_symbol_changed(text: str, exch_input: QLineEdit) -> None:
        """Auto-populate exchange for known futures symbols."""
        symbol = text.strip().upper()
        mapped = _FUT_EXCHANGE.get(symbol, "")
        if mapped:
            exch_input.setText(mapped)
        elif not exch_input.text().strip():
            exch_input.setText("")

    def _build_saved_table(self) -> QTableWidget:
        table = QTableWidget(0, len(_SAVED_COLS))
        table.setHorizontalHeaderLabels(_SAVED_COLS)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)  # Remove aggressive grid lines
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.setStyleSheet(_TABLE_STYLE)

        for col, w in enumerate([100, 100, 80, 100, 80, 100, 80, 60, 160, 80]):
            table.setColumnWidth(col, w)

        return table
