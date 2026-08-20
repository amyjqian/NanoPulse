"""
AccountsTableModel – a single QAbstractTableModel that aggregates
real-time account data from every connected IBWorkerThread.

Design principles
─────────────────
* **Composite key** ``(connection_label, account)`` → row index for O(1) look-ups.
* **Surgical repaints** – only ``dataChanged`` is emitted per cell;
  ``layoutChanged`` is never emitted during streaming updates.
* **Formatting** – financial columns are right-aligned, formatted with commas
  and 2 decimal places.  IBKR "Infinity" values (> 1e100) are shown as ``"-"``.
* **Editable Alias** column – users can type a friendly name per account.
"""
from __future__ import annotations
from src.core import config_manager
from PySide6.QtGui import QColor, QFont
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
    Signal,
)

import logging
from typing import Any

log = logging.getLogger(__name__)


# Column indices
COL_CONNECTION = 0
COL_ACCOUNT = 1
COL_TYPE = 2
COL_ALIAS = 3
COL_NET_LIQ = 4
COL_TOTAL_CASH = 5
COL_EXCESS_LIQ = 6
COL_DAILY_PNL = 7

HEADERS = [
    "Connection",
    "Account",
    "Type",
    "Alias",
    "Net Liquidation",
    "Total Cash",
    "Excess Liq",
    "Daily P&L",
]

# Map IBKR tag names → our column indices
_TAG_COL_MAP: dict[str, int] = {
    "NetLiquidation": COL_NET_LIQ,
    "TotalCashValue": COL_TOTAL_CASH,
    "ExcessLiquidity": COL_EXCESS_LIQ,
    "DailyPnL": COL_DAILY_PNL,
}

_FINANCIAL_COLS = {
    COL_NET_LIQ, COL_TOTAL_CASH, COL_EXCESS_LIQ,
    COL_DAILY_PNL,
}

# P&L columns get green/red coloring based on positive/negative value
_PNL_COLS = {COL_DAILY_PNL}


# ── Account type detection ────────────────────────────────────────────────────

_PAPER_PREFIXES = ("DU", "DF")   # DU = individual paper, DF = FA demo
_LIVE_PREFIXES = ("U", "F", "I")  # U = individual, F = FA, I = institutional


def detect_account_type(account: str) -> str:
    """Return ``'Paper'``, ``'Live'``, or ``'—'`` based on IBKR account prefix."""
    acct = account.strip().upper()
    if acct.startswith(_PAPER_PREFIXES):
        return "Paper"
    if acct.startswith(_LIVE_PREFIXES):
        return "Live"
    return "—"


class AccountsTableModel(QAbstractTableModel):
    """Central aggregation model for all IBKR account summaries."""

    # A connection label flipped online/offline.  Dedicated signal so
    # listeners that only care about the dim/⚠ row state (e.g.
    # AccountListPanel) don't have to re-check it on every one of the
    # hundreds of per-tag ``dataChanged`` emits in a summary batch.
    connection_state_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._table_data: list[list[str]] = []
        self._row_map: dict[tuple[str, str], int] = {}
        self._acct_to_label: dict[str, str] = {}
        self._aliases: dict[str, str] = config_manager.load_aliases()
        # Buying power tracked separately (not shown as a column in Connections view)
        self._buying_power: dict[str, float] = {}   # account → buying power
        # Per-currency cash balances from the ``$LEDGER:ALL`` summary tag,
        # keyed account → {currency: balance}.  Not a table column — read by
        # AccountListPanel to render the "USD 6,300  JPY 5,000" line per row.
        self._cash_by_ccy: dict[str, dict[str, float]] = {}
        # Live TWS connection state per connection-label.  Updated by
        # ConnectionsView when the worker emits ``connection_status_changed``;
        # consumed by AccountListPanel to dim rows whose worker is offline
        # and by FlashTradeView during order fan-out to skip them.  Defaults
        # to "True" for any unknown label so freshly-detected accounts aren't
        # treated as offline before the first signal lands.
        self._connected_labels: dict[str, bool] = {}

    # ── Qt model interface ────────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        # Append a virtual "Total" row after the data rows when the table is
        # non-empty.  The totals row is computed on demand from _table_data;
        # nothing is stored for it.
        n = len(self._table_data)
        return n + 1 if n > 0 else 0

    def account_count(self) -> int:
        """Real account count, excluding the virtual totals row."""
        return len(self._table_data)

    def _is_total_row(self, row: int) -> bool:
        return row == len(self._table_data) and row > 0

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None

        row, col = index.row(), index.column()
        if self._is_total_row(row):
            return self._total_data(col, role)
        if row >= len(self._table_data):
            return None

        raw = self._table_data[row][col]

        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_DAILY_PNL:
                return self._format_pnl(raw)
            if col in _FINANCIAL_COLS:
                return self._format_financial(raw)
            return raw

        if role == Qt.ItemDataRole.EditRole:
            # When Qt opens the inline editor for the Alias column, it asks
            # for EditRole — return the current text so the user can modify
            # it instead of starting from an empty field.
            return raw

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in _FINANCIAL_COLS:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if col == COL_TYPE:
                return int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == COL_TYPE:
                if raw == "Paper":
                    return QColor("#F85149")   # red – simulated
                if raw == "Live":
                    return QColor("#3FB950")   # green – real
            if col in _PNL_COLS:
                try:
                    value = float(raw)
                    if value < 0:
                        return QColor("#F85149")   # red
                    if value > 0:
                        return QColor("#3FB950")   # green
                except (ValueError, TypeError):
                    pass  # keep default color

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if self._is_total_row(index.row()):
            # Totals row: visible but not selectable/editable.
            return Qt.ItemFlag.ItemIsEnabled
        base = super().flags(index)
        if index.column() == COL_ALIAS:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role == Qt.ItemDataRole.EditRole and index.column() == COL_ALIAS:
            alias = str(value).strip()
            row_data = self._table_data[index.row()]
            row_data[COL_ALIAS] = alias

            # Persist: account_number → alias
            account = row_data[COL_ACCOUNT]
            if account:
                if alias:
                    self._aliases[account] = alias
                else:
                    self._aliases.pop(account, None)
                config_manager.save_aliases(self._aliases)
                log.info("Alias saved: %s → %r", account, alias)

            self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole])
            return True
        return False

    # ── Batch update (the "funnel" entry-point) ──────────────────────────────

    def update_batch(self, batch: list[tuple[str, str, str, str, str]]) -> None:
        """
        Receive a batch of ``(label, account, tag, value, currency)`` tuples
        from any worker thread and surgically update the table.
        """
        totals_to_refresh: set[int] = set()
        for label, account, tag, value, currency in batch:
            # Buying power is cached but not shown as a column in Connections view
            if tag == "BuyingPower":
                try:
                    self._buying_power[account] = float(value)
                except (ValueError, TypeError):
                    pass
                continue

            # Per-account, per-currency cash from the reqAccountUpdatesMulti
            # stream (account="" → all accounts).  Each real currency (USD,
            # JPY, CAD, …) arrives as its own CashBalance row; "BASE" is the
            # base-currency roll-up we don't want in the per-currency line.
            if tag == "CashBalance":
                if currency and currency != "BASE":
                    try:
                        self._cash_by_ccy.setdefault(
                            account, {})[currency] = float(value)
                    except (ValueError, TypeError):
                        pass
                    else:
                        # Nudge the account's row so AccountListPanel re-reads
                        # the cash map (CashBalance isn't a table column).  Never
                        # insert from cash alone — NetLiquidation owns row
                        # creation, and any group-consolidated ledger row that
                        # slips through must not spawn a phantom account row.
                        row_idx = self._row_map.get((label, account))
                        if row_idx is not None:
                            mi = self.index(row_idx, COL_NET_LIQ)
                            self.dataChanged.emit(
                                mi, mi, [Qt.ItemDataRole.DisplayRole])
                continue

            col = _TAG_COL_MAP.get(tag)
            if col is None:
                continue

            key = (label, account)
            if key not in self._row_map:
                self._insert_row(label, account)

            row_idx = self._row_map[key]
            self._table_data[row_idx][col] = value

            mi = self.index(row_idx, col)
            self.dataChanged.emit(mi, mi, [Qt.ItemDataRole.DisplayRole])
            totals_to_refresh.add(col)

        if totals_to_refresh and self._table_data:
            self._emit_totals_changed(totals_to_refresh)

    # ── Row management ────────────────────────────────────────────────────────

    def _insert_row(self, label: str, account: str) -> None:
        row_idx = len(self._table_data)
        acct_type = detect_account_type(account)
        alias = self._aliases.get(account, "")
        log.info("New account row: label=%r account=%r type=%s alias=%r (row %d)",
                 label, account, acct_type, alias, row_idx)
        # When inserting the very first account, the virtual totals row is
        # also appearing — tell Qt two rows are materialising (data row 0,
        # totals row 1).  For subsequent inserts we add just one data row;
        # the totals row's effective index shifts down automatically because
        # rowCount() tracks len(_table_data).
        if row_idx == 0:
            self.beginInsertRows(QModelIndex(), 0, 1)
        else:
            self.beginInsertRows(QModelIndex(), row_idx, row_idx)
        self._table_data.append(
            [label, account, acct_type, alias, "", "", "", ""])   # 8 cols
        self._row_map[(label, account)] = row_idx
        self._acct_to_label[account] = label
        self.endInsertRows()

    def get_buying_power(self, account: str) -> float:
        """Return the last-known buying power for *account*, or 0.0."""
        return self._buying_power.get(account, 0.0)

    def _get_financial_col(self, account: str, col: int) -> float:
        """Float value of a financial column for *account*, 0.0 when the
        account is unknown or the tag hasn't arrived yet."""
        label = self._acct_to_label.get(account)
        if label is None:
            return 0.0
        row_idx = self._row_map.get((label, account))
        if row_idx is None:
            return 0.0
        try:
            return float(self._table_data[row_idx][col] or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def get_net_liquidation(self, account: str) -> float:
        """Last-known NetLiquidation for *account*, or 0.0.  The base for
        percent-of-account order sizing."""
        return self._get_financial_col(account, COL_NET_LIQ)

    def get_excess_liquidity(self, account: str) -> float:
        """Last-known ExcessLiquidity for *account*, or 0.0."""
        return self._get_financial_col(account, COL_EXCESS_LIQ)

    def get_cash_by_currency(self, account: str) -> dict[str, float]:
        """Return the last-known per-currency cash balances for *account*
        (e.g. ``{"USD": 6300.0, "JPY": 5000.0}``), or an empty dict."""
        return self._cash_by_ccy.get(account, {})

    def remove_rows_by_label(self, label: str) -> None:
        """Remove every row whose Connection column matches *label*."""
        keys_to_remove = [k for k in self._row_map if k[0] == label]
        if not keys_to_remove:
            return
        log.info("Removing %d account row(s) for label=%r",
                 len(keys_to_remove), label)
        # Remove from bottom to top to keep indices valid.
        indices = sorted((self._row_map[k]
                         for k in keys_to_remove), reverse=True)
        for idx in indices:
            # When removing the last remaining data row, the virtual totals
            # row disappears with it — tell Qt both rows are going away.
            if len(self._table_data) == 1:
                self.beginRemoveRows(QModelIndex(), 0, 1)
            else:
                self.beginRemoveRows(QModelIndex(), idx, idx)
            self._table_data.pop(idx)
            self.endRemoveRows()
        # Rebuild _row_map and _acct_to_label after removals
        self._row_map.clear()
        self._acct_to_label.clear()
        for i, row in enumerate(self._table_data):
            self._row_map[(row[COL_CONNECTION], row[COL_ACCOUNT])] = i
            self._acct_to_label[row[COL_ACCOUNT]] = row[COL_CONNECTION]
        # Drop the connection-state entry too — the label no longer has
        # any account rows attached to it, so its state is irrelevant.
        self._connected_labels.pop(label, None)
        # Surviving totals row needs to recompute after removal.
        if self._table_data:
            self._emit_totals_changed(
                {COL_NET_LIQ, COL_TOTAL_CASH, COL_EXCESS_LIQ, COL_DAILY_PNL})

    # ── Per-label TWS connection state ────────────────────────────────────────

    def set_connection_status(self, label: str, connected: bool) -> None:
        """Record the live TWS connection state for ``label``.

        Emits ``dataChanged`` on every row whose Connection column matches
        so views (the Connect-tab accounts table, AccountListPanel) repaint
        with the new state — AccountListPanel listens to ``dataChanged``
        already (see ``_on_model_data_changed``)."""
        prev = self._connected_labels.get(label)
        if prev == bool(connected):
            return
        self._connected_labels[label] = bool(connected)
        last_col = self.columnCount() - 1
        for (lbl, _acct), row_idx in self._row_map.items():
            if lbl == label:
                tl = self.index(row_idx, 0)
                br = self.index(row_idx, last_col)
                self.dataChanged.emit(tl, br, [Qt.ItemDataRole.DisplayRole])
        self.connection_state_changed.emit()

    def is_label_connected(self, label: str | None) -> bool:
        """True when ``label`` is currently reporting a live TWS
        connection.  Defaults to True for unknown labels so a brand-new
        connection isn't dimmed before the first signal arrives."""
        if not label:
            return True
        return self._connected_labels.get(label, True)

    # ── Totals row ────────────────────────────────────────────────────────────

    def _sum_financial_col(self, col: int) -> float:
        total = 0.0
        for row in self._table_data:
            raw = row[col]
            if not raw:
                continue
            try:
                v = float(raw)
            except (ValueError, TypeError):
                continue
            # Skip IBKR "Infinity" sentinels.
            if abs(v) > 1e100:
                continue
            total += v
        return total

    def _total_data(self, col: int, role: int) -> Any:
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_CONNECTION:
                return "Total"
            if col in _FINANCIAL_COLS:
                total = self._sum_financial_col(col)
                if col == COL_DAILY_PNL:
                    return self._format_pnl(str(total))
                return self._format_financial(str(total))
            return ""

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in _FINANCIAL_COLS:
                return int(Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.FontRole:
            f = QFont()
            f.setBold(True)
            return f

        if role == Qt.ItemDataRole.BackgroundRole:
            # Slightly darker to visually separate the footer from data rows.
            return QColor("#151922")

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == COL_DAILY_PNL:
                total = self._sum_financial_col(col)
                if total > 0:
                    return QColor("#3FB950")
                if total < 0:
                    return QColor("#F85149")

        return None

    def _emit_totals_changed(self, cols: set[int]) -> None:
        """Notify the view that the virtual totals row's cells recomputed."""
        total_row = len(self._table_data)
        if total_row == 0:
            return
        for c in cols:
            idx = self.index(total_row, c)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DisplayRole])

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _format_pnl(raw: str) -> str:
        """Format Daily P&L as a signed integer (no decimals)."""
        if not raw:
            return ""
        try:
            val = float(raw)
        except (ValueError, TypeError):
            return raw
        if abs(val) > 1e100:
            return "-"
        v = int(round(val))
        if v == 0:
            return ""
        return f"+{v:,}" if v > 0 else f"{v:,}"

    @staticmethod
    def _format_financial(raw: str) -> str:
        if not raw:
            return ""
        try:
            val = float(raw)
        except (ValueError, TypeError):
            return raw
        if abs(val) > 1e100:
            return "-"
        return f"{val:,.2f}"

    def daily_pnl_entries(self) -> list[tuple[str, float]]:
        """(display name, Daily P&L) for every account row that has
        received a ``DailyPnL`` value — the same numbers the Connect
        tab's Daily P&L column shows, in the same row order.  The name
        is the account's alias when one is set, else the account id.
        Rows whose tag hasn't arrived yet and IBKR "Infinity" sentinels
        are skipped."""
        out: list[tuple[str, float]] = []
        for row in self._table_data:
            raw = row[COL_DAILY_PNL]
            if not raw:
                continue
            try:
                v = float(raw)
            except (ValueError, TypeError):
                continue
            if abs(v) > 1e100:
                continue
            out.append((row[COL_ALIAS] or row[COL_ACCOUNT], v))
        return out

    def connection_for_account(self, account: str) -> str | None:
        """Return the connection_label that manages *account*, or None.

        Uses the O(1) reverse index ``_acct_to_label`` which is kept in
        sync by ``_insert_row`` and ``remove_rows_by_label``.
        """
        return self._acct_to_label.get(account)
