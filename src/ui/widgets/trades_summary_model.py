"""
TradesSummaryModel – one row per symbol, aggregated across every fill.

Columns: Financial Instrument, Buys, Sells, Net, Avg Buy Price,
Avg Sell Price, Net Total, Commission, Net Incl. Comm, Realized P&L,
Exchange List.

The model owns the aggregate state.  ``add_fill`` either updates an
existing row (dedupe by execId) or folds a new fill into its symbol's
bucket and emits the right ``beginInsertRows`` / ``dataChanged`` pairs.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QFont

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# ── Column indices ────────────────────────────────────────────────────────────
S_COL_SYMBOL        = 0
S_COL_BUYS          = 1
S_COL_SELLS         = 2
S_COL_NET           = 3
S_COL_AVG_BUY       = 4
S_COL_AVG_SELL      = 5
S_COL_TRANS_TOTAL   = 6
S_COL_NET_TOTAL     = 7
S_COL_COMMISSION    = 8
S_COL_NET_INCL_COMM = 9
S_COL_REALIZED      = 10
S_COL_EXCHANGES     = 11

S_HEADERS: list[str] = [
    "Symbol", "Buys", "Sells", "Net",
    "Avg buy price", "Avg sell price", "Trans total",
    "P&L", "Comm", "Net P&L", "Rlz P&L", "Exchanges",
]

# Column key used by the preferences dialog / column visibility state
S_COL_KEYS: list[str] = [
    "symbol", "buys", "sells", "net", "avg_buy", "avg_sell",
    "trans_total", "net_total", "commission", "net_incl_comm",
    "realized_pnl", "exchanges",
]

# Fill-level fields the search box matches against before aggregation.
# Mirrors the flat view's key set so typing ``U123`` (an account) or
# ``NASDAQ`` (an exchange) works the same way in both views — the
# summary just folds the matching subset into symbol aggregates.
_FILL_SEARCH_KEYS: tuple[str, ...] = (
    "symbol", "side", "account", "sec_type", "exchange", "order_ref",
)

_S_NUM_COLS = len(S_HEADERS)

_S_RIGHT_ALIGN_COLS = {
    S_COL_BUYS, S_COL_SELLS, S_COL_NET,
    S_COL_AVG_BUY, S_COL_AVG_SELL, S_COL_TRANS_TOTAL,
    S_COL_NET_TOTAL, S_COL_COMMISSION, S_COL_NET_INCL_COMM,
    S_COL_REALIZED,
}

# Theme tokens (keep in sync with trades_model)
CLR_BUY    = "#3FB950"
CLR_SELL   = "#F85149"
TEXT       = "#C9D1D9"
TEXT_DIM   = "#8B949E"
TEXT_BOLD  = "#E6EDF3"
# Subtle tint for the footer/total row so it reads as a separate
# band from the per-symbol rows above without shouting.
BG_TOTAL   = "#242834"


class TradesSummaryModel(QAbstractTableModel):
    """Symbol-grouped aggregate view of the same fill stream feeding
    ``TradesModel``.  One row per symbol."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # symbol → aggregate dict
        self._agg: dict[str, dict] = {}
        # ordered list of symbols as presented (insertion order of first fill)
        self._order: list[str] = []
        # execId → raw fill dict — keeps the last flattened fill per execId so
        # a re-snapshot that re-emits the same fill doesn't double-count it
        self._fills: dict[str, dict] = {}
        # Date-range filter.  0 means "show every fill regardless of time".
        # ``_latest_fill_date`` is tracked incrementally from the stored
        # fills; ``_anchor_date`` is derived from it + today_ET (mirrors
        # the rule in TradesFilterProxy so the two views stay in sync).
        # Default 1 = "Today" to match TradesFilterProxy's default so a
        # fresh app shows the same window in both flat and summary views.
        self._days_back: int = 1
        self._latest_fill_date: date | None = None
        self._anchor_date: date | None = None
        # Time-threshold filter (driven by the search box parsing a
        # datetime).  When set, it *overrides* the date window — the
        # user is being explicit about which moment forward to include.
        self._time_threshold: datetime | None = None
        # Lowercased substring the panel pushes from the search box.
        # Aggregation is done over only the matching subset of fills,
        # so typing an account like ``U123`` shows that account's
        # symbol totals — the flat proxy can't search aggregate rows
        # on per-fill fields, so the filter lives here instead.
        self._search_text: str = ""
        # Symbols the user has temporarily hidden from the view via the
        # context menu / Delete key.  The underlying aggregate stays in
        # ``_agg`` so a later ``restore_hidden`` can bring it back, and
        # the total row iterates over ``_order`` (visible only) so hides
        # recompute the footer.  Cleared on any filter-driven rebuild.
        self._hidden_symbols: set[str] = set()

        self._symbol_font = QFont("Helvetica Neue, Arial", 11)
        self._symbol_font.setWeight(QFont.Weight.DemiBold)
        # Bold font used across every cell of the totals/footer row.
        self._total_font = QFont("Helvetica Neue, Arial", 11)
        self._total_font.setWeight(QFont.Weight.Bold)

    # ── Proxy helpers ───────────────────────────────────────────────────────

    def row_data(self, row: int) -> dict | None:
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
        # One extra row at the bottom is the totals/footer.  Hidden
        # entirely when the model has no aggregates so an empty view
        # doesn't display a stray "TOTAL 0" band.
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

    # ── Total-row rendering ─────────────────────────────────────────────────

    def _total_row_data(self, col: int, role: int) -> Any:
        """Render a cell on the synthetic footer row that totals every
        aggregate.  Only the four columns the user asked for carry a
        number — every other column stays blank so the band reads as a
        focused summary rather than a second data row."""
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

    def _visible_aggs(self) -> list[dict]:
        """Aggregates that are currently shown (hidden symbols excluded).
        The totals footer folds over these so hiding a row recomputes
        the footer immediately."""
        return [self._agg[s] for s in self._order if s in self._agg]

    def _total_cell_text(self, col: int) -> str:
        if col == S_COL_SYMBOL:
            return "TOTAL"
        visible = self._visible_aggs()
        if col == S_COL_TRANS_TOTAL:
            return self._fmt_compact(
                sum(self._trans_total(a) for a in visible))
        if col == S_COL_NET_TOTAL:
            return self._fmt_signed(
                sum(self._net_total(a) for a in visible))
        if col == S_COL_COMMISSION:
            s = sum(a.get("commission", 0.0) for a in visible)
            return f"{s:,.2f}" if s else ""
        if col == S_COL_NET_INCL_COMM:
            s = sum(self._net_total(a) - a.get("commission", 0.0)
                    for a in visible)
            return self._fmt_signed(s)
        return ""

    def _total_cell_color(self, col: int) -> QColor | None:
        if col == S_COL_SYMBOL:
            return QColor(TEXT_BOLD)
        visible = self._visible_aggs()
        if col == S_COL_NET_TOTAL:
            v = sum(self._net_total(a) for a in visible)
            if v > 0:
                return QColor(CLR_BUY)
            if v < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        if col == S_COL_NET_INCL_COMM:
            v = sum(self._net_total(a) - a.get("commission", 0.0)
                    for a in visible)
            if v > 0:
                return QColor(CLR_BUY)
            if v < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        if col == S_COL_TRANS_TOTAL:
            return QColor(TEXT_BOLD)
        return QColor(TEXT)

    # ── Cell rendering ──────────────────────────────────────────────────────

    @staticmethod
    def _fmt_qty(v: float) -> str:
        if not v:
            return "0"
        return f"{int(v)}" if float(v).is_integer() else f"{v:g}"

    @staticmethod
    def _fmt_money(v: float) -> str:
        return f"{v:,.2f}" if v else "0.00"

    @staticmethod
    def _fmt_signed(v: float) -> str:
        if v == 0:
            return "0.00"
        return f"{v:+,.2f}"

    @staticmethod
    def _fmt_compact(v: float) -> str:
        """Human-readable amount: plain for values <$1 k, ``k`` suffix up
        to a million, ``m`` suffix beyond.  Used by the Trans total
        column where futures notionals routinely run into the millions
        (NQ at ~$20 k face × hundreds of contracts)."""
        if v == 0:
            return "0"
        sign = "-" if v < 0 else ""
        a = abs(v)
        if a < 1_000:
            return f"{sign}{a:,.2f}"
        if a < 1_000_000:
            return f"{sign}{a / 1_000:.2f}k"
        return f"{sign}{a / 1_000_000:.2f}m"

    def _cell_text(self, agg: dict, col: int) -> str:
        if col == S_COL_SYMBOL:
            return agg.get("symbol", "")
        if col == S_COL_BUYS:
            return self._fmt_qty(agg.get("buys_qty", 0.0))
        if col == S_COL_SELLS:
            return self._fmt_qty(agg.get("sells_qty", 0.0))
        if col == S_COL_NET:
            net = agg.get("buys_qty", 0.0) - agg.get("sells_qty", 0.0)
            if net == 0:
                return "0"
            return f"{int(net):+}" if float(net).is_integer() else f"{net:+g}"
        if col == S_COL_AVG_BUY:
            b_q = agg.get("buys_qty", 0.0)
            return f"{(agg.get('buys_notional', 0.0) / b_q):,.2f}" if b_q else ""
        if col == S_COL_AVG_SELL:
            s_q = agg.get("sells_qty", 0.0)
            return f"{(agg.get('sells_notional', 0.0) / s_q):,.2f}" if s_q else ""
        if col == S_COL_TRANS_TOTAL:
            return self._fmt_compact(self._trans_total(agg))
        if col == S_COL_NET_TOTAL:
            return self._fmt_signed(self._net_total(agg))
        if col == S_COL_COMMISSION:
            c = agg.get("commission", 0.0)
            return f"{c:,.2f}" if c else ""
        if col == S_COL_NET_INCL_COMM:
            return self._fmt_signed(self._net_total(agg) - agg.get("commission", 0.0))
        if col == S_COL_REALIZED:
            pnl = agg.get("realized_pnl", 0.0)
            if pnl == 0:
                # If no fill carried a commissionReport we render blank rather
                # than a misleading 0.00.
                return "0.00" if agg.get("has_any_realized") else ""
            return self._fmt_signed(pnl)
        if col == S_COL_EXCHANGES:
            return ", ".join(sorted(agg.get("exchanges", set())))
        return ""

    @staticmethod
    def _net_total(agg: dict) -> float:
        """Cash-flow net: sells proceeds minus buys cost, scaled by the
        contract multiplier so futures (NQ × 20, MNQ × 2, …) report in
        dollars rather than per-point units.  Stocks round-trip as-is
        because their multiplier is 1."""
        diff = agg.get("sells_notional", 0.0) - agg.get("buys_notional", 0.0)
        return diff * agg.get("multiplier", 1.0)

    @staticmethod
    def _trans_total(agg: dict) -> float:
        """Gross transaction volume — total buy notional + total sell
        notional, multiplier-scaled.  Unlike P&L this is always
        non-negative and indicates how much capital changed hands."""
        total = agg.get("buys_notional", 0.0) + agg.get("sells_notional", 0.0)
        return total * agg.get("multiplier", 1.0)

    def _cell_color(self, agg: dict, col: int) -> QColor | None:
        if col == S_COL_SYMBOL:
            return QColor(TEXT_BOLD)
        if col == S_COL_NET:
            net = agg.get("buys_qty", 0.0) - agg.get("sells_qty", 0.0)
            if net > 0:
                return QColor(CLR_BUY)
            if net < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        if col in (S_COL_NET_TOTAL, S_COL_NET_INCL_COMM):
            v = (self._net_total(agg) if col == S_COL_NET_TOTAL
                 else self._net_total(agg) - agg.get("commission", 0.0))
            if v > 0:
                return QColor(CLR_BUY)
            if v < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        if col == S_COL_REALIZED:
            pnl = agg.get("realized_pnl", 0.0)
            if pnl > 0:
                return QColor(CLR_BUY)
            if pnl < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        if col in (S_COL_EXCHANGES,):
            return QColor(TEXT_DIM)
        return QColor(TEXT)

    # ── Public mutation API ─────────────────────────────────────────────────

    def add_fill(self, fill: dict) -> None:
        """Fold a flattened fill into its symbol aggregate, or refresh in
        place if a prior fill with the same execId was already counted.

        Respects the date filter set via ``set_days_back``: fills are
        always stored in ``_fills`` (so a later filter change can pull
        them back in) but only aggregated into the visible rows when
        they fall inside the current window.
        """
        exec_id = fill.get("exec_id", "")
        symbol = fill.get("symbol", "") or "—"

        # Detect an anchor advance (e.g. a fresh session's first fill
        # arrives and pushes "today" forward).  A full rebuild is the
        # simplest correct response — yesterday's aggregates may need
        # to drop out of a 1-day window and rebuilding is O(n_fills).
        prev_anchor = self._anchor_date
        local_date = self._fill_local_date(fill)
        if local_date is not None and (
                self._latest_fill_date is None
                or local_date > self._latest_fill_date):
            self._latest_fill_date = local_date
        self._anchor_date = self._compute_anchor()
        anchor_changed = (
            self._days_back > 0 and prev_anchor != self._anchor_date)

        # Re-snapshot case: same execId arrived again.  Roll back the old
        # contribution and re-apply with the new data (commission may now
        # have arrived for the same execId).  Only subtract if the prior
        # fill was actually counted (i.e. it passed the filter).
        prev = self._fills.get(exec_id) if exec_id else None
        if prev is not None and self._passes_filter(prev):
            prev_symbol = prev.get("symbol", "") or "—"
            self._subtract_from_agg(prev_symbol, prev)
            if self._agg.get(prev_symbol, {}).get("fill_count", 0) == 0:
                self._drop_symbol_row(prev_symbol)

        if exec_id:
            self._fills[exec_id] = fill

        if anchor_changed:
            # Rebuild from all fills under the new anchor; we've already
            # stored the new fill above so it's included in the rebuild.
            self._rebuild_from_fills()
            return

        # Gate incremental aggregation on the filter — fills outside the
        # window are kept in _fills but not reflected in the grid.
        if not self._passes_filter(fill):
            return

        # Add to aggregate.  Insert a new row if this symbol is new.
        if symbol not in self._agg:
            n = len(self._order)
            if n == 0:
                # First aggregate materialises the totals row too, so we
                # insert rows [0, 1] in a single transaction to keep Qt's
                # row-count bookkeeping consistent.
                self.beginInsertRows(QModelIndex(), 0, 1)
            else:
                self.beginInsertRows(QModelIndex(), n, n)
            self._agg[symbol] = self._empty_agg(symbol)
            self._order.append(symbol)
            self.endInsertRows()

        self._add_to_agg(symbol, fill)

        # Refresh the updated row *and* the totals footer.  When the
        # symbol is user-hidden the row isn't in ``_order`` — we update
        # the stored aggregate but emit nothing because neither the
        # hidden row nor the totals (which exclude hidden rows) change
        # visually.
        if symbol in self._order:
            row = self._order.index(symbol)
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, _S_NUM_COLS - 1))
            total_row = len(self._order)
            self.dataChanged.emit(
                self.index(total_row, 0),
                self.index(total_row, _S_NUM_COLS - 1))

    def set_days_back(self, days: int) -> None:
        """Change the date window (1 = today, 2 = last 2 days, …).
        0 or negative means "show every fill regardless of time"."""
        days = max(0, int(days or 0))
        if days == self._days_back:
            return
        self._days_back = days
        self._rebuild_from_fills()

    def set_time_threshold(self, threshold: datetime | None) -> None:
        """Re-aggregate using only fills at or after *threshold* (an
        ET-aware datetime).  ``None`` removes the override and falls
        back to the date window."""
        if threshold == self._time_threshold:
            return
        self._time_threshold = threshold
        self._rebuild_from_fills()

    def set_search_text(self, text: str) -> None:
        """Substring filter applied to the fill stream before
        aggregation.  Callers should pass the raw substring (datetime
        queries are routed through ``set_time_threshold`` instead)."""
        new_search = (text or "").strip().lower()
        if new_search == self._search_text:
            return
        self._search_text = new_search
        self._rebuild_from_fills()

    def clear(self) -> None:
        if not self._order and not self._fills:
            return
        self.beginResetModel()
        self._agg.clear()
        self._order.clear()
        self._fills.clear()
        self._hidden_symbols.clear()
        self._latest_fill_date = None
        self._anchor_date = None
        self.endResetModel()

    # ── Hide / restore (user-driven, temporary) ────────────────────────────

    def hide_symbol(self, symbol: str) -> None:
        """Temporarily remove *symbol*'s row from the view and
        recalculate the totals footer without it.  The aggregate stays
        in ``_agg`` so ``restore_hidden`` can bring it back; the hide
        is cleared on any filter-driven rebuild."""
        if not symbol or symbol in self._hidden_symbols:
            return
        if symbol not in self._order:
            return
        row = self._order.index(symbol)
        self.beginRemoveRows(QModelIndex(), row, row)
        self._order.pop(row)
        self._hidden_symbols.add(symbol)
        self.endRemoveRows()
        # Totals footer shifted up by one and its values changed.
        if self._order:
            total_row = len(self._order)
            self.dataChanged.emit(
                self.index(total_row, 0),
                self.index(total_row, _S_NUM_COLS - 1))

    def restore_hidden(self) -> None:
        """Un-hide every user-hidden symbol.  Order after restore is
        not guaranteed to match the original — restored symbols land
        at the tail of ``_order``."""
        if not self._hidden_symbols:
            return
        self.beginResetModel()
        for s in list(self._hidden_symbols):
            if s in self._agg and s not in self._order:
                self._order.append(s)
        self._hidden_symbols.clear()
        self.endResetModel()

    def hidden_count(self) -> int:
        return len(self._hidden_symbols)

    # ── Date-filter helpers ─────────────────────────────────────────────────

    @staticmethod
    def _fill_local_date(fill: dict) -> date | None:
        """Parse a fill's ``time_sort`` into an ET calendar date."""
        ts = fill.get("time_sort", "")
        if not ts:
            return None
        try:
            parsed = datetime.fromisoformat(ts)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_ET)
        return parsed.astimezone(_ET).date()

    def _compute_anchor(self) -> date | None:
        """Rule mirrors ``TradesFilterProxy``: anchor = ``today_ET`` during
        live trading, but if the latest fill is in the past (weekend /
        pre-open) fall back to that fill's date so the window isn't empty.
        Returns ``None`` when no fills have been seen yet."""
        if self._latest_fill_date is None:
            return None
        today = datetime.now(_ET).date()
        return self._latest_fill_date if self._latest_fill_date < today else today

    def _passes_filter(self, fill: dict) -> bool:
        if not self._passes_time_filter(fill):
            return False
        if not self._passes_search_filter(fill):
            return False
        return True

    def _passes_time_filter(self, fill: dict) -> bool:
        # Time-threshold overrides the date window — when a user types
        # a specific moment into the search box we want the aggregates
        # to reflect exactly that cutoff (match what the flat view is
        # already showing).
        if self._time_threshold is not None:
            ts = fill.get("time_sort", "")
            if not ts:
                return True
            try:
                parsed = datetime.fromisoformat(ts)
            except ValueError:
                return True
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_ET)
            return parsed >= self._time_threshold

        if self._days_back <= 0 or self._anchor_date is None:
            return True
        local_date = self._fill_local_date(fill)
        if local_date is None:
            return True
        cutoff = self._anchor_date - timedelta(days=self._days_back - 1)
        return local_date >= cutoff

    def _passes_search_filter(self, fill: dict) -> bool:
        if not self._search_text:
            return True
        needle = self._search_text
        for k in _FILL_SEARCH_KEYS:
            v = fill.get(k, "")
            if v and needle in str(v).lower():
                return True
        return False

    def _rebuild_from_fills(self) -> None:
        """Replace aggregates with a fresh fold over every stored fill,
        honouring the current date filter.  Called when the filter
        window or anchor changes — incremental add_fill is used the rest
        of the time.  User-hides are cleared here because the dataset
        changed: keeping a hide across filter windows would silently
        exclude rows the user may not realize are gone."""
        self.beginResetModel()
        self._agg.clear()
        self._order.clear()
        self._hidden_symbols.clear()
        for fill in self._fills.values():
            if not self._passes_filter(fill):
                continue
            symbol = fill.get("symbol", "") or "—"
            if symbol not in self._agg:
                self._agg[symbol] = self._empty_agg(symbol)
                self._order.append(symbol)
            self._add_to_agg(symbol, fill)
        self.endResetModel()

    # ── Internal aggregation helpers ────────────────────────────────────────

    @staticmethod
    def _empty_agg(symbol: str) -> dict:
        return {
            "symbol": symbol,
            "buys_qty": 0.0,
            "sells_qty": 0.0,
            # Price-weighted notionals — kept in per-point terms so
            # ``avg_buy`` / ``avg_sell`` divide out cleanly.  The
            # contract multiplier is applied at Net-total computation
            # time (see ``_net_total``).
            "buys_notional": 0.0,
            "sells_notional": 0.0,
            # Captured from the first fill and assumed stable per
            # symbol (IB's ``contract.multiplier`` doesn't vary within
            # a root).  1.0 for stocks, 20 for NQ, 2 for MNQ, etc.
            "multiplier": 1.0,
            "commission": 0.0,
            "realized_pnl": 0.0,
            "has_any_realized": False,
            "exchanges": set(),
            "fill_count": 0,
        }

    def _add_to_agg(self, symbol: str, fill: dict) -> None:
        agg = self._agg[symbol]
        side = fill.get("side", "")
        shares = float(fill.get("shares", 0.0) or 0.0)
        price = float(fill.get("price", 0.0) or 0.0)
        # Capture the contract multiplier from the first fill carrying
        # one.  Subsequent fills on the same symbol have the same value
        # (IB's contract.multiplier is a root-level attribute).
        mult = fill.get("multiplier")
        if mult is not None:
            try:
                mult_f = float(mult)
                if mult_f > 0:
                    agg["multiplier"] = mult_f
            except (TypeError, ValueError):
                pass
        if side == "BOT":
            agg["buys_qty"] += shares
            agg["buys_notional"] += shares * price
        elif side == "SLD":
            agg["sells_qty"] += shares
            agg["sells_notional"] += shares * price
        if fill.get("commission") is not None:
            agg["commission"] += float(fill["commission"])
        if fill.get("realized_pnl") is not None:
            agg["realized_pnl"] += float(fill["realized_pnl"])
            agg["has_any_realized"] = True
        exch = fill.get("exchange", "")
        if exch:
            agg["exchanges"].add(exch)
        agg["fill_count"] += 1

    def _subtract_from_agg(self, symbol: str, fill: dict) -> None:
        agg = self._agg.get(symbol)
        if agg is None:
            return
        side = fill.get("side", "")
        shares = float(fill.get("shares", 0.0) or 0.0)
        price = float(fill.get("price", 0.0) or 0.0)
        if side == "BOT":
            agg["buys_qty"] -= shares
            agg["buys_notional"] -= shares * price
        elif side == "SLD":
            agg["sells_qty"] -= shares
            agg["sells_notional"] -= shares * price
        if fill.get("commission") is not None:
            agg["commission"] -= float(fill["commission"])
        if fill.get("realized_pnl") is not None:
            agg["realized_pnl"] -= float(fill["realized_pnl"])
        agg["fill_count"] = max(0, agg["fill_count"] - 1)
        # Exchange membership isn't reversed — harmless; a symbol that once
        # routed through NYSE still shows NYSE in its exchange list.

    def _drop_symbol_row(self, symbol: str) -> None:
        # Hidden symbols aren't in ``_order`` but still have a stored
        # aggregate — drop from internal state only; no row signals
        # needed because the view isn't showing them.
        if symbol in self._hidden_symbols:
            self._hidden_symbols.discard(symbol)
            self._agg.pop(symbol, None)
            return
        if symbol not in self._order:
            return
        row = self._order.index(symbol)
        if len(self._order) == 1:
            # Dropping the last aggregate also hides the totals footer
            # so the view goes back to empty — remove both rows in one
            # transaction.
            self.beginRemoveRows(QModelIndex(), 0, 1)
        else:
            self.beginRemoveRows(QModelIndex(), row, row)
        self._order.pop(row)
        self._agg.pop(symbol, None)
        self.endRemoveRows()
