"""
AccountPositionModel – grouped QAbstractTableModel for live portfolio positions.

Row layout
──────────
For every account group the model inserts:
  • one HEADER row  → the whole row is spanned into one merged band painted
                       by AccountHeaderInfoDelegate (account name · Daily P&L ·
                       Net · EX · BP).  No buttons.
  • N POSITION rows → symbol, day P&L, position, prices, per-position P&L,
                       and the 25|50|75|100% close control in the last column

Columns
───────
Symbol(0) DayP&L(1) Qty(2) Unrealized(3) AvgPrice(4)
LastPrice(5) Realized(6) MktValue(7) Close(8)
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
from PySide6.QtGui import QBrush, QColor

from src.ui.theme import Theme
from src.utils.helpers import format_option_symbol_two_line

from .table_theme import CELL_FONT, CELL_FONT_BOLD

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
# COL_SYMBOL holds the account name on header rows (it is the span origin)
# and the contract symbol on position rows.
COL_SYMBOL         = 0
COL_DAY_PNL        = 1
COL_POSITION       = 2
COL_UNREALIZED_PNL = 3
COL_AVG_PRICE      = 4
COL_LAST_PRICE     = 5
COL_REALIZED_PNL   = 6
COL_MARKET_VALUE   = 7
# Historical name — this column now carries the 25|50|75|100% close control.
COL_FLATTEN        = 8

# "Close %" rather than "Close": only the last segment is labelled 100%,
# so the caption is what tells you 25/50/75 are percentages of the
# position and not share counts.
HEADERS = [
    "Symbol", "Day P&L", "Qty", "Unrealized", "Avg Price", "Last Price",
    "Realized", "Mkt Value", "Close %",
]

NUM_COLS = len(HEADERS)

# ── Theme tokens ──────────────────────────────────────────────────────────────
TEXT     = Theme.POS_TEXT
TEXT_DIM = Theme.POS_TEXT_MUTED
TEXT_MUTED  = Theme.POS_LABEL      # en-dash placeholders / zero P&L
CLR_PNL_POS = Theme.POS_GAIN
CLR_PNL_NEG = Theme.POS_LOSS
CLR_LONG    = Theme.POS_GAIN
CLR_SHORT   = Theme.POS_LOSS
CLR_ACCT    = Theme.POS_ACCT_NAME
BG_HEADER_ROW = Theme.POS_BG_ACCT_BAND

# Shared with the Trades blotter so the two tabs render cells identically.
# They carry family and weight only — the size comes from the table's
# stylesheet (table_theme.CELL_FONT_PX).
_MONO       = CELL_FONT
_MONO_BOLD  = CELL_FONT_BOLD
_ACCT_FONT  = CELL_FONT_BOLD

_BG_HEADER_BRUSH = QBrush(QColor(BG_HEADER_ROW))


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_price(val: float) -> str:
    if val == 0 or not math.isfinite(val):
        return "–"
    return f"{val:,.2f}"


def _fmt_money(val: float) -> str:
    if val == 0 or not math.isfinite(val):
        return "–"
    return f"{val:,.0f}"


def _fmt_pnl(val: float) -> str:
    if not math.isfinite(val):
        return "–"
    v = int(round(val))
    if v == 0:
        return "–"
    return f"+{v:,}" if v > 0 else f"-{abs(v):,}"


def _fmt_qty(val: float) -> str:
    v = int(val)
    return f"+{v}" if v > 0 else str(v)


def _fmt_mktval(val: float) -> str:
    """Format market value: rounded to integer, en-dash for zero, signed."""
    if not math.isfinite(val):
        return "–"
    v = int(round(val))
    if v == 0:
        return "–"
    return f"{v:,}" if v > 0 else f"-{abs(v):,}"


def _pnl_color(val: float) -> QColor:
    """Green/red for a live P&L; muted grey for zero or missing.

    Zero has to be muted, not green — the cell renders as an en-dash and a
    green dash reads as a gain that isn't there.
    """
    if not math.isfinite(val) or int(round(val)) == 0:
        return QColor(TEXT_MUTED)
    return QColor(CLR_PNL_POS) if val > 0 else QColor(CLR_PNL_NEG)


# ══════════════════════════════════════════════════════════════════════════════
#  AccountPositionModel
# ══════════════════════════════════════════════════════════════════════════════

class AccountPositionModel(QAbstractTableModel):
    """Grouped table – account header row followed by its position rows."""

    # Keep the account ordering identical to the Account Management panel
    # (``AccountListPanel._name_sort_key``): ``mgn`` prefix ranks first
    # (top), ``tfs`` rank 1, ``rsp`` and ``fhs`` share rank 2, and every
    # other prefix falls through to rank 3 where the alphabetical tiebreak
    # takes over.
    _PREFIX_PRIORITY: dict[str, int] = {
        "mgn": 0,
        "tfs": 1,
        "rsp": 2,
        "fhs": 2,
    }

    def _name_sort_key(self, account: str) -> tuple:
        """Sort key matching ``AccountListPanel._name_sort_key``:
        priority-prefix buckets first (MGN > TFS > RSP/FHS > everything
        else), then alphabetical by first 3 chars of the display name,
        then last 2 chars.  Display name = alias when set, else account
        id.  Case-insensitive, with the account id as final tiebreaker."""
        alias = (self._get_alias(account) if self._get_alias else "") or ""
        name = alias if alias else account
        lower = name.lower()
        first3 = lower[:3]
        last2 = lower[-2:]
        priority = self._PREFIX_PRIORITY.get(first3, 3)
        return (priority, first3, last2, account)

    def __init__(self, get_alias=None, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        # Connection labels currently disconnected.  Their positions stay
        # in ``_positions`` but are filtered out of the rendered rows, so
        # a reconnect restores them instantly — see ``offline_connections``.
        self._offline_labels: set[str] = set()
        # Accounts served by those connections, so their header bands are
        # suppressed too (a header renders even with no positions under it).
        self._offline_accounts: set[str] = set()
        self._row_map: dict[tuple[str, int], int] = {}
        self._acct_row_map: dict[str, int] = {}
        self._positions: dict[tuple[str, int], dict] = {}
        self._pnl_single: dict[tuple[str, int], dict] = {}
        self._acct_data: dict[str, dict] = {}
        self._get_alias = get_alias
        self._show_zero: bool = True
        # None  = no filter, show all accounts (initial state before the
        #         Account List panel has pushed a selection).
        # set() = user has explicitly deselected everything → show nothing.
        self._selected_accounts: set[str] | None = None
        # Lowercased search text from the control-bar search field.
        # Matches account id / alias / symbol / localSymbol.
        self._search_text: str = ""

    def set_search_text(self, text: str) -> None:
        """Filter rows by account id/alias or symbol (case-insensitive
        substring), then rebuild.  Empty text clears the filter."""
        text = (text or "").strip().lower()
        if self._search_text != text:
            self._search_text = text
            self.rebuild()

    def _account_matches_search(self, account: str) -> bool:
        if self._search_text in account.lower():
            return True
        alias = (self._get_alias(account) if self._get_alias else "") or ""
        return self._search_text in alias.lower()

    def _position_matches_search(self, account: str, pos: dict) -> bool:
        if not self._search_text:
            return True
        if self._account_matches_search(account):
            return True
        return any(
            self._search_text in (pos.get(f) or "").lower()
            for f in ("symbol", "local_symbol"))

    def set_show_zero(self, show: bool) -> None:
        """Show or hide rows where position qty == 0, then rebuild."""
        if self._show_zero != show:
            self._show_zero = show
            self.rebuild()

    def set_selected_accounts(self, accounts: set[str]) -> None:
        """Filter the view to only the given accounts.

        Pass an empty set to show nothing (all accounts deselected).
        Pass None to clear the filter and show all accounts.
        """
        new = set(accounts) if accounts is not None else None
        if self._selected_accounts != new:
            self._selected_accounts = new
            self.rebuild()

    def set_offline(self, labels, accounts=()) -> None:
        """Hide everything served by these (disconnected) connections.

        *labels* filters the position rows, which each carry the
        connection that reported them.  *accounts* additionally
        suppresses the per-account HEADER bands: a header is rendered
        for any in-scope account with account-summary data even when it
        holds nothing, so without this an offline account would still
        show its (frozen) Net / Excess / BP line with no rows beneath.

        A full rebuild, because rows genuinely appear and disappear.  The
        ``_positions`` cache is untouched, so reconnecting brings
        everything straight back.
        """
        new_labels = {str(x) for x in (labels or ()) if x}
        new_accounts = {str(x) for x in (accounts or ()) if x}
        if (new_labels == self._offline_labels
                and new_accounts == self._offline_accounts):
            return
        self._offline_labels = new_labels
        self._offline_accounts = new_accounts
        self.rebuild()

    def offline_labels(self) -> set[str]:
        return set(self._offline_labels)

    def hidden_accounts(self) -> set[str]:
        """Accounts currently hidden because their connection is down.

        The caller's explicit list UNION every account whose positions
        came from an offline connection — so the model stays correct even
        when the caller could not enumerate the accounts (e.g. the
        worker's ``accounts_detected`` fired before anything was
        listening).  The panel reports the count in its banner: a shorter
        list of positions must never pass silently for a smaller book."""
        out = set(self._offline_accounts)
        out |= {
            acct for (acct, _cid), pos in self._positions.items()
            if (pos.get("label", "") or "") in self._offline_labels
        }
        return out

    # ── Qt table interface ────────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return NUM_COLS

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation != Qt.Orientation.Horizontal or section >= NUM_COLS:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            # Captions sit over their values: Symbol and the close control
            # read left, numbers right.
            align = (Qt.AlignmentFlag.AlignLeft
                     if section in (COL_SYMBOL, COL_FLATTEN)
                     else Qt.AlignmentFlag.AlignRight)
            return int(align | Qt.AlignmentFlag.AlignVCenter)
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
            # The panel spans the whole row into one cell at COL_SYMBOL, and
            # AccountHeaderInfoDelegate reads this role to paint the band.
            if role == HeaderInfoRole:
                return {
                    "daily_pnl":    ad.get("daily_pnl",    float("nan")),
                    "net_liq":      ad.get("net_liq",      0.0),
                    "excess_liq":   ad.get("excess_liq",   0.0),
                    "buying_power": ad.get("buying_power", 0.0),
                }

            if role == Qt.ItemDataRole.DisplayRole:
                if col == COL_SYMBOL:
                    alias = (self._get_alias(account) if self._get_alias else "")
                    return alias if alias else account
                return None  # all other cells blank (delegate paints the info)

            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

            if role == Qt.ItemDataRole.BackgroundRole:
                return _BG_HEADER_BRUSH

            if role == Qt.ItemDataRole.ForegroundRole:
                if col == COL_SYMBOL:
                    return QColor(CLR_ACCT)
                return QColor(TEXT_DIM)

            if role == Qt.ItemDataRole.FontRole:
                if col == COL_SYMBOL:
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
            if col == COL_SYMBOL:
                # Options stack root+expiry over strike+right — the symbol
                # column delegate paints newline'd cells as two small-font
                # lines (see table_theme.paint_two_line_cell).
                return format_option_symbol_two_line(
                    rd.get("local_symbol") or rd.get("symbol", ""))
            if col == COL_DAY_PNL:
                v = pnl_s.get("daily_pnl", rd.get("daily_pnl", float("nan")))
                return _fmt_pnl(v) if math.isfinite(v) else "–"
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
            return None  # COL_FLATTEN (close control) → blank

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == COL_SYMBOL:
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col == COL_DAY_PNL:
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
            if col in (COL_SYMBOL, COL_DAY_PNL):
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
            new_qty = pos.get("position", 0)
            old_qty = old.get("position", 0) if old is not None else None
            self._positions[key] = pos

            in_view        = key in self._row_map
            should_be_visible = (self._show_zero or new_qty != 0) \
                and self._position_matches_search(account, pos)

            if in_view:
                if new_qty != old_qty:
                    # Qty changed → row may need to appear/disappear or reorder
                    needs_rebuild = True
                else:
                    # Same qty — fast path: refresh only the affected cells
                    self._update_row_in_place(key, pos)
            elif should_be_visible:
                # Row ought to be in the view but isn't (new position or
                # show_zero just turned on) → rebuild to add it
                needs_rebuild = True
            # else: qty==0 and show_zero=False → row is intentionally hidden;
            # no rebuild needed, just keep _positions updated.

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
            # Span the whole row: realized P&L sits past the P&L block and
            # the panel watches dataChanged to auto-hide that column.
            left  = self.index(row, COL_SYMBOL)
            right = self.index(row, NUM_COLS - 1)
            self.dataChanged.emit(left, right, [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.ForegroundRole,
            ])

    def set_account_data(self, account: str, *,
                         net_liq: float = 0, excess_liq: float = 0,
                         buying_power: float = 0,
                         daily_pnl: float = float("nan"),
                         realized_pnl: float = float("nan"),
                         unrealized_pnl: float = float("nan")) -> None:
        ad = self._acct_data.setdefault(account, {})
        if net_liq:
            ad["net_liq"] = net_liq
        if excess_liq:
            ad["excess_liq"] = excess_liq
        if buying_power:
            ad["buying_power"] = buying_power
        if math.isfinite(daily_pnl):
            ad["daily_pnl"] = daily_pnl
        if math.isfinite(realized_pnl):
            ad["realized_pnl"] = realized_pnl
        if math.isfinite(unrealized_pnl):
            ad["unrealized_pnl"] = unrealized_pnl

        # Refresh the account header row live if it already exists …
        hrow = self._acct_row_map.get(account)
        if hrow is not None:
            left  = self.index(hrow, 0)
            right = self.index(hrow, NUM_COLS - 1)
            self.dataChanged.emit(left, right, [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.ForegroundRole,
            ])
        elif self._account_in_scope(account):
            # … otherwise this is an in-scope account we now have summary data
            # for but no header row yet (e.g. a selected account with no
            # positions) → rebuild so its account-summary row appears.
            self.rebuild()

    def _account_in_scope(self, account: str) -> bool:
        """Whether *account* would be shown given the current selection filter
        (None = show all, set() = show nothing, {...} = only those listed)."""
        if self._selected_accounts is None:
            return True
        return account in self._selected_accounts

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

    def has_realized(self) -> bool:
        """True when any *visible* row carries a non-zero realized P&L.

        The panel hides the Realized column while this is False — it is
        empty on most days and a column of en-dashes is pure noise.
        Resolves the value exactly the way ``data()`` does for
        COL_REALIZED_PNL: live pnlSingle first, row snapshot second.
        """
        for rd in self._rows:
            if rd.get("_row_type") != ROW_TYPE_POSITION:
                continue
            key = (rd.get("account", ""), rd.get("con_id", 0))
            v = self._pnl_single.get(key, {}).get(
                "realized_pnl", rd.get("realized_pnl", float("nan")))
            if math.isfinite(v) and int(round(v)) != 0:
                return True
        return False

    def get_symbol_for_position(self, account: str, con_id: int) -> str:
        """Return the best display symbol (localSymbol preferred) for a position."""
        pos = self._positions.get((account, con_id), {})
        return pos.get("local_symbol") or pos.get("symbol", "")

    def position_data(self, account: str, con_id: int) -> dict | None:
        """Full position dict for (account, con_id), or None if unknown."""
        pos = self._positions.get((account, int(con_id)))
        return dict(pos) if pos is not None else None

    def stk_positions_for_account(self, account: str) -> list[dict]:
        """All non-flat STK positions held by *account*.

        Used by the Options Income ticket to build its position chips."""
        out = []
        for (acct, _con_id), pos in self._positions.items():
            if acct != account:
                continue
            if (pos.get("sec_type") or "STK") != "STK":
                continue
            if not (pos.get("position") or 0):
                continue
            out.append(dict(pos))
        return out

    def position_for_symbol(self, account: str, symbol: str) -> dict | None:
        """Full position dict for the *account*'s STK holding in *symbol*
        (matched on root or local symbol, case-insensitive), or None.

        Used by the Options Income ticket for share counts, cost basis
        and the underlying conId."""
        want = (symbol or "").upper()
        for (acct, _con_id), pos in self._positions.items():
            if acct != account:
                continue
            if (pos.get("sec_type") or "STK") != "STK":
                continue
            sym = (pos.get("symbol") or pos.get("local_symbol") or "").upper()
            if sym == want:
                return dict(pos)
        return None

    def position_qty_for_symbol(self, account: str, symbol: str) -> float:
        """Total position quantity *account* holds in *symbol* (matched on
        localSymbol or root symbol, case-insensitive).  0.0 if none/unknown."""
        want = (symbol or "").upper()
        total = 0.0
        for (acct, _con_id), pos in self._positions.items():
            if acct != account:
                continue
            sym = (pos.get("local_symbol") or pos.get("symbol") or "").upper()
            if sym == want:
                total += pos.get("position", 0) or 0
        return total

    # ── Rebuild ───────────────────────────────────────────────────────────────

    def rebuild(self) -> None:
        self.beginResetModel()
        self._rows.clear()
        self._row_map.clear()
        self._acct_row_map.clear()

        # Collect all positions grouped by account (including zero positions)
        acct_pos: dict[str, list] = {}
        for key, pos in self._positions.items():
            account = key[0]
            # A disconnected connection's accounts are hidden entirely —
            # nothing is refreshing them, so every figure they carry is
            # frozen (see ``offline_connections``).
            if (pos.get("label", "") or "") in self._offline_labels:
                continue
            # _selected_accounts is None  → no filter, show all
            # _selected_accounts is set() → user deselected all, show nothing
            # _selected_accounts is {...} → show only listed accounts
            if self._selected_accounts is not None \
                    and account not in self._selected_accounts:
                continue
            if not self._show_zero and pos.get("position", 0) == 0:
                continue
            if not self._position_matches_search(account, pos):
                continue
            acct_pos.setdefault(account, []).append((key, pos))

        # Accounts to render a summary header for: those with qualifying
        # positions, plus every in-scope account we have account-summary data
        # for — so a selected account with no positions still shows its Net /
        # Excess / BP / Daily P&L header row (with no position rows beneath).
        if self._selected_accounts is None:
            scope = set(self._acct_data.keys())          # no filter → all known
        else:
            scope = set(self._selected_accounts)         # explicit selection
        header_accounts = set(acct_pos.keys())
        # No band for a hidden account.  Uses the DERIVED set, not the
        # caller's list alone: an account band renders from _acct_data,
        # which carries no connection label, so an incomplete list from
        # the caller would leave a header showing frozen Net / Excess /
        # BP with no rows under it.
        scope -= self.hidden_accounts()
        for acct in scope:
            if acct in self._acct_data:
                # While searching, a position-less account only keeps its
                # header row when its own id/alias matches the query.
                if self._search_text and not self._account_matches_search(acct):
                    continue
                header_accounts.add(acct)

        for account in sorted(header_accounts, key=self._name_sort_key):
            # Sort positions within the account by abs(market_value) desc,
            # with abs(position qty) as tiebreaker for rows where market
            # value hasn't arrived yet from pnlSingleEvent.
            positions = acct_pos.get(account, [])
            positions.sort(
                key=lambda kv: (abs(kv[1].get("market_value", 0)),
                                abs(kv[1].get("position", 0))),
                reverse=True,
            )

            # Account header row
            self._acct_row_map[account] = len(self._rows)
            self._rows.append({"_row_type": ROW_TYPE_HEADER, "account": account})

            # Position rows
            for key, pos in positions:
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
            "AccountPositionModel: rebuilt – %d account header(s), %d positions",
            len(header_accounts), len(self._row_map),
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

