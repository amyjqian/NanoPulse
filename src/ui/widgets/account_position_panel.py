"""
AccountPositionPanel – live portfolio positions in a QTableWidget.

Uses the same table-based styling as OrderStatusPanel, with per-row
action buttons (Flatten, Reverse, 25 %, 50 %, 75 %) and colour-coded
Side / P&L columns.
"""
from __future__ import annotations

import logging
import math

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

# ── Theme tokens (shared with OrderStatusPanel) ──────────────────────────────
BG_PANEL   = "#1e2230"
BG_TABLE   = "#1A1D24"
BG_TABLE_ALT = "#1E222A"
BG_HEADER  = "#14171D"
BORDER     = "#30363D"
TEXT       = "#C9D1D9"
TEXT_DIM   = "#8B949E"

CLR_LONG     = "#2e7d32"
CLR_SHORT    = "#c62828"
CLR_FLATTEN  = "#cc6600"
CLR_REVERSE  = "#8E44AD"
CLR_CLOSE    = "#8E8E93"
CLR_PNL_POS  = "#3FB950"
CLR_PNL_NEG  = "#F85149"
CLR_ACCT_ROW = "#181C28"
CLR_OPEN     = "#58A6FF"

# ── Fonts ─────────────────────────────────────────────────────────────────────
_TITLE_FONT = QFont("Helvetica Neue, Arial", 11)
_TITLE_FONT.setWeight(QFont.Weight.Bold)

_TABLE_FONT = QFont("Menlo, Consolas, monospace", 11)

_BTN_FONT = QFont("Helvetica Neue, Arial", 9)
_BTN_FONT.setWeight(QFont.Weight.Bold)

_ACCT_FONT = QFont("Helvetica Neue, Arial", 11)
_ACCT_FONT.setWeight(QFont.Weight.Bold)

# ── Column indices ────────────────────────────────────────────────────────────
COL_SYMBOL  = 0
COL_ACCOUNT = 1
COL_SIDE    = 2
COL_QTY     = 3
COL_AVG     = 4
COL_CURRENT = 5
COL_PNL     = 6
COL_ACTIONS = 7

_COLUMNS = ["Symbol", "Account", "Side", "Qty", "Avg", "Current", "P&L", ""]

# ── Table QSS (mirrors OrderStatusPanel) ─────────────────────────────────────
_TABLE_STYLE = f"""
    QTableWidget {{
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
        padding: 5px 8px;
        font-weight: 600;
        font-size: 11px;
    }}
    QTableWidget::item {{
        padding: 4px 8px;
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_action_btn(text: str, colour: str, min_h: int = 22) -> QPushButton:
    """Small action button for position rows."""
    btn = QPushButton(text)
    btn.setFixedHeight(min_h)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFont(_BTN_FONT)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {colour};
            color: #ffffff;
            border: none;
            border-radius: 3px;
            padding: 2px 8px;
            font-weight: bold;
            font-size: 9px;
        }}
        QPushButton:hover {{ border: 1px solid #ffffff30; }}
        QPushButton:pressed {{ border: 1px solid #ffffff60; }}
    """)
    return btn


def _fmt_price(val: float) -> str:
    if val == 0 or math.isnan(val):
        return "–"
    return f"${val:,.2f}"


def _fmt_pnl(val: float) -> str:
    if math.isnan(val):
        return "–"
    sign = "+" if val >= 0 else ""
    return f"{sign}${val:,.0f}"


def _fmt_bal(val: float) -> str:
    if val == 0 or math.isnan(val):
        return "–"
    if abs(val) >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if abs(val) >= 1_000:
        return f"${val / 1_000:.0f}k"
    return f"${val:,.0f}"


# ═══════════════════════════════════════════════════════════════════════════════
#  AccountPositionPanel
# ═══════════════════════════════════════════════════════════════════════════════

class AccountPositionPanel(QWidget):
    """Displays live portfolio positions in a QTableWidget grouped by account."""

    # Emitted when user clicks a position action button
    position_action_requested = Signal(dict)

    # Emitted when user clicks account-level Flatten
    account_action_requested = Signal(dict)

    # Emitted when user clicks Refresh button
    refresh_requested = Signal()

    def __init__(self, accounts_model=None, parent=None):
        super().__init__(parent)
        self._accounts_model = accounts_model
        # positions: (account, con_id) → dict with all position fields
        self._positions: dict[tuple[str, int], dict] = {}
        # account-level data: account → {"balance": float, "pnl": float}
        self._account_data: dict[str, dict] = {}

        # ── Row tracking ──
        # position_rows: (account, con_id) → table row index
        self._position_rows: dict[tuple[str, int], int] = {}
        # account_header_rows: account → table row index
        self._account_header_rows: dict[str, int] = {}

        # ── Coalesced rebuild timer ──
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(0)
        self._rebuild_timer.timeout.connect(self._rebuild)

        self._setup_ui()

    # ── UI construction ───────────────────────────────────────────────────────

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

        # ── Header row: title + refresh button ──
        header = QHBoxLayout()
        header.setSpacing(6)

        title = QLabel("Account Position")
        title.setFont(_TITLE_FONT)
        title.setStyleSheet("color: #ffffff; border: none;")
        header.addWidget(title)

        refresh_btn = QPushButton("⟳")
        refresh_btn.setToolTip("Refresh positions from TWS")
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
        root.addLayout(header)

        # ── Table ──
        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(True)
        self._table.setFont(_TABLE_FONT)
        self._table.setStyleSheet(_TABLE_STYLE)

        hdr = self._table.horizontalHeader()
        hdr.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        _default_widths = {
            COL_SYMBOL: 80, COL_ACCOUNT: 100, COL_SIDE: 60,
            COL_QTY: 50, COL_AVG: 90, COL_CURRENT: 90,
            COL_PNL: 75, COL_ACTIONS: 280,
        }
        for col, w in _default_widths.items():
            self._table.setColumnWidth(col, w)

        self._table.verticalHeader().setDefaultSectionSize(30)
        root.addWidget(self._table, stretch=1)

    # ── Public API ────────────────────────────────────────────────────────────

    def update_positions(self, batch: list[dict]) -> None:
        """Receive a batch of position dicts from a worker thread."""
        needs_rebuild = False
        for pos in batch:
            account = pos.get("account", "")
            con_id = pos.get("con_id", 0)
            if not account or not con_id:
                continue
            key = (account, con_id)
            old = self._positions.get(key)
            self._positions[key] = pos

            if key in self._position_rows and old is not None:
                new_pos = pos.get("position", 0)
                old_pos = old.get("position", 0)
                if new_pos == 0 and old_pos != 0:
                    needs_rebuild = True
                    log.debug("AcctPos: position closed %s → rebuild", pos.get("symbol"))
                elif new_pos != 0 and old_pos == 0:
                    needs_rebuild = True
                    log.debug("AcctPos: position reopened %s → rebuild", pos.get("symbol"))
                else:
                    self._update_row_in_place(key, pos)
            else:
                if pos.get("position", 0) != 0:
                    needs_rebuild = True
                    log.debug("AcctPos: new position %s qty=%.1f → rebuild",
                              pos.get("symbol"), pos.get("position", 0))

        # Update account header P&L in-place when no structural change
        if not needs_rebuild:
            self._update_all_account_headers_in_place()

        if needs_rebuild:
            self._schedule_rebuild()

    def set_account_data(self, account: str, balance: float, pnl: float) -> None:
        """Update header-level info for an account (balance, pnl)."""
        self._account_data[account] = {"balance": balance, "pnl": pnl}
        if account in self._account_header_rows:
            self._update_account_header_in_place(account)
        elif any(k[0] == account for k in self._positions):
            self._schedule_rebuild()

    # ── In-place update helpers ───────────────────────────────────────────────

    def _update_row_in_place(self, key: tuple[str, int], pos: dict) -> None:
        """Update cell text for an existing position row without rebuild."""
        row = self._position_rows.get(key)
        if row is None:
            return

        position = pos.get("position", 0)
        avg_cost = pos.get("avg_cost", 0)
        market_price = pos.get("market_price", 0)
        unrealized_pnl = pos.get("unrealized_pnl", 0)
        sec_type = pos.get("sec_type", "")

        display_avg = avg_cost
        if sec_type == "FUT" and position != 0 and market_price != 0:
            implied_mult = abs(pos.get("market_value", 0) / (position * market_price))
            if implied_mult > 1:
                display_avg = avg_cost / implied_mult

        qty = abs(int(position))

        item = self._table.item(row, COL_QTY)
        if item:
            item.setText(str(qty))

        item = self._table.item(row, COL_AVG)
        if item:
            item.setText(_fmt_price(display_avg))

        item = self._table.item(row, COL_CURRENT)
        if item:
            item.setText(_fmt_price(market_price))

        item = self._table.item(row, COL_PNL)
        if item:
            pnl_clr = CLR_PNL_POS if unrealized_pnl >= 0 else CLR_PNL_NEG
            item.setText(_fmt_pnl(unrealized_pnl))
            item.setForeground(QColor(pnl_clr))

    def _update_all_account_headers_in_place(self) -> None:
        for acct in self._account_header_rows:
            self._update_account_header_in_place(acct)

    def _update_account_header_in_place(self, account: str) -> None:
        """Update balance + P&L text in the account header spanning row."""
        row = self._account_header_rows.get(account)
        if row is None:
            return

        total_pnl = sum(
            p.get("unrealized_pnl", 0)
            for (acct, _), p in self._positions.items()
            if acct == account and p.get("position", 0) != 0
        )

        acct_info = self._account_data.get(account, {})
        bal = acct_info.get("balance", 0)

        widget = self._table.cellWidget(row, COL_SYMBOL)
        if widget is None:
            return

        bal_lbl = widget.findChild(QLabel, "bal_lbl")
        pnl_lbl = widget.findChild(QLabel, "pnl_lbl")

        if bal_lbl:
            if bal:
                bal_lbl.setText(f"Bal: {_fmt_bal(bal)}")
                bal_lbl.show()
            else:
                bal_lbl.hide()

        if pnl_lbl:
            pnl_clr = CLR_PNL_POS if total_pnl >= 0 else CLR_PNL_NEG
            pnl_lbl.setText(f"P&L: {_fmt_pnl(total_pnl)}")
            pnl_lbl.setStyleSheet(
                f"color:{pnl_clr};border:none;font-weight:bold;font-size:11px;")

    def _schedule_rebuild(self) -> None:
        if not self._rebuild_timer.isActive():
            self._rebuild_timer.start()

    def _on_refresh_clicked(self) -> None:
        """Clear all rows and ask the workers to re-fetch positions from TWS."""
        log.info("AcctPos: refresh requested by user")
        self.clear_positions()
        self.refresh_requested.emit()

    def clear_positions(self) -> None:
        """Remove every row and reset internal state."""
        self._table.setRowCount(0)
        self._position_rows.clear()
        self._account_header_rows.clear()
        self._positions.clear()
        self._account_data.clear()
        log.debug("AcctPos: cleared all positions")

    # ── Rebuild ───────────────────────────────────────────────────────────────

    def _rebuild(self) -> None:
        """Rebuild the entire table from self._positions."""
        self._position_rows.clear()
        self._account_header_rows.clear()
        self._table.setRowCount(0)

        # Group positions by account
        accounts: dict[str, list[dict]] = {}
        for (acct, _), pos in self._positions.items():
            if pos.get("position", 0) == 0:
                continue
            accounts.setdefault(acct, []).append(pos)

        if not accounts:
            return

        for acct in sorted(accounts.keys()):
            positions = accounts[acct]
            self._insert_account_header(acct, positions)
            for pos in sorted(positions, key=lambda p: p.get("symbol", "")):
                self._insert_position_row(acct, pos)

    # ── Account header row ────────────────────────────────────────────────────

    def _insert_account_header(self, account: str, positions: list[dict]) -> None:
        """Insert a spanning header row for one account."""
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setRowHeight(row, 34)
        self._account_header_rows[account] = row

        # Span all columns
        self._table.setSpan(row, 0, 1, len(_COLUMNS))

        # Build header widget
        widget = QWidget()
        widget.setStyleSheet(f"background:{CLR_ACCT_ROW};border:none;")
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(10)

        alias = self._get_alias(account)
        alias_lbl = QLabel(alias if alias else account)
        alias_lbl.setFont(_ACCT_FONT)
        alias_lbl.setStyleSheet("color:#ffffff;border:none;font-weight:bold;font-size:12px;")
        layout.addWidget(alias_lbl)

        if alias:
            acct_lbl = QLabel(account)
            acct_lbl.setStyleSheet(f"color:{TEXT_DIM};border:none;font-size:10px;")
            layout.addWidget(acct_lbl)

        layout.addStretch()

        acct_info = self._account_data.get(account, {})
        bal = acct_info.get("balance", 0)
        bal_lbl = QLabel(f"Bal: {_fmt_bal(bal)}" if bal else "")
        bal_lbl.setObjectName("bal_lbl")
        bal_lbl.setStyleSheet(f"color:{TEXT_DIM};border:none;font-size:11px;")
        bal_lbl.setVisible(bool(bal))
        layout.addWidget(bal_lbl)

        total_pnl = sum(p.get("unrealized_pnl", 0) for p in positions)
        pnl_clr = CLR_PNL_POS if total_pnl >= 0 else CLR_PNL_NEG
        pnl_lbl = QLabel(f"P&L: {_fmt_pnl(total_pnl)}")
        pnl_lbl.setObjectName("pnl_lbl")
        pnl_lbl.setStyleSheet(
            f"color:{pnl_clr};border:none;font-weight:bold;font-size:11px;")
        layout.addWidget(pnl_lbl)

        flatten_btn = _make_action_btn("× Flatten", CLR_FLATTEN, 24)
        flatten_btn.clicked.connect(
            lambda checked, a=account: self.account_action_requested.emit(
                {"action": "FLATTEN_ALL", "account": a}
            )
        )
        layout.addWidget(flatten_btn)

        self._table.setCellWidget(row, 0, widget)

    # ── Position row ──────────────────────────────────────────────────────────

    def _insert_position_row(self, account: str, pos: dict) -> None:
        """Insert one position row with data cells and action buttons."""
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setRowHeight(row, 30)

        con_id = pos.get("con_id", 0)
        key = (account, con_id)
        self._position_rows[key] = row

        symbol = pos.get("local_symbol") or pos.get("symbol", "")
        position = pos.get("position", 0)
        avg_cost = pos.get("avg_cost", 0)
        market_price = pos.get("market_price", 0)
        unrealized_pnl = pos.get("unrealized_pnl", 0)
        sec_type = pos.get("sec_type", "")

        display_avg = avg_cost
        if sec_type == "FUT" and position != 0 and market_price != 0:
            implied_mult = abs(pos.get("market_value", 0) / (position * market_price))
            if implied_mult > 1:
                display_avg = avg_cost / implied_mult

        side = "LONG" if position > 0 else "SHORT"
        qty = abs(int(position))

        alias = self._get_alias(account)

        # Symbol
        item = QTableWidgetItem(symbol)
        item.setForeground(QColor(TEXT))
        item.setFont(QFont("Menlo, Consolas, monospace", 11, QFont.Weight.Bold))
        self._table.setItem(row, COL_SYMBOL, item)

        # Account
        item = QTableWidgetItem(alias if alias else account)
        item.setForeground(QColor(TEXT_DIM))
        self._table.setItem(row, COL_ACCOUNT, item)

        # Side
        item = QTableWidgetItem(side)
        side_clr = CLR_PNL_POS if side == "LONG" else CLR_PNL_NEG
        item.setForeground(QColor(side_clr))
        item.setFont(QFont("Helvetica Neue, Arial", 11, QFont.Weight.Bold))
        self._table.setItem(row, COL_SIDE, item)

        # Qty
        item = QTableWidgetItem(str(qty))
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setForeground(QColor(TEXT))
        self._table.setItem(row, COL_QTY, item)

        # Avg
        item = QTableWidgetItem(_fmt_price(display_avg))
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setForeground(QColor(TEXT))
        self._table.setItem(row, COL_AVG, item)

        # Current
        item = QTableWidgetItem(_fmt_price(market_price))
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setForeground(QColor(TEXT))
        self._table.setItem(row, COL_CURRENT, item)

        # P&L
        pnl_clr = CLR_PNL_POS if unrealized_pnl >= 0 else CLR_PNL_NEG
        item = QTableWidgetItem(_fmt_pnl(unrealized_pnl))
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setForeground(QColor(pnl_clr))
        item.setFont(QFont("Menlo, Consolas, monospace", 11, QFont.Weight.Bold))
        self._table.setItem(row, COL_PNL, item)

        # Action buttons
        action_info = {
            "account": account,
            "con_id": con_id,
            "symbol": symbol,
            "position": position,
            "sec_type": sec_type,
            "exchange": pos.get("exchange", ""),
            "currency": pos.get("currency", "USD"),
        }

        btn_widget = QWidget()
        btn_widget.setStyleSheet("background:transparent;border:none;")
        btn_layout = QHBoxLayout(btn_widget)
        btn_layout.setContentsMargins(4, 2, 4, 2)
        btn_layout.setSpacing(3)

        flat_btn = _make_action_btn("× Flatten", CLR_FLATTEN, 22)
        flat_btn.clicked.connect(
            lambda checked, info=dict(action_info, action="FLATTEN"):
                self.position_action_requested.emit(info)
        )
        btn_layout.addWidget(flat_btn)

        rev_btn = _make_action_btn("↻ Reverse", CLR_REVERSE, 22)
        rev_btn.clicked.connect(
            lambda checked, info=dict(action_info, action="REVERSE"):
                self.position_action_requested.emit(info)
        )
        btn_layout.addWidget(rev_btn)

        for pct, label in [(25, "25%"), (50, "50%"), (75, "75%")]:
            btn = _make_action_btn(label, CLR_CLOSE, 22)
            btn.clicked.connect(
                lambda checked, info=dict(action_info, action=f"CLOSE_{pct}"):
                    self.position_action_requested.emit(info)
            )
            btn_layout.addWidget(btn)

        btn_layout.addStretch()
        self._table.setCellWidget(row, COL_ACTIONS, btn_widget)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_alias(self, account: str) -> str:
        """Get account alias from the accounts model."""
        if self._accounts_model is not None:
            return self._accounts_model._aliases.get(account, "")
        return ""

