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
BG_PRIMARY = "#1A1D24"
BG_SECONDARY = "#1E222A"
BG_HEADER = "#14171D"
BORDER = "#2A2E37"
TEXT = "#C9D1D9"
TEXT_DIM = "#8B949E"

BLUE = "#58A6FF"
GREEN = Theme.ACCENT
ORANGE = "#D29922"

_TOOLBAR_BTN = f"""
    QPushButton {{
        background-color: transparent;
        color: {TEXT};
        border: 1px solid #30363D;
        border-radius: 4px;
        padding: 6px 14px;
        font-weight: 500;
        font-size: 13px;
    }}
    QPushButton:hover {{ background-color: #2A2E38; }}
    QPushButton:pressed {{ background-color: {BG_PRIMARY}; }}
    QPushButton:disabled {{ background-color: {BG_PRIMARY}; color: #484F58; border-color: {BORDER}; }}
    QPushButton:checked {{ background-color: {BLUE}; color: #ffffff; border: none; font-weight: bold; }}
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

_ACTION_BTN = f"""
    QPushButton {{
        background-color: {BLUE};
        color: #ffffff;
        border: none;
        border-radius: 4px;
        padding: 6px 14px;
        font-weight: bold;
        font-size: 13px;
    }}
    QPushButton:hover {{ background-color: #4b97f0; }}
    QPushButton:pressed {{ background-color: #2a72d1; }}
    QPushButton:disabled {{ background-color: #1A2A3A; color: #484F58; }}
"""

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
        alternate-background-color: {BG_SECONDARY};
        color: {TEXT};
        gridline-color: {BORDER};
        border: 1px solid {BORDER};
        font-size: 12px;
        selection-background-color: #253545;
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
    QTableWidget::item {{
        padding: 4px 8px;
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
_COL_TICK = 10
_SAVED_COLS = [
    "ConID", "Symbol", "SecType", "Exchange", "Currency",
    "Expiry", "Strike", "Right", "LocalSymbol", "Multiplier", "Tick",
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

# ── Futures symbol → minimum tick size ────────────────────────────────────────
_FUT_TICK: dict[str, float] = {
    "MNQ": 0.25, "NQ":  0.25,
    "MES": 0.25, "ES":  0.25,
    "M2K": 0.1,  "RTY": 0.1,
    "MYM": 1.0,  "YM":  1.0,
    "MGC": 0.1,  "GC":  0.1,
    "MCL": 0.01, "CL":  0.01,
}


def _tick_for(symbol: str, sec_type: str) -> float:
    """Return the minimum tick size for *symbol* / *sec_type*.

    * STK / OPT → 0.01
    * FUT       → looked up in ``_FUT_TICK``; defaults to 0.25 for unknowns
    """
    if sec_type in ("STK", "OPT"):
        return 0.01
    return _FUT_TICK.get(symbol.strip().upper(), 0.25)



# ── Main view ─────────────────────────────────────────────────────────────────

class ContractsView(QWidget):
    """
    Contract Universe page – find stocks / futures / options and
    persist them for quick lookup during order entry.
    """
    contracts_changed = Signal()
    # Emitted after a contract deletion that updated/disabled hot buttons
    # so the OrderEntryPanel can reload its in-memory list and rebuild
    # the grid (greying out disabled cells, refreshing tooltips, etc.).
    hot_buttons_changed = Signal()

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
        # Contracts can now change outside this page (search-popup
        # auto-save, chip-removal cleanup).  Rebuild the saved table on
        # those external changes — otherwise a later whole-table save
        # from this page would resurrect deleted rows.  The flag skips
        # the rebuild for this page's own saves.
        self._saving_contracts = False
        from src.core.reference_data import get_instance as _rd
        _rd().contracts_changed.connect(self._on_external_contracts_changed)

    def _setup_ui(self) -> None:
        self.setStyleSheet(
            f"ContractsView {{ background-color: {BG_PRIMARY}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # ── Top pane: Search Card ─────────────────────────────────────────────
        top_pane = QWidget()
        top_pane.setStyleSheet(
            f"QWidget {{ background-color: {BG_PRIMARY}; }}")
        top_layout = QVBoxLayout(top_pane)
        top_layout.setContentsMargins(0, 0, 0, 0)

        lbl_title = QLabel("Find Contracts")
        lbl_title.setFont(QFont("Helvetica Neue, Arial",
                          18, QFont.Weight.DemiBold))
        lbl_title.setStyleSheet(
            f"QLabel {{ color: {TEXT}; background: transparent; }}")
        top_layout.addWidget(lbl_title)

        search_card = self._build_search_card()
        top_layout.addWidget(search_card)
        top_layout.addStretch()

        # ── Bottom pane: Saved Contracts ───────────────────────────────────────
        bottom_pane = QWidget()
        bottom_pane.setStyleSheet(
            f"QWidget {{ background-color: {BG_PRIMARY}; }}")
        bottom_layout = QVBoxLayout(bottom_pane)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(10)

        lbl_saved = QLabel("Saved Contracts (available for Order Entry)")
        lbl_saved.setFont(QFont("Helvetica Neue, Arial",
                          14, QFont.Weight.DemiBold))
        lbl_saved.setStyleSheet(
            f"QLabel {{ color: {TEXT}; background: transparent; }}")
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
        btn_stk.setStyleSheet(
            _TOOLBAR_BTN + "QPushButton { border-top-right-radius: 0; border-bottom-right-radius: 0; }")

        btn_fut = QPushButton("Future")
        btn_fut.setCheckable(True)
        btn_fut.setStyleSheet(
            _TOOLBAR_BTN + "QPushButton { border-radius: 0; border-left: none; border-right: none; }")

        btn_opt = QPushButton("Option")
        btn_opt.setCheckable(True)
        btn_opt.setStyleSheet(
            _TOOLBAR_BTN + "QPushButton { border-top-left-radius: 0; border-bottom-left-radius: 0; }")

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
        self._stack.setStyleSheet(
            "QStackedWidget { background: transparent; border: none; }")

        # Page 0: STK
        stk_page = QWidget()
        stk_layout = QHBoxLayout(stk_page)
        stk_layout.setContentsMargins(0, 0, 0, 0)
        self._stk = {}
        self._stk['symbol'] = self._make_inline_field(
            stk_layout, "Symbol", "e.g. AAPL")
        self._stk['exchange'] = self._make_inline_field(
            stk_layout, "Exchange", "SMART")
        self._stk['currency'] = self._make_inline_field(
            stk_layout, "Currency", "USD")
        self._stk['symbol'].returnPressed.connect(self._on_find_stk)
        stk_layout.addStretch()
        self._stack.addWidget(stk_page)

        # Page 1: FUT
        fut_page = QWidget()
        fut_layout = QHBoxLayout(fut_page)
        fut_layout.setContentsMargins(0, 0, 0, 0)
        self._fut = {}
        self._fut['symbol'] = self._make_inline_field(
            fut_layout, "Symbol", "e.g. NQ")
        self._fut['exchange'] = self._make_inline_field(
            fut_layout, "Exchange", "CME")
        self._fut['currency'] = self._make_inline_field(
            fut_layout, "Currency", "USD")
        self._fut['symbol'].returnPressed.connect(self._on_find_fut)
        self._fut['symbol'].textChanged.connect(
            lambda text: self._on_fut_symbol_changed(text, self._fut["exchange"]))
        fut_layout.addStretch()
        self._stack.addWidget(fut_page)

        # Page 2: OPT
        opt_page = QWidget()
        opt_layout = QHBoxLayout(opt_page)
        opt_layout.setContentsMargins(0, 0, 0, 0)
        self._opt = {}
        self._opt['symbol'] = self._make_inline_field(
            opt_layout, "Symbol", "e.g. AAPL")
        self._opt['exchange'] = self._make_inline_field(
            opt_layout, "Exchange", "SMART")
        self._opt['currency'] = self._make_inline_field(
            opt_layout, "Currency", "USD")
        self._opt['expiry'] = self._make_inline_field(
            opt_layout, "Expiry", f"{date.today().year}{date.today().month:02d}")
        self._opt['strike'] = self._make_inline_field(
            opt_layout, "Strike", "0.00")

        opt_right_layout = QVBoxLayout()
        lbl = QLabel("Right")
        lbl.setStyleSheet(
            f"QLabel {{ color: {TEXT_DIM}; font-size: 11px; background: transparent; border: none; font-weight: bold; text-transform: uppercase; }}")
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
        lbl.setStyleSheet(
            f"QLabel {{ color: {TEXT_DIM}; font-size: 11px; background: transparent; border: none; font-weight: bold; text-transform: uppercase; }}")
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
        table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(True)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        table.setStyleSheet(_TABLE_STYLE)

        for col, w in enumerate([100, 100, 80, 100, 80, 100, 80, 60, 160, 80, 60]):
            table.setColumnWidth(col, w)

        return table

    # ── Find handlers (one per section) ───────────────────────────────────────

    def _on_find_stk(self) -> None:
        w = self._stk
        symbol = w["symbol"].text().strip().upper()
        if not symbol:
            QMessageBox.warning(self, "Missing Symbol",
                                "Please enter a stock symbol.")
            return
        params = {
            "sec_type": "STK",
            "symbol": symbol,
            "exchange": w["exchange"].text().strip() or "SMART",
            "currency": w["currency"].text().strip() or "USD",
        }
        self._do_find(params, w["btn"], "🔍 Find Stock")

    def _on_find_fut(self) -> None:
        w = self._fut
        symbol = w["symbol"].text().strip().upper()
        if not symbol:
            QMessageBox.warning(self, "Missing Symbol",
                                "Please enter a futures symbol.")
            return
        params = {
            "sec_type": "FUT",
            "symbol": symbol,
            "exchange": w["exchange"].text().strip() or "",
            "currency": w["currency"].text().strip() or "USD",
        }
        self._do_find(params, w["btn"], "🔍 Find Future")

    def _on_find_opt(self) -> None:
        w = self._opt
        symbol = w["symbol"].text().strip().upper()
        if not symbol:
            QMessageBox.warning(self, "Missing Symbol",
                                "Please enter an option symbol.")
            return
        params = {
            "sec_type": "OPT",
            "symbol": symbol,
            "exchange": w["exchange"].text().strip() or "SMART",
            "currency": w["currency"].text().strip() or "USD",
            "expiry": w["expiry"].text().strip(),
            "strike": w["strike"].text().strip(),
            "right": w["right"].currentText(),
        }
        self._do_find(params, w["btn"], "🔍 Find Option")

    # ── Common find logic ─────────────────────────────────────────────────────

    def _find_workers_by_priority(self) -> list[IBWorkerThread]:
        """Return connected workers sorted by config priority (1 = highest)."""
        from src.core import config_manager
        ordered_labels = config_manager.get_labels_by_priority()
        workers: list[IBWorkerThread] = []
        for label in ordered_labels:
            w = self._active_threads.get(label)
            if w is not None and w.isRunning() and w.is_ib_connected:
                workers.append(w)
        # Include any connected workers whose label wasn't in the config
        for label, w in self._active_threads.items():
            if w not in workers and w.isRunning() and w.is_ib_connected:
                workers.append(w)
        return workers

    def _do_find(self, params: dict, btn: QPushButton, btn_label: str) -> None:
        workers = self._find_workers_by_priority()
        if not workers:
            log.warning("No connected worker found among %d active thread(s)",
                        len(self._active_threads))
            QMessageBox.warning(
                self, "No Connection",
                "No active TWS connection.\n"
                "Go to Connect and connect first.",
            )
            return

        btn.setEnabled(False)
        btn.setText("⏳ Searching…")
        self._active_btn = btn
        self._active_btn_label = btn_label

        # Store retry queue: remaining workers to try on failure
        self._pending_workers = workers[1:]  # skip the first, we use it now
        self._pending_params = params

        if not hasattr(self, "_find_timer"):
            self._find_timer = QTimer(self)
            self._find_timer.setSingleShot(True)
            self._find_timer.timeout.connect(self._on_find_timeout)
        self._find_timer.start(20_000)

        self._start_find_with_worker(workers[0], params)

    def _start_find_with_worker(self, worker: IBWorkerThread, params: dict) -> None:
        """Wire signals to *worker* and kick off the browse request."""
        # Disconnect previous worker signals
        if self._connected_worker is not None:
            try:
                self._connected_worker.browse_results.disconnect(
                    self._on_find_results)
            except (RuntimeError, TypeError):
                pass
            try:
                self._connected_worker.browse_failed.disconnect(
                    self._on_find_failed)
            except (RuntimeError, TypeError):
                pass

        self._connected_worker = worker
        worker.browse_results.connect(self._on_find_results)
        worker.browse_failed.connect(self._on_find_failed)
        log.info("Trying worker %s for find request", worker._label)
        worker.request_browse(params)

    def _reset_btn(self) -> None:
        if self._active_btn is not None:
            self._active_btn.setEnabled(True)
            self._active_btn.setText(self._active_btn_label)
            self._active_btn = None

    def _on_find_timeout(self) -> None:
        log.warning("Find contract timed out (no response from worker)")
        self._reset_btn()
        QMessageBox.warning(
            self, "Search Timed Out",
            "No response from TWS within 20 seconds.\n"
            "The connection may have dropped — please check "
            "the Connect page and try again.",
        )

    def _on_find_results(self, results: list[dict]) -> None:
        if hasattr(self, "_find_timer"):
            self._find_timer.stop()
        self._reset_btn()

        if not results:
            QMessageBox.information(
                self, "Find Contract", "No contracts found.")
            return

        existing_conids: set[str] = set()
        for row_idx in range(self._saved_table.rowCount()):
            item = self._saved_table.item(row_idx, _COL_CONID)
            if item:
                existing_conids.add(item.text())

        if len(results) == 1:
            c = results[0]
            conid_str = str(c.get("conid", ""))
            if conid_str in existing_conids:
                QMessageBox.information(
                    self, "Already Saved",
                    f"{c.get('symbol', '')} ({c.get('local_symbol', '')}) "
                    f"is already in the list.",
                )
                return
            self._add_contract_row(c)
            self._save_all_contracts()
            self._reload_saved_table()
            log.info("Find: auto-added single result %s (conId=%s)",
                     c.get("symbol"), c.get("conid"))
        else:
            dlg = BrowseDialog(results, existing_conids, self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                selected = dlg.selected_contracts()
                added = 0
                for c in selected:
                    if str(c["conid"]) in existing_conids:
                        continue
                    self._add_contract_row(c)
                    existing_conids.add(str(c["conid"]))
                    added += 1
                if added:
                    self._save_all_contracts()
                    self._reload_saved_table()
                    log.info(
                        "Find: added %d contract(s) from browse dialog", added)

    def _on_find_failed(self, error: str) -> None:
        # Disconnect current worker signals
        if self._connected_worker is not None:
            try:
                self._connected_worker.browse_results.disconnect(
                    self._on_find_results)
            except (RuntimeError, TypeError):
                pass
            try:
                self._connected_worker.browse_failed.disconnect(
                    self._on_find_failed)
            except (RuntimeError, TypeError):
                pass

        # Try next priority worker if available
        pending = getattr(self, "_pending_workers", [])
        params = getattr(self, "_pending_params", None)
        if pending and params:
            next_worker = pending.pop(0)
            log.info("Find failed on worker %s (%s), retrying with next priority worker %s",
                     getattr(self._connected_worker, '_label', '?'), error,
                     next_worker._label)
            self._start_find_with_worker(next_worker, params)
            return

        # All workers exhausted — show error
        if hasattr(self, "_find_timer"):
            self._find_timer.stop()
        self._reset_btn()
        log.warning("Find failed (all workers exhausted): %s", error)
        QMessageBox.critical(self, "Find Failed", error)

    # ── Table management ──────────────────────────────────────────────────────

    def _add_contract_row(self, c: dict) -> None:
        row = self._saved_table.rowCount()
        self._saved_table.insertRow(row)

        # Compute tick size: use saved value if present, otherwise derive it
        sec_type = c.get("sec_type", "")
        symbol   = c.get("symbol", "")
        raw_tick = c.get("tick_size", "") or c.get("min_tick", "")
        try:
            tick_val = float(raw_tick) if raw_tick else _tick_for(symbol, sec_type)
        except (ValueError, TypeError):
            tick_val = _tick_for(symbol, sec_type)
        # Format without trailing zeros (e.g. 0.25 not 0.250000)
        tick_str = f"{tick_val:g}"

        values = [
            str(c.get("conid", "")),
            symbol,
            sec_type,
            c.get("exchange", ""),
            c.get("currency", ""),
            c.get("expiry", ""),
            str(c.get("strike", "")) if c.get("strike") else "",
            c.get("right", ""),
            c.get("local_symbol", ""),
            c.get("multiplier", ""),
            tick_str,
        ]
        for col, val in enumerate(values):
            item = QTableWidgetItem(val)
            if col == _COL_SECTYPE:
                if val == "STK":
                    item.setForeground(QColor(BLUE))
                elif val == "FUT":
                    item.setForeground(QColor(GREEN))
                elif val == "OPT":
                    item.setForeground(QColor(ORANGE))
            if col == _COL_TICK:
                item.setForeground(QColor(TEXT_DIM))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._saved_table.setItem(row, col, item)

    def _on_remove_selected(self) -> None:
        rows = sorted(set(idx.row()
                      for idx in self._saved_table.selectedIndexes()), reverse=True)
        if not rows:
            return

        # Compute hot-button impact BEFORE removing the rows so we can
        # show the user what will change and let them cancel.  Returns
        # the new hot-button list to persist (or None if no impact).
        new_hot_buttons = self._review_hot_button_impact(rows)
        if new_hot_buttons is None:
            # User cancelled the impact dialog — abort the deletion.
            return

        for r in rows:
            self._saved_table.removeRow(r)
        self._save_all_contracts()
        log.info("Removed %d contract(s)", len(rows))

        # Persist any hot-button updates produced by the impact review.
        # Empty list means "there were no impacted hot buttons" — skip.
        if new_hot_buttons:
            try:
                from src.core import config_manager as _cm
                _cm.save_hot_buttons(new_hot_buttons)
                self.hot_buttons_changed.emit()
                log.info("Updated hot buttons after contract deletion")
            except Exception:
                log.exception(
                    "Failed to persist hot-button updates after deletion")

    def _review_hot_button_impact(
            self, rows_to_delete: list[int]) -> list[dict] | None:
        """Compute the impact of *rows_to_delete* on saved hot buttons,
        confirm with the user via a dialog, and return the updated
        hot-button list.

        Returns:
          * ``None``  — user cancelled (caller should abort the deletion).
          * ``[]``    — no impacted hot buttons (caller deletes silently).
          * ``[...]`` — impacted hot buttons; caller deletes contracts then
                        persists this updated list.

        Replacement rule: when a deleted FUT row has at least one
        surviving same-root saved row, hot buttons whose ``symbol`` field
        targeted the deleted contract are rewritten to the surviving
        local symbol.  When no replacement remains, those hot buttons
        get ``disabled: True`` and render greyed-out.
        """
        from src.core import config_manager as _cm
        try:
            hot_buttons = _cm.load_hot_buttons()
        except Exception:
            log.exception("Could not load hot buttons; skipping impact review")
            return []

        # Snapshot rows about to be deleted (uppercase for matching).
        deleted: list[dict] = []
        for r in rows_to_delete:
            deleted.append({
                "symbol":       (self._cell(r, _COL_SYMBOL) or "").upper().strip(),
                "sec_type":     (self._cell(r, _COL_SECTYPE) or "").upper().strip(),
                "local_symbol": (self._cell(r, _COL_LOCAL) or "").upper().strip(),
                "expiry":       self._cell(r, _COL_EXPIRY) or "",
            })

        # Snapshot rows that will SURVIVE the deletion, indexed by root.
        # For each surviving row we keep both local_symbol and expiry so
        # the resolver below can mirror pick_active_contract's "soonest
        # non-expired" rule.
        delete_set = set(rows_to_delete)
        survivors_by_root: dict[str, list[dict]] = {}
        survivor_locals: set[str] = set()
        for row_idx in range(self._saved_table.rowCount()):
            if row_idx in delete_set:
                continue
            root = (self._cell(row_idx, _COL_SYMBOL) or "").upper().strip()
            sec_type = (self._cell(row_idx, _COL_SECTYPE) or "").upper().strip()
            if sec_type != "FUT":
                continue
            local = self._cell(row_idx, _COL_LOCAL) or ""
            survivor_locals.add(local.upper().strip())
            survivors_by_root.setdefault(root, []).append({
                "local_symbol": local,
                "expiry":       self._cell(row_idx, _COL_EXPIRY) or "",
            })

        def _resolves_post_delete(sym: str) -> dict | None:
            """Return the contract (from survivors) that *sym* would
            resolve to after the deletion, or None.  Mirrors
            ReferenceData.pick_active_contract: exact local match wins,
            otherwise root match → soonest non-expired."""
            up = sym.upper().strip()
            if up in survivor_locals:
                # Exact local-symbol match — find which row it is.
                for survivors in survivors_by_root.values():
                    for s in survivors:
                        if (s.get("local_symbol", "") or "").upper().strip() == up:
                            return s
            survivors = survivors_by_root.get(up, [])
            if survivors:
                return self._choose_replacement(survivors)
            return None

        # Walk hot buttons, decide replace / disable per entry.  Buttons
        # whose symbol is the still-surviving root of a deleted contract
        # are left untouched (auto-rollover via pick_active_contract).
        # Buttons pinned to a deleted local symbol are re-pointed when
        # any same-root survivor exists, otherwise disabled.
        impact: list[tuple[int, str, str, dict]] = []
        new_buttons: list[dict] = [dict(b) for b in hot_buttons]
        for i, btn in enumerate(new_buttons):
            btn_sym = (btn.get("symbol", "") or "").upper().strip()
            if not btn_sym:
                continue
            # Was this button pointing at a row we're about to delete?
            target = next((d for d in deleted
                           if d["sec_type"] == "FUT"
                           and btn_sym in (d["symbol"], d["local_symbol"])),
                          None)
            if target is None:
                continue

            # Direct post-deletion resolution (exact local match in any
            # survivor, or root match producing a surviving row).
            direct = _resolves_post_delete(btn_sym)
            if direct is not None:
                # btn_sym still resolves on its own — no change needed.
                # Most common case: a root-symbol button whose root has
                # other surviving expirations.
                continue

            # Direct resolution failed.  If the deleted contract's root
            # has any survivor, re-point this button at the survivor's
            # local symbol.  Otherwise disable it.
            same_root_survivors = survivors_by_root.get(target["symbol"], [])
            if same_root_survivors:
                replacement = self._choose_replacement(same_root_survivors)
                new_local = (replacement.get("local_symbol", "") or "").strip()
                if new_local:
                    btn["symbol"] = new_local
                    btn["disabled"] = False
                    impact.append((i, "replace",
                                   f"{btn_sym} → {new_local}", btn))
                    continue
            btn["disabled"] = True
            impact.append((i, "disable",
                           f"{btn_sym} (no replacement)", btn))

        if not impact:
            return []

        if not self._confirm_hot_button_impact(deleted, impact):
            return None
        return new_buttons

    @staticmethod
    def _choose_replacement(survivors: list[dict]) -> dict:
        """Pick the best replacement contract from *survivors* (same root).
        Mirrors ReferenceData.pick_active_contract's FUT rule: soonest
        non-expired by expiry; falls back to latest expiry when none are
        future."""
        from datetime import date, datetime
        from zoneinfo import ZoneInfo
        et_today = datetime.now(ZoneInfo("America/New_York")).date()

        def _parse(s: str) -> date | None:
            s = (s or "").strip()
            if len(s) == 8 and s.isdigit():
                try:
                    return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
                except ValueError:
                    return None
            if len(s) == 6 and s.isdigit():
                try:
                    from calendar import monthrange
                    y, m = int(s[0:4]), int(s[4:6])
                    return date(y, m, monthrange(y, m)[1])
                except ValueError:
                    return None
            return None

        future = [(d, s) for s in survivors
                  if (d := _parse(s.get("expiry", ""))) is not None
                  and d >= et_today]
        if future:
            future.sort(key=lambda t: t[0])
            return future[0][1]
        dated = [(d, s) for s in survivors
                 if (d := _parse(s.get("expiry", ""))) is not None]
        if dated:
            dated.sort(key=lambda t: t[0], reverse=True)
            return dated[0][1]
        return survivors[0]

    def _confirm_hot_button_impact(
            self,
            deleted: list[dict],
            impact: list[tuple[int, str, str, dict]]) -> bool:
        """Show the deletion-impact dialog and return True if the user
        chose to proceed.  Lists every impacted hot button with a clear
        before/after; users can cancel and keep the contracts/buttons
        intact."""
        deleted_fut_locals = ", ".join(
            d["local_symbol"] or d["symbol"]
            for d in deleted if d["sec_type"] == "FUT"
        ) or "(no futures rows)"

        # Lazy import: _auto_hot_button_label lives in order_entry_panel
        # and we don't want a top-level import cycle.
        from src.ui.widgets.order_entry_panel import _auto_hot_button_label
        replace_lines: list[str] = []
        disable_lines: list[str] = []
        for _i, action, detail, btn in impact:
            label = btn.get("label", "") or _auto_hot_button_label(
                side=btn.get("side", "BUY"),
                qty=btn.get("qty", ""),
                sym=btn.get("symbol", ""),
                ord_type=btn.get("ord_type", "LMT"),
                anchor=btn.get("anchor", "BID"),
                offset=int(btn.get("offset", 0) or 0),
                tp=btn.get("tp"),
                sl=btn.get("sl"),
            )
            if action == "replace":
                replace_lines.append(f"  • {label}   →   {detail}")
            else:
                disable_lines.append(f"  • {label}   →   {detail}")

        msg = [f"Deleting: {deleted_fut_locals}", ""]
        if replace_lines:
            msg.append("Hot buttons that will be re-pointed to the next "
                       "available contract:")
            msg.extend(replace_lines)
            msg.append("")
        if disable_lines:
            msg.append("Hot buttons that will be DISABLED (no replacement "
                       "contract is saved):")
            msg.extend(disable_lines)
            msg.append("")
        msg.append("Proceed with the deletion?")

        reply = QMessageBox.question(
            self,
            "Hot buttons reference deleted contracts",
            "\n".join(msg),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _on_clear_all(self) -> None:
        if self._saved_table.rowCount() == 0:
            return
        reply = QMessageBox.question(
            self, "Clear All",
            "Remove all saved contracts?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._saved_table.setRowCount(0)
            self._save_all_contracts()
            log.info("Cleared all contracts")

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_all_contracts(self) -> None:
        contracts: list[dict[str, Any]] = []
        for row in range(self._saved_table.rowCount()):
            contracts.append({
                "conid":        self._cell(row, _COL_CONID),
                "symbol":       self._cell(row, _COL_SYMBOL),
                "sec_type":     self._cell(row, _COL_SECTYPE),
                "exchange":     self._cell(row, _COL_EXCHANGE),
                "currency":     self._cell(row, _COL_CURRENCY),
                "expiry":       self._cell(row, _COL_EXPIRY),
                "strike":       self._cell(row, _COL_STRIKE),
                "right":        self._cell(row, _COL_RIGHT),
                "local_symbol": self._cell(row, _COL_LOCAL),
                "multiplier":   self._cell(row, _COL_MULTIPLIER),
                "tick_size":    self._cell(row, _COL_TICK),
            })
        # Sort by SecType then Symbol for consistent display order
        _SEC_ORDER = {"STK": 0, "FUT": 1, "OPT": 2}
        contracts.sort(key=lambda c: (_SEC_ORDER.get(
            c.get("sec_type", ""), 9), c.get("symbol", "")))
        # The table doesn't display primary_exchange / name — carry them
        # over from the stored rows (matched by conid) so a table save
        # doesn't wipe them.
        stored = {str(c.get("conid", "")): c
                  for c in config_manager.load_contracts()
                  if c.get("conid")}
        for c in contracts:
            prev = stored.get(str(c.get("conid", "")))
            if prev is not None:
                c["primary_exchange"] = prev.get("primary_exchange", "")
                c["name"] = prev.get("name", "")
        config_manager.save_contracts(contracts)
        from src.core.reference_data import get_instance as _rd
        self._saving_contracts = True
        try:
            _rd().reload_contracts()
        finally:
            self._saving_contracts = False
        self.contracts_changed.emit()

    def _cell(self, row: int, col: int) -> str:
        item = self._saved_table.item(row, col)
        return item.text() if item else ""

    def _load_saved_contracts(self) -> None:
        contracts = config_manager.load_contracts()
        _SEC_ORDER = {"STK": 0, "FUT": 1, "OPT": 2}
        contracts.sort(key=lambda c: (_SEC_ORDER.get(
            c.get("sec_type", ""), 9), c.get("symbol", "")))
        log.info("Loading %d saved contract(s)", len(contracts))
        for c in contracts:
            self._add_contract_row(c)

    def _reload_saved_table(self) -> None:
        """Clear and rebuild the table from the (sorted) persisted contracts."""
        self._saved_table.setRowCount(0)
        self._load_saved_contracts()

    def _on_external_contracts_changed(self) -> None:
        """Contracts changed outside this page — rebuild the saved table."""
        if self._saving_contracts:
            return
        self._reload_saved_table()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(BG_PRIMARY))
        p.end()


# ── Browse Dialog ──────────────────────────────────────────────────────────────

_BROWSE_COLS = [
    "", "ConID", "Symbol", "SecType", "Exchange", "Currency",
    "Expiry", "Strike", "Right", "LocalSymbol", "Multiplier",
]
# Column index of the Expiry header — used as the default sort key in
# BrowseDialog so multi-result futures lists land in calendar order.
_BROWSE_COL_EXPIRY = 6

_BROWSE_TABLE_STYLE = f"""
    QTableWidget {{
        background-color: {Theme.BG_MAIN};
        alternate-background-color: {Theme.BG_PANEL};
        color: {Theme.TEXT};
        gridline-color: {Theme.BORDER};
        border: 1px solid {Theme.BORDER};
        font-size: 12px;
        selection-background-color: #253545;
        selection-color: {Theme.TEXT};
    }}
    QHeaderView::section {{
        background-color: {Theme.BG_INPUT};
        color: {Theme.TEXT_DIM};
        border: none;
        border-bottom: 1px solid {Theme.BORDER};
        border-right: 1px solid {Theme.BORDER};
        padding: 5px 8px;
        font-weight: 600;
        font-size: 11px;
    }}
    QTableWidget::item {{
        padding: 4px 8px;
    }}
    QScrollBar:vertical {{
        background: {Theme.BG_MAIN};
        width: 8px;
    }}
    QScrollBar::handle:vertical {{
        background: #3A3E48;
        border-radius: 4px;
        min-height: 20px;
    }}
    QScrollBar:horizontal {{
        background: {Theme.BG_MAIN};
        height: 8px;
    }}
    QScrollBar::handle:horizontal {{
        background: #3A3E48;
        border-radius: 4px;
        min-width: 20px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        height: 0; width: 0;
    }}
"""

_BROWSE_ADD_SS = f"""
    QPushButton {{
        background-color: #1A3A2A;
        color: {Theme.ACCENT};
        border: 1px solid #2A4A3A;
        border-radius: 4px;
        padding: 6px 14px;
        font-weight: 500;
        font-size: 13px;
    }}
    QPushButton:hover {{ background-color: #244A34; }}
    QPushButton:pressed {{ background-color: #1A3A2A; }}
"""

_BROWSE_CANCEL_SS = f"""
    QPushButton {{
        background-color: #22262E;
        color: {Theme.TEXT};
        border: 1px solid {Theme.BORDER};
        border-radius: 4px;
        padding: 6px 14px;
        font-weight: 500;
        font-size: 13px;
    }}
    QPushButton:hover {{ background-color: #2A2E38; }}
    QPushButton:pressed {{ background-color: #22262E; }}
"""


class BrowseDialog(QDialog):
    """Modal dialog showing browse results with checkboxes for multi-select."""

    def __init__(
        self,
        contracts: list[dict],
        existing_conids: set[str],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(
            f"Browse Results — {len(contracts)} contract(s) found")
        self.setMinimumSize(900, 500)
        self.resize(1050, 600)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {Theme.BG_MAIN};
                color: {Theme.TEXT};
            }}
        """)

        self._contracts = contracts
        self._existing_conids = existing_conids

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        info = QLabel(
            f"Found {len(contracts)} contract(s). Check the ones you want to add:")
        info.setStyleSheet(
            f"QLabel {{ color: {Theme.TEXT_DIM}; font-size: 12px; background: transparent; }}")
        layout.addWidget(info)

        self._table = QTableWidget(len(contracts), len(_BROWSE_COLS))
        self._table.setHorizontalHeaderLabels(_BROWSE_COLS)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        # Click-to-sort on column headers.  Toggled off while populating
        # so the row indices line up with ``contracts`` during the build,
        # then re-enabled with an initial Expiry-ASC sort below.
        self._table.setSortingEnabled(False)
        self._table.horizontalHeader().setSortIndicatorShown(True)
        self._table.horizontalHeader().setSectionsClickable(True)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        self._table.setStyleSheet(_BROWSE_TABLE_STYLE)

        for col, w in enumerate([32, 120, 70, 60, 80, 70, 90, 70, 50, 280, 70]):
            self._table.setColumnWidth(col, w)

        for row, c in enumerate(contracts):
            chk_item = QTableWidgetItem()
            conid_str = str(c.get("conid", ""))
            if conid_str in existing_conids:
                chk_item.setCheckState(Qt.CheckState.Unchecked)
                chk_item.setFlags(chk_item.flags() & ~
                                  Qt.ItemFlag.ItemIsEnabled)
                chk_item.setToolTip("Already saved")
            else:
                chk_item.setCheckState(Qt.CheckState.Unchecked)
            # Persist the original list index on the checkbox item so
            # selected_contracts() can map back to the right dict after
            # the user re-sorts the table by clicking a column header.
            chk_item.setData(Qt.ItemDataRole.UserRole, row)
            self._table.setItem(row, 0, chk_item)

            values = [
                conid_str,
                c.get("symbol", ""),
                c.get("sec_type", ""),
                c.get("exchange", ""),
                c.get("currency", ""),
                c.get("expiry", ""),
                str(c.get("strike", "")) if c.get("strike") else "",
                c.get("right", ""),
                c.get("local_symbol", ""),
                c.get("multiplier", ""),
            ]
            for col, val in enumerate(values, start=1):
                item = QTableWidgetItem(val)
                if col == 3:  # SecType
                    if val == "FUT":
                        item.setForeground(QColor(GREEN))
                    elif val == "OPT":
                        item.setForeground(QColor(ORANGE))
                # Strike column sorts numerically rather than lex —
                # otherwise "100" lands before "99" because '1' < '9'.
                # Stored on UserRole so the default Qt sort comparator
                # uses it; Expiry strings (YYYYMMDD) already lex-sort
                # correctly, so no extra handling needed there.
                if col == 7:  # Strike
                    try:
                        item.setData(Qt.ItemDataRole.UserRole,
                                     float(val) if val else 0.0)
                    except (TypeError, ValueError):
                        item.setData(Qt.ItemDataRole.UserRole, 0.0)
                if conid_str in existing_conids:
                    item.setForeground(QColor(TEXT_DIM))
                self._table.setItem(row, col, item)

        # Re-enable sorting and land on Expiry-ASC as the default order
        # (most browse results are for futures month chains, where the
        # natural reading order is calendar).  The user can click any
        # header to override.
        self._table.setSortingEnabled(True)
        self._table.sortItems(
            _BROWSE_COL_EXPIRY, Qt.SortOrder.AscendingOrder)

        layout.addWidget(self._table)

        btn_bar = QHBoxLayout()
        btn_bar.setSpacing(8)

        btn_select_all = QPushButton("Select All")
        btn_select_all.setStyleSheet(_BROWSE_CANCEL_SS)
        btn_select_all.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_select_all.clicked.connect(self._select_all)
        btn_bar.addWidget(btn_select_all)

        btn_deselect = QPushButton("Deselect All")
        btn_deselect.setStyleSheet(_BROWSE_CANCEL_SS)
        btn_deselect.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_deselect.clicked.connect(self._deselect_all)
        btn_bar.addWidget(btn_deselect)

        btn_bar.addStretch()

        btn_add = QPushButton("✅ Add Selected")
        btn_add.setStyleSheet(_BROWSE_ADD_SS)
        btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_add.clicked.connect(self.accept)
        btn_bar.addWidget(btn_add)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setStyleSheet(_BROWSE_CANCEL_SS)
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.clicked.connect(self.reject)
        btn_bar.addWidget(btn_cancel)

        layout.addLayout(btn_bar)

    def _select_all(self) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.flags() & Qt.ItemFlag.ItemIsEnabled:
                item.setCheckState(Qt.CheckState.Checked)

    def _deselect_all(self) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.flags() & Qt.ItemFlag.ItemIsEnabled:
                item.setCheckState(Qt.CheckState.Unchecked)

    def selected_contracts(self) -> list[dict]:
        """Return the list of contract dicts the user checked.

        Reads the original-index pointer stored on the checkbox item
        rather than the live row index — sorting the table moves rows,
        so a row→list lookup would point at the wrong contract."""
        selected: list[dict] = []
        for row in range(self._table.rowCount()):
            chk = self._table.item(row, 0)
            if not chk or chk.checkState() != Qt.CheckState.Checked:
                continue
            src = chk.data(Qt.ItemDataRole.UserRole)
            try:
                idx = int(src)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(self._contracts):
                selected.append(self._contracts[idx])
        return selected
