"""
AccountPositionModel – grouped QAbstractTableModel for live portfolio positions.

Row layout
──────────
For every account group the model inserts:
  • one HEADER row  → shows account name; columns 1-7 are spanned into one
                       merged info cell painted by AccountHeaderInfoDelegate
                       (Daily P&L · Net Liq · Excess Liq); column 8 = Flatten
  • N POSITION rows → symbol, position, prices, per-position P&L

Columns
───────
Account(0) Symbol(1) Position(2) AvgPrice(3) LastPrice(4)
DailyP&L(5) RealizedP&L(6) UnrealizedP&L(7) Flatten(8)
"""
from __future__ import annotations

import logging
import math
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
)
from PySide6.QtGui import QBrush, QColor, QFont

# ── Custom roles ──────────────────────────────────────────────────────────────
IsFirstInGroupRole      = Qt.ItemDataRole.UserRole + 100  # kept for compat (unused now)
RowTypeRole             = Qt.ItemDataRole.UserRole + 101  # str: ROW_TYPE_*
FlattenRole             = Qt.ItemDataRole.UserRole + 102  # str: account id (header rows)
HeaderInfoRole          = Qt.ItemDataRole.UserRole + 103  # dict: daily_pnl/net_liq/excess_liq
PositionKeyRole         = Qt.ItemDataRole.UserRole + 104  # tuple(account, con_id) for position rows
PositionRole            = Qt.ItemDataRole.UserRole + 105  # float: raw position qty (position rows)
AccountHasPositionsRole = Qt.ItemDataRole.UserRole + 106  # bool: account has any non-zero position (header rows)

# ── Row type constants ────────────────────────────────────────────────────────
ROW_TYPE_HEADER   = "header"
ROW_TYPE_POSITION = "position"

log = logging.getLogger(__name__)

# ── Column indices ────────────────────────────────────────────────────────────
COL_ACCOUNT        = 0
COL_SYMBOL         = 1
COL_POSITION       = 2
COL_UNREALIZED_PNL = 3
COL_AVG_PRICE      = 4
COL_LAST_PRICE     = 5
COL_REALIZED_PNL   = 6
COL_MARKET_VALUE   = 7
COL_FLATTEN        = 8   # header rows: Flatten button; position rows: blank

HEADERS = [
    "Account", "Symbol", "Qty", "Unrealized", "Avg Price", "Last Price",
    "Realized", "Mkt Value", "",
]

NUM_COLS = len(HEADERS)

# ── Theme tokens ──────────────────────────────────────────────────────────────
TEXT     = "#C9D1D9"
TEXT_DIM = "#8B949E"
CLR_PNL_POS = "#4CAF50"
CLR_PNL_NEG = "#FF5252"
CLR_LONG    = "#4CAF50"
CLR_SHORT   = "#FF5252"
CLR_ACCT    = "#58A6FF"
BG_HEADER_ROW = "#1D2535"

_MONO       = QFont("Menlo, Consolas, monospace", 11)
_MONO_BOLD  = QFont("Menlo, Consolas, monospace", 11, QFont.Weight.Bold)
_ACCT_FONT  = QFont("Menlo, Consolas, monospace", 11, QFont.Weight.Bold)

_BG_HEADER_BRUSH = QBrush(QColor(BG_HEADER_ROW))


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_price(val: float) -> str:
    if val == 0 or not math.isfinite(val):
        return "–"
    return f"{val:,.2f}"


def _fmt_money(val: float) -> str:
    if val == 0 or not math.isfinite(val):
        return "–"
    return f"${val:,.0f}"


def _fmt_pnl(val: float) -> str:
    if not math.isfinite(val):
        return "–"
    v = int(round(val))
    if v == 0:
        return ""
    return f"+${v:,}" if v > 0 else f"-${abs(v):,}"


def _fmt_qty(val: float) -> str:
    v = int(val)
    return f"+{v}" if v > 0 else str(v)


def _fmt_mktval(val: float) -> str:
    """Format market value: rounded to integer, empty for zero, signed."""
    if not math.isfinite(val):
        return "–"
    v = int(round(val))
    if v == 0:
        return ""
    return f"${v:,}" if v > 0 else f"-${abs(v):,}"


def _pnl_color(val: float) -> QColor:
    if not math.isfinite(val):
        return QColor(TEXT_DIM)
    return QColor(CLR_PNL_POS) if val >= 0 else QColor(CLR_PNL_NEG)


# ══════════════════════════════════════════════════════════════════════════════
#  AccountPositionModel
# ══════════════════════════════════════════════════════════════════════════════

class AccountPositionModel(QAbstractTableModel):
    """Grouped table – account header row followed by its position rows."""

    def __init__(self, get_alias=None, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self._row_map: dict[tuple[str, int], int] = {}  # (acct, conId) → row idx
        self._acct_row_map: dict[str, int] = {}          # account → header row idx
        self._positions: dict[tuple[str, int], dict] = {}
        self._pnl_single: dict[tuple[str, int], dict] = {}
        self._acct_data: dict[str, dict] = {}
        self._get_alias = get_alias
        self._show_zero: bool = True   # default ON – show zero-qty rows

    def set_show_zero(self, show: bool) -> None:
        """Show or hide rows where position qty == 0, then rebuild."""
        if self._show_zero != show:
            self._show_zero = show
            self.rebuild()

    # ── Qt table interface ────────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return NUM_COLS

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if (role == Qt.ItemDataRole.DisplayRole
                and orientation == Qt.Orientation.Horizontal):
            return HEADERS[section] if section < NUM_COLS else None
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ── data() ────────────────────────────────────────────────────────────────

    def data(self, index: QModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if row >= len(self._rows):
            return None

        rd       = self._rows[row]
        row_type = rd.get("_row_type", ROW_TYPE_POSITION)
        account  = rd.get("account", "")

        # ── Row-type role (always available) ──────────────────────────────
        if role == RowTypeRole:
            return row_type

        # ══════════════════════════════════════════════════════════════════
        #  ACCOUNT HEADER ROW
        # ══════════════════════════════════════════════════════════════════
        if row_type == ROW_TYPE_HEADER:
            ad = self._acct_data.get(account, {})

            # ── HeaderInfoRole – available on any column of a header row ──
            # The panel uses setSpan to merge cols 1-7 into one cell, and
            # AccountHeaderInfoDelegate reads this role to paint the summary.
            if role == HeaderInfoRole:
                return {
                    "daily_pnl":  ad.get("daily_pnl",  float("nan")),
                    "net_liq":    ad.get("net_liq",    0.0),
                    "excess_liq": ad.get("excess_liq", 0.0),
                }

            if role == Qt.ItemDataRole.DisplayRole:
                if col == COL_ACCOUNT:
                    alias = (self._get_alias(account) if self._get_alias else "")
                    return alias if alias else account
                return None  # all other cells blank (delegate paints the info)

            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

            if role == Qt.ItemDataRole.BackgroundRole:
                return _BG_HEADER_BRUSH

            if role == Qt.ItemDataRole.ForegroundRole:
                if col == COL_ACCOUNT:
                    return QColor(CLR_ACCT)
                return QColor(TEXT_DIM)

            if role == Qt.ItemDataRole.FontRole:
                if col == COL_ACCOUNT:
                    return _ACCT_FONT
                return _MONO

            if role == FlattenRole:
                return account

            if role == AccountHasPositionsRole:
                return any(
                    pos.get("position", 0) != 0
                    for key, pos in self._positions.items()
                    if key[0] == account
                )

            if role == IsFirstInGroupRole:
                return False

            return None

        # ══════════════════════════════════════════════════════════════════
        #  POSITION ROW
        # ══════════════════════════════════════════════════════════════════
        con_id = rd.get("con_id", 0)
        pnl_s  = self._pnl_single.get((account, con_id), {})

        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_ACCOUNT:
                # Show daily P&L in the first column for position rows
                v = pnl_s.get("daily_pnl", rd.get("daily_pnl", float("nan")))
                return _fmt_pnl(v) if math.isfinite(v) else "–"
            if col == COL_SYMBOL:
                return rd.get("local_symbol") or rd.get("symbol", "")
            if col == COL_POSITION:
                return _fmt_qty(rd.get("position", 0))
            if col == COL_AVG_PRICE:
                v = rd.get("display_avg", rd.get("avg_cost", 0))
                return f"{v:,.2f}" if (v and math.isfinite(v)) else ""
            if col == COL_LAST_PRICE:
                return _fmt_price(rd.get("market_price", 0))
            if col == COL_REALIZED_PNL:
                v = pnl_s.get("realized_pnl", rd.get("realized_pnl", float("nan")))
                return _fmt_pnl(v) if math.isfinite(v) else "–"
            if col == COL_UNREALIZED_PNL:
                v = pnl_s.get("unrealized_pnl", rd.get("unrealized_pnl", float("nan")))
                return _fmt_pnl(v) if math.isfinite(v) else "–"
            if col == COL_MARKET_VALUE:
                return _fmt_mktval(rd.get("market_value", float("nan")))
            return None  # COL_NET_LIQ, COL_EXCESS_LIQ, COL_FLATTEN → blank

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == COL_SYMBOL:
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == COL_ACCOUNT:
                v = pnl_s.get("daily_pnl", rd.get("daily_pnl", float("nan")))
                return _pnl_color(v)
            if col == COL_REALIZED_PNL:
                v = pnl_s.get("realized_pnl", rd.get("realized_pnl", float("nan")))
                return _pnl_color(v)
            if col == COL_UNREALIZED_PNL:
                v = pnl_s.get("unrealized_pnl", rd.get("unrealized_pnl", float("nan")))
                return _pnl_color(v)
            if col == COL_POSITION:
                pos_val = rd.get("position", 0)
                if pos_val > 0:
                    return QColor(CLR_LONG)
                elif pos_val < 0:
                    return QColor(CLR_SHORT)
                else:
                    return QColor(TEXT_DIM)  # zero position – neutral
            return QColor(TEXT)

        if role == Qt.ItemDataRole.FontRole:
            if col in (COL_ACCOUNT, COL_SYMBOL):
                return _MONO_BOLD
            if col in (COL_REALIZED_PNL, COL_UNREALIZED_PNL):
                return _MONO_BOLD
            return _MONO

        if role == IsFirstInGroupRole:
            return False  # position rows never need a separator (header row does it)

        if role == PositionKeyRole:
            return (account, con_id)   # delegate reads this to identify the position

        if role == PositionRole:
            return rd.get("position", 0)   # raw float qty – 0 means flat

        return None

    # ══════════════════════════════════════════════════════════════════════════
    #  Public API
    # ══════════════════════════════════════════════════════════════════════════

    def update_positions(self, batch: list[dict]) -> None:
        needs_rebuild = False
        for pos in batch:
            account = pos.get("account", "")
            con_id  = pos.get("con_id", 0)
            if not account or not con_id:
                continue
            key = (account, con_id)
            old = self._positions.get(key)
            pos = dict(pos)
            self._compute_display_avg(pos)
            self._positions[key] = pos

            if key in self._row_map and old is not None:
                # Re-sort whenever market_value changes — it drives the sort order
                if pos.get("market_value", 0) != old.get("market_value", 0):
                    needs_rebuild = True
                else:
                    self._update_row_in_place(key, pos)
            else:
                needs_rebuild = True  # new key – always add to view

        if needs_rebuild:
            self.rebuild()

    def update_pnl_single(self, account: str, con_id: int,
                          daily_pnl: float, realized_pnl: float,
                          unrealized_pnl: float) -> None:
        key = (account, con_id)
        pnl_data = {
            "daily_pnl":      daily_pnl,
            "realized_pnl":   realized_pnl,
            "unrealized_pnl": unrealized_pnl,
        }
        self._pnl_single[key] = pnl_data
        row = self._row_map.get(key)
        if row is not None:
            # Write finite values back into the row dict so the data persists
            # even if the broker stops sending pnlSingle for zero-position rows.
            rd = self._rows[row]
            for field, val in pnl_data.items():
                if math.isfinite(val):
                    rd[field] = val
            left  = self.index(row, COL_ACCOUNT)
            right = self.index(row, COL_UNREALIZED_PNL)
            self.dataChanged.emit(left, right, [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.ForegroundRole,
            ])

    def set_account_data(self, account: str, *,
                         net_liq: float = 0, excess_liq: float = 0,
                         daily_pnl: float = float("nan"),
                         realized_pnl: float = float("nan"),
                         unrealized_pnl: float = float("nan")) -> None:
        ad = self._acct_data.setdefault(account, {})
        if net_liq:
            ad["net_liq"] = net_liq
        if excess_liq:
            ad["excess_liq"] = excess_liq
        if math.isfinite(daily_pnl):
            ad["daily_pnl"] = daily_pnl
        if math.isfinite(realized_pnl):
            ad["realized_pnl"] = realized_pnl
        if math.isfinite(unrealized_pnl):
            ad["unrealized_pnl"] = unrealized_pnl

        # Refresh the account header row live if it already exists
        hrow = self._acct_row_map.get(account)
        if hrow is not None:
            left  = self.index(hrow, 0)
            right = self.index(hrow, NUM_COLS - 1)
            self.dataChanged.emit(left, right, [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.ForegroundRole,
            ])

    def clear(self) -> None:
        if self._rows:
            self.beginResetModel()
            self._rows.clear()
            self._row_map.clear()
            self._acct_row_map.clear()
            self.endResetModel()
        self._positions.clear()
        # _acct_data and _pnl_single are intentionally NOT cleared here so
        # cached account-level P&L (Daily P&L, Net Liq, Excess Liq) and
        # per-position P&L survive a Refresh and are immediately visible
        # when rows are rebuilt, before the next pnlEvent/pnlSingleEvent fires.

    def get_position_keys(self) -> list[tuple[str, int]]:
        return [k for k, v in self._positions.items()
                if v.get("position", 0) != 0]

    # ── Rebuild ───────────────────────────────────────────────────────────────

    def rebuild(self) -> None:
        self.beginResetModel()
        self._rows.clear()
        self._row_map.clear()
        self._acct_row_map.clear()

        # Collect all positions grouped by account (including zero positions)
        acct_pos: dict[str, list] = {}
        for key, pos in self._positions.items():
            if not self._show_zero and pos.get("position", 0) == 0:
                continue
            acct_pos.setdefault(key[0], []).append((key, pos))

        for account in sorted(acct_pos):
            # Sort positions within the account by abs(market_value) desc
            acct_pos[account].sort(
                key=lambda kv: abs(kv[1].get("market_value", 0)),
                reverse=True,
            )

            # Account header row
            self._acct_row_map[account] = len(self._rows)
            self._rows.append({"_row_type": ROW_TYPE_HEADER, "account": account})

            # Position rows
            for key, pos in acct_pos[account]:
                self._row_map[key] = len(self._rows)
                p = dict(pos)
                p["_row_type"] = ROW_TYPE_POSITION
                # Merge cached pnl_single data into the row so Daily P&L is
                # visible immediately, even for zero-position rows where the
                # broker has stopped sending pnlSingle updates.
                pnl_s = self._pnl_single.get(key, {})
                for field in ("daily_pnl", "realized_pnl", "unrealized_pnl"):
                    if field in pnl_s and math.isfinite(pnl_s[field]):
                        p.setdefault(field, pnl_s[field])
                self._rows.append(p)

        self.endResetModel()
        log.debug(
            "AccountPositionModel: rebuilt – %d accounts, %d positions",
            len(acct_pos), len(self._row_map),
        )

    # ── In-place position update ───────────────────────────────────────────────

    def _update_row_in_place(self, key: tuple[str, int], pos: dict) -> None:
        row = self._row_map.get(key)
        if row is None or row >= len(self._rows):
            return
        rd = self._rows[row]
        for k, v in pos.items():
            rd[k] = v
        # When position hits zero, snapshot the last known pnl_single data
        # into rd — the broker may stop sending updates for flat positions.
        if pos.get("position", 0) == 0:
            pnl_s = self._pnl_single.get(key, {})
            for field in ("daily_pnl", "realized_pnl", "unrealized_pnl"):
                if field in pnl_s and math.isfinite(pnl_s[field]):
                    rd.setdefault(field, pnl_s[field])
        left  = self.index(row, 0)
        right = self.index(row, NUM_COLS - 1)
        self.dataChanged.emit(left, right, [
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.ForegroundRole,
        ])

    @staticmethod
    def _compute_display_avg(pos: dict) -> None:
        sec_type     = pos.get("sec_type", "")
        position     = pos.get("position", 0)
        market_price = pos.get("market_price", 0)
        avg_cost     = pos.get("avg_cost", 0)
        display_avg  = avg_cost
        if sec_type == "FUT" and position != 0 and market_price != 0:
            market_value  = pos.get("market_value", 0)
            implied_mult  = abs(market_value / (position * market_price))
            if implied_mult > 1:
                display_avg = avg_cost / implied_mult
        pos["display_avg"] = display_avg

