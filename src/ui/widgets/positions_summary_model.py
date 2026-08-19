"""
PositionsSummaryModel – one row per contract (conId), aggregated across
every account holding the contract.

Mirrors the TradesSummaryModel pattern (aggregates in `_agg`, TOTAL
footer row, hide/restore API, `row_data` for proxy/menu helpers) but
with positions-stream semantics:

  * Aggregation key is `con_id`, not the bare ticker — a symbol that
    resolves to multiple contracts (multi-expiry futures, STK + OPT of
    the same ticker) produces one row per contract.
  * Display label is `local_symbol or symbol`.
  * Data arrives as full-portfolio batches via `update_positions`;
    every call folds from scratch so an account going flat correctly
    drops its contribution to weighted avg cost.
  * Futures correction: IB occasionally reports `avg_cost` in
    dollars-per-contract when it should be per-point; the flat model's
    `_compute_display_avg` fixes this before aggregation.

Columns
──────
Symbol(0) Type(1) Pos(2) AvgCost(3) MktPrice(4) UnrPnL(5) RlzPnL(6)
MktValue(7) Accts(8) Exch(9)
"""
from __future__ import annotations

import logging
import math
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from src.ui.theme import Theme
from src.utils.helpers import format_option_symbol_two_line

from .table_theme import CELL_FONT_BOLD, IsFooterRowRole

from .account_position_model import (
    AccountPositionModel,
    TEXT, TEXT_DIM, CLR_PNL_POS, CLR_PNL_NEG,
    _fmt_pnl, _fmt_money, _fmt_mktval, _pnl_color,
)

log = logging.getLogger(__name__)

# ── Custom roles ──────────────────────────────────────────────────────────────
# bool: TOTAL footer row.  Shared with the Trades summary so one delegate
# handles both tables' footer bands.
IsTotalRowRole = IsFooterRowRole

# ── Column indices ────────────────────────────────────────────────────────────
S_COL_SYMBOL         = 0
S_COL_SEC_TYPE       = 1
S_COL_POSITION       = 2
S_COL_AVG_COST       = 3
S_COL_MARKET_PRICE   = 4
S_COL_UNREALIZED_PNL = 5
S_COL_REALIZED_PNL   = 6
S_COL_MARKET_VALUE   = 7
S_COL_ACCOUNTS       = 8
S_COL_EXCHANGE       = 9

S_HEADERS: list[str] = [
    "Symbol", "Type", "Pos", "Avg Cost", "Mkt Price",
    "Unrealized", "Realized", "Mkt Value", "Accts", "Exch",
]

# Column key used by the Preferences dialog / column visibility state.
S_COL_KEYS: list[str] = [
    "symbol", "sec_type", "position", "avg_cost", "market_price",
    "unrealized_pnl", "realized_pnl", "market_value", "accounts", "exchange",
]

_S_NUM_COLS = len(S_HEADERS)

_S_RIGHT_ALIGN_COLS = {
    S_COL_POSITION, S_COL_AVG_COST, S_COL_MARKET_PRICE,
    S_COL_UNREALIZED_PNL, S_COL_REALIZED_PNL, S_COL_MARKET_VALUE,
    S_COL_ACCOUNTS,
}

# Theme tokens (keep in sync with account_position_model / trades_summary_model).
TEXT_BOLD = Theme.POS_TEXT
# Same subtle tint used by the Trades summary footer so both summary
# views read the same way visually.
BG_TOTAL  = Theme.POS_BG_ACCT_BAND

# Net-zero threshold for the Zero-Pos toggle — fractional-share
# positions are possible so an exact `== 0` test is too strict.
_ZERO_EPS = 1e-9


class PositionsSummaryModel(QAbstractTableModel):
    """Contract-grouped aggregate of the live portfolio stream.  One row
    per con_id, plus a synthetic TOTAL footer row when any aggregates
    are visible."""

    def __init__(self, parent=None, *, get_alias=None):
        super().__init__(parent)
        self._get_alias = get_alias
        # Full position state keyed by (account, con_id) — mirrors the
        # flat model's local cache so a toggle/filter change rebuilds
        # without waiting for the next worker tick.
        self._positions: dict[tuple[str, int], dict] = {}

        # con_id → aggregate dict.  Rebuilt from `_positions` on every
        # update_positions call.
        self._agg: dict[int, dict] = {}
        # Visible con_ids in display order.  Sorted by `abs(market_value)`
        # descending on each rebuild — mirrors the flat panel's natural
        # "biggest exposure first" order.
        self._order: list[int] = []

        # con_ids the user has temporarily hidden via the context menu /
        # Delete key.  Aggregates for these stay in `_agg` so
        # `restore_hidden` can bring them back.  Cleared on any rebuild
        # driven by a filter change (account selection / show-zero) so
        # the dataset and hidden state never drift apart.
        self._hidden_con_ids: set[int] = set()

        # Mirrors the flat model's show-zero toggle (default True =
        # show flats).  OFF hides aggregates with Σ position ≈ 0 —
        # either genuinely flat single-account or long+short cancelling
        # across accounts.
        self._show_zero: bool = True

        # None means "all accounts"; an empty set also means the same
        # (matches the flat model's semantics so they stay in sync).
        self._selected_accounts: set[str] | None = None

        # Lowercased search text (control-bar search field).  A position
        # contributes to the aggregates only when its account id/alias
        # or symbol matches — so typing an alias shows that account's
        # aggregates, typing a symbol shows that contract's row.
        self._search_text: str = ""

        # Shared cell fonts — family + weight only, size from the table's
        # stylesheet, so every table in both tabs renders identically.
        self._symbol_font = CELL_FONT_BOLD
        self._total_font = CELL_FONT_BOLD

    # ── Proxy / menu helpers ────────────────────────────────────────────────

    def row_data(self, row: int) -> dict | None:
        """Aggregate dict for the given visible row, or ``None`` for the
        TOTAL footer row / out-of-range index."""
        if 0 <= row < len(self._order):
            return self._agg[self._order[row]]
        return None

    def column_keys(self) -> list[str]:
        return list(S_COL_KEYS)

    def column_headers(self) -> list[str]:
        return list(S_HEADERS)

    # ── Qt model plumbing ───────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        n = len(self._order)
        return n + 1 if n else 0

    def _is_total_row(self, row: int) -> bool:
        return bool(self._order) and row == len(self._order)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else _S_NUM_COLS

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole \
                and orientation == Qt.Orientation.Horizontal \
                and 0 <= section < _S_NUM_COLS:
            return S_HEADERS[section]
        return None

    def data(self, index: QModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()

        # Available on every row, TOTAL included — the row-separator
        # delegate asks each row whether it is the footer.
        if role == IsTotalRowRole:
            return self._is_total_row(row)

        if self._is_total_row(row):
            return self._total_row_data(col, role)

        if row < 0 or row >= len(self._order):
            return None
        agg = self._agg[self._order[row]]

        if role == Qt.ItemDataRole.DisplayRole:
            return self._cell_text(agg, col)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._cell_color(agg, col)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in _S_RIGHT_ALIGN_COLS:
                return int(Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.FontRole and col == S_COL_SYMBOL:
            return self._symbol_font
        return None

    # ── TOTAL footer rendering ──────────────────────────────────────────────

    def _visible_aggs(self) -> list[dict]:
        """Aggregates currently shown (hidden con_ids excluded).  The
        footer folds over these so hiding a row recomputes totals."""
        return [self._agg[c] for c in self._order if c in self._agg]

    def _total_row_data(self, col: int, role: int) -> Any:
        if role == Qt.ItemDataRole.DisplayRole:
            return self._total_cell_text(col)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._total_cell_color(col)
        if role == Qt.ItemDataRole.BackgroundRole:
            return QColor(BG_TOTAL)
        if role == Qt.ItemDataRole.FontRole:
            return self._total_font
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in _S_RIGHT_ALIGN_COLS:
                return int(Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter)
        return None

    def _total_cell_text(self, col: int) -> str:
        if col == S_COL_SYMBOL:
            return "TOTAL"
        visible = self._visible_aggs()
        if col == S_COL_UNREALIZED_PNL:
            return _fmt_pnl(_safe_sum(a.get("unrealized_pnl", 0.0)
                                      for a in visible))
        if col == S_COL_REALIZED_PNL:
            return _fmt_pnl(_safe_sum(a.get("realized_pnl", 0.0)
                                      for a in visible))
        if col == S_COL_MARKET_VALUE:
            return _fmt_mktval(_safe_sum(a.get("market_value", 0.0)
                                         for a in visible))
        # Position / Avg Cost / Mkt Price / Accts / Exch / Type — blank;
        # signed sum across different contracts is meaningless.
        return ""

    def _total_cell_color(self, col: int) -> QColor | None:
        if col == S_COL_SYMBOL:
            return QColor(TEXT_BOLD)
        visible = self._visible_aggs()
        if col == S_COL_UNREALIZED_PNL:
            return _pnl_color(_safe_sum(a.get("unrealized_pnl", 0.0)
                                        for a in visible))
        if col == S_COL_REALIZED_PNL:
            return _pnl_color(_safe_sum(a.get("realized_pnl", 0.0)
                                        for a in visible))
        if col == S_COL_MARKET_VALUE:
            return _pnl_color(_safe_sum(a.get("market_value", 0.0)
                                        for a in visible))
        return QColor(TEXT)

    # ── Per-row cell rendering ──────────────────────────────────────────────

    def _cell_text(self, agg: dict, col: int) -> str:
        if col == S_COL_SYMBOL:
            # Options stack root+expiry over strike+right — the shared
            # delegate paints newline'd cells as two small-font lines.
            return format_option_symbol_two_line(
                agg.get("local_symbol") or agg.get("symbol") or "")
        if col == S_COL_SEC_TYPE:
            return agg.get("sec_type", "")
        if col == S_COL_POSITION:
            pos = agg.get("position", 0.0)
            if abs(pos) < _ZERO_EPS:
                return "0"
            if float(pos).is_integer():
                return f"{int(pos):+}"
            return f"{pos:+g}"
        if col == S_COL_AVG_COST:
            num = agg.get("avg_cost_num", 0.0)
            den = agg.get("avg_cost_den", 0.0)
            if abs(den) < _ZERO_EPS:
                return ""
            v = num / den
            return f"{v:,.2f}" if math.isfinite(v) else ""
        if col == S_COL_MARKET_PRICE:
            v = agg.get("market_price", 0.0)
            return f"{v:,.2f}" if v and math.isfinite(v) else ""
        if col == S_COL_UNREALIZED_PNL:
            return _fmt_pnl(agg.get("unrealized_pnl", 0.0))
        if col == S_COL_REALIZED_PNL:
            return _fmt_pnl(agg.get("realized_pnl", 0.0))
        if col == S_COL_MARKET_VALUE:
            return _fmt_mktval(agg.get("market_value", 0.0))
        if col == S_COL_ACCOUNTS:
            n = agg.get("n_accounts", 0)
            return str(n) if n else ""
        if col == S_COL_EXCHANGE:
            return agg.get("exchange", "")
        return ""

    def _cell_color(self, agg: dict, col: int) -> QColor | None:
        if col == S_COL_SYMBOL:
            return QColor(TEXT_BOLD)
        if col == S_COL_POSITION:
            pos = agg.get("position", 0.0)
            if pos > _ZERO_EPS:
                return QColor(CLR_PNL_POS)
            if pos < -_ZERO_EPS:
                return QColor(CLR_PNL_NEG)
            return QColor(TEXT_DIM)
        if col == S_COL_UNREALIZED_PNL:
            return _pnl_color(agg.get("unrealized_pnl", 0.0))
        if col == S_COL_REALIZED_PNL:
            return _pnl_color(agg.get("realized_pnl", 0.0))
        if col == S_COL_MARKET_VALUE:
            return _pnl_color(agg.get("market_value", 0.0))
        if col in (S_COL_SEC_TYPE, S_COL_EXCHANGE, S_COL_ACCOUNTS):
            return QColor(TEXT_DIM)
        return QColor(TEXT)

    # ── Public mutation API ─────────────────────────────────────────────────

    def update_positions(self, batch: list[dict]) -> None:
        """Merge *batch* per-key into the local position cache and
        rebuild the aggregates.

        The worker's portfolio_updated signal emits **incremental**
        batches (only positions that changed during the 250 ms
        throttle window), not full snapshots.  Replacing
        ``_positions`` wholesale would drop every unchanged position
        on every tick and leave the summary showing whichever 1–2
        positions happened to be in the last update.  Positions going
        to zero stay in ``_positions`` with qty=0 — they contribute
        nothing to the aggregates and are gated by ``_show_zero``
        just like in the flat view."""
        if not isinstance(batch, list):
            return
        for pos in batch:
            account = pos.get("account", "")
            con_id = pos.get("con_id")
            if not account or con_id is None:
                continue
            # Apply the futures avg-cost correction on our own copy so
            # we don't mutate the dict the flat model is using.
            p = dict(pos)
            AccountPositionModel._compute_display_avg(p)
            self._positions[(account, int(con_id))] = p
        self._rebuild()

    def set_show_zero(self, show: bool) -> None:
        show = bool(show)
        if show == self._show_zero:
            return
        self._show_zero = show
        self._rebuild()

    def set_search_text(self, text: str) -> None:
        """Filter by account id/alias or symbol (case-insensitive
        substring), then rebuild.  Empty text clears the filter."""
        text = (text or "").strip().lower()
        if text == self._search_text:
            return
        self._search_text = text
        self._rebuild()

    def set_selected_accounts(self, accounts) -> None:
        """Filter aggregates to only the given accounts.  ``None`` or
        an empty set means "all accounts" (matches the flat model's
        convention)."""
        new = set(accounts) if accounts else None
        if new == self._selected_accounts:
            return
        self._selected_accounts = new
        self._rebuild()

    def clear(self) -> None:
        if not self._positions and not self._order:
            return
        self.beginResetModel()
        self._positions.clear()
        self._agg.clear()
        self._order.clear()
        self._hidden_con_ids.clear()
        self.endResetModel()

    # ── Hide / restore ──────────────────────────────────────────────────────

    def hide_con_id(self, con_id: int) -> None:
        """Temporarily remove *con_id*'s row from the view and
        recalculate the TOTAL footer without it.  The aggregate stays
        in ``_agg`` so ``restore_hidden`` can bring it back; the hide
        is cleared on any rebuild."""
        if con_id is None or con_id in self._hidden_con_ids:
            return
        if con_id not in self._order:
            return
        row = self._order.index(con_id)
        self.beginRemoveRows(QModelIndex(), row, row)
        self._order.pop(row)
        self._hidden_con_ids.add(con_id)
        self.endRemoveRows()
        # Totals footer shifted up by one and its values changed.
        if self._order:
            total_row = len(self._order)
            self.dataChanged.emit(
                self.index(total_row, 0),
                self.index(total_row, _S_NUM_COLS - 1))

    def restore_hidden(self) -> None:
        """Un-hide every user-hidden contract.  Order after restore is
        not guaranteed to match the original — the next `_rebuild()`
        call will re-sort by abs(market_value)."""
        if not self._hidden_con_ids:
            return
        self.beginResetModel()
        self._hidden_con_ids.clear()
        # Re-add every agg that's not already visible.  A full rebuild
        # would also work but this preserves the current totals band's
        # scroll position more reliably.
        for c in self._agg.keys():
            if c not in self._order:
                self._order.append(c)
        self.endResetModel()

    def hidden_count(self) -> int:
        return len(self._hidden_con_ids)

    # ── Internal rebuild ────────────────────────────────────────────────────

    def _rebuild(self) -> None:
        """Fold `_positions` into per-con_id aggregates, applying the
        account filter and show-zero rule.  Clears `_hidden_con_ids`
        because a dataset change shouldn't silently hide rows."""
        self.beginResetModel()
        self._agg.clear()
        self._order.clear()
        self._hidden_con_ids.clear()

        # Phase 1: fold every included position into its con_id bucket.
        for (account, con_id), pos in self._positions.items():
            if not self._passes_account_filter(account):
                continue
            if not self._matches_search(account, pos):
                continue
            agg = self._agg.get(con_id)
            if agg is None:
                agg = self._empty_agg(con_id, pos)
                self._agg[con_id] = agg
            self._fold(agg, account, pos)

        # Phase 2: apply show-zero rule and materialise `_order` sorted
        # by absolute market value (biggest exposure at the top).
        visible = [
            (c, a) for c, a in self._agg.items()
            if self._show_zero or abs(a.get("position", 0.0)) > _ZERO_EPS
        ]
        visible.sort(
            key=lambda it: abs(it[1].get("market_value", 0.0) or 0.0),
            reverse=True,
        )
        self._order = [c for c, _a in visible]

        self.endResetModel()

    def _passes_account_filter(self, account: str) -> bool:
        sel = self._selected_accounts
        if not sel:
            return True
        return account in sel

    def _matches_search(self, account: str, pos: dict) -> bool:
        if not self._search_text:
            return True
        if self._search_text in account.lower():
            return True
        alias = (self._get_alias(account) if self._get_alias else "") or ""
        if self._search_text in alias.lower():
            return True
        return any(
            self._search_text in (pos.get(f) or "").lower()
            for f in ("symbol", "local_symbol"))

    @staticmethod
    def _empty_agg(con_id: int, seed: dict) -> dict:
        """Initial aggregate dict for a new con_id.  Contract-level
        fields (symbol, multiplier, exchange, currency, sec_type) are
        seeded from the first position seen — they're the same across
        every account holding the contract, so any seed is fine."""
        return {
            "con_id": con_id,
            "symbol": seed.get("symbol", "") or "",
            "local_symbol": seed.get("local_symbol", "") or "",
            "sec_type": seed.get("sec_type", "") or "",
            "exchange": seed.get("exchange", "") or "",
            "currency": seed.get("currency", "") or "",
            "multiplier": seed.get("multiplier", "") or "",
            # Running totals.
            "position": 0.0,
            "market_value": 0.0,
            "unrealized_pnl": 0.0,
            "realized_pnl": 0.0,
            # Weighted-avg-cost numerator and denominator — see the
            # module docstring.  Numerator accumulates position×cost,
            # denominator accumulates position.  Avg = num / den.  The
            # multiplier cancels out (same for every account holding
            # this con_id), so it doesn't enter either side.
            "avg_cost_num": 0.0,
            "avg_cost_den": 0.0,
            # Market price is a contract-level property; capture once
            # from the first non-zero position seen.  Zero/NaN means
            # "not yet reported" — render blank.
            "market_price": float(seed.get("market_price", 0.0) or 0.0),
            # Accounts that hold a non-zero position in this contract.
            "accounts": set(),
            "n_accounts": 0,
        }

    @staticmethod
    def _fold(agg: dict, account: str, pos: dict) -> None:
        """Add one position's contribution to an aggregate."""
        position = float(pos.get("position", 0.0) or 0.0)
        display_avg = float(pos.get("display_avg", pos.get("avg_cost", 0.0)) or 0.0)
        market_price = float(pos.get("market_price", 0.0) or 0.0)
        market_value = float(pos.get("market_value", 0.0) or 0.0)
        unrealized = float(pos.get("unrealized_pnl", 0.0) or 0.0)
        realized = float(pos.get("realized_pnl", 0.0) or 0.0)

        agg["position"] += position
        if math.isfinite(market_value):
            agg["market_value"] += market_value
        # A flat per-account row has no unrealized P&L by definition —
        # guard against a stale pre-close figure lingering in its dict.
        if math.isfinite(unrealized) and abs(position) > _ZERO_EPS:
            agg["unrealized_pnl"] += unrealized
        if math.isfinite(realized):
            agg["realized_pnl"] += realized

        # Weighted avg cost: position × avg.  Use absolute position so
        # long+short positions both contribute weight (otherwise a
        # long 100 @ 10 + short 100 @ 12 nets to zero qty and avg would
        # be undefined).  Simple sum works because multiplier is equal
        # across accounts for one con_id and cancels from numerator and
        # denominator.
        if math.isfinite(display_avg) and abs(position) > _ZERO_EPS:
            agg["avg_cost_num"] += abs(position) * display_avg
            agg["avg_cost_den"] += abs(position)

        # Market price is contract-level; prefer the first non-zero we
        # see and stick with it (a zero reading from a stale position
        # shouldn't clobber a valid one from another account).
        if (not agg.get("market_price")) and market_price \
                and math.isfinite(market_price):
            agg["market_price"] = market_price

        # Track accounts with a non-zero position so the "Accts" column
        # and the `n_accounts` metric reflect live holders.
        if abs(position) > _ZERO_EPS:
            agg["accounts"].add(account)
            agg["n_accounts"] = len(agg["accounts"])


def _safe_sum(values) -> float:
    """Sum *values*, skipping NaN/inf entries.  Used by the TOTAL row
    so a single missing-data position can't poison the whole footer."""
    total = 0.0
    for v in values:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fv):
            total += fv
    return total
