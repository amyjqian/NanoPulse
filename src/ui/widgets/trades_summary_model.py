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
S_COL_TYPE          = 1
S_COL_BUYS          = 2
S_COL_SELLS         = 3
S_COL_NET           = 4
S_COL_AVG_BUY       = 5
S_COL_AVG_SELL      = 6
S_COL_TRANS_TOTAL   = 7
S_COL_NET_TOTAL     = 8
S_COL_COMMISSION    = 9
S_COL_NET_INCL_COMM = 10
S_COL_REALIZED      = 11
S_COL_EXCHANGES     = 12

S_HEADERS: list[str] = [
    "Symbol", "Type", "Buys", "Sells", "Net",
    "Avg buy price", "Avg sell price", "Trans total",
    "P&L", "Comm", "Net P&L", "Rlz P&L", "Exchanges",
]

# Column key used by the preferences dialog / column visibility state
S_COL_KEYS: list[str] = [
    "symbol", "sec_type", "buys", "sells", "net", "avg_buy", "avg_sell",
    "trans_total", "net_total", "commission", "net_incl_comm",
    "realized_pnl", "exchanges",
]

# Fill-level fields the search box matches against before aggregation.
# Mirrors the flat view's key set so typing ``U123`` (an account) or
# ``NASDAQ`` (an exchange) works the same way in both views — the
# summary just folds the matching subset into symbol aggregates.
_FILL_SEARCH_KEYS: tuple[str, ...] = (
    "symbol", "local_symbol", "side", "account", "sec_type",
    "exchange", "order_ref",
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
TEXT       = "#E6EBF2"
TEXT_DIM   = "#A3AFC2"
TEXT_BOLD  = "#E6EBF2"
# Subtle tint for the footer/total row so it reads as a separate
# band from the per-symbol rows above without shouting.
BG_TOTAL   = "#232A3B"


class TradesSummaryModel(QAbstractTableModel):
    """Symbol-grouped aggregate view of the same fill stream feeding
    ``TradesModel``.  One row per (symbol, sec_type) — so TSLA stock and
    TSLA options aggregate separately even though they share the root."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # (symbol, sec_type) → aggregate dict.  Keyed as a tuple so the
        # same root symbol under different instrument types (STK vs OPT
        # vs FUT) keeps its own bucket.
        self._agg: dict[tuple[str, str], dict] = {}
        # ordered list of keys as presented (insertion order of first fill)
        self._order: list[tuple[str, str]] = []
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
        # Keys the user has temporarily hidden from the view via the
        # context menu / Delete key.  The underlying aggregate stays in
        # ``_agg`` so a later ``restore_hidden`` can bring it back, and
        # the total row iterates over ``_order`` (visible only) so hides
        # recompute the footer.  Cleared on any filter-driven rebuild.
        self._hidden_symbols: set[tuple[str, str]] = set()
        # exec_ids the user hid in the *flat* list view.  Mirrored here by
        # the panel so a fill hidden in the list also drops out of its
        # symbol aggregate.  Unlike ``_hidden_symbols`` this is owned by
        # the flat proxy, so it survives this model's filter rebuilds.
        self._hidden_fills: set[str] = set()
        # Keys the user asked to "match buy & sell": the larger of the
        # buy/sell legs is trimmed (qty + notional, avg price kept) down
        # to the smaller so Net reads flat and the P&L columns show only
        # the closed round-trip.  Applied at render time via
        # ``_effective_agg`` so live fills keep updating the raw aggregate.
        self._matched_keys: set[tuple[str, str]] = set()

        # Last traded price per uppercase symbol (root and/or local),
        # fed by the host via ``set_last_price``.  The P&L footer row
        # marks unclosed (net long/short) positions to these prices.
        self._last_prices: dict[str, float] = {}

        self._symbol_font = QFont("Inter, Helvetica Neue, Arial", 11)
        self._symbol_font.setWeight(QFont.Weight.DemiBold)
        # Bold font used across every cell of the totals/footer row.
        self._total_font = QFont("Inter, Helvetica Neue, Arial", 11)
        self._total_font.setWeight(QFont.Weight.Bold)

    # ── Proxy helpers ───────────────────────────────────────────────────────

    def row_data(self, row: int) -> dict | None:
        if 0 <= row < len(self._order):
            return self._agg[self._order[row]]
        return None

    def row_key(self, row: int) -> tuple[str, str] | None:
        # Aggregation key for ``hide_symbol`` lookups.  The key uses
        # ``cid:<con_id>`` (see ``_agg_key``) and is *not* derivable from
        # the agg dict's display fields, so callers that want to hide a
        # row must round-trip through this helper.
        if 0 <= row < len(self._order):
            return self._order[row]
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
        # Three extra rows at the bottom: the "P&L" totals/footer, the
        # "Realized P&L" (paired round-trips only) band, and the
        # "Unrealized P&L" (open remainder, marked to last) band.
        # Hidden entirely when the model has no aggregates so an empty
        # view doesn't display stray footer bands.
        return n + 3 if n else 0

    def _is_total_row(self, row: int) -> bool:
        return bool(self._order) and row == len(self._order)

    def _is_realized_row(self, row: int) -> bool:
        return bool(self._order) and row == len(self._order) + 1

    def _is_unrealized_row(self, row: int) -> bool:
        return bool(self._order) and row == len(self._order) + 2

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
        if self._is_realized_row(row):
            return self._realized_row_data(col, role)
        if self._is_unrealized_row(row):
            return self._unrealized_row_data(col, role)

        if row < 0 or row >= len(self._order):
            return None
        agg = self._effective_agg(self._order[row])
        if agg is None:
            return None

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
        """Aggregates that are currently shown (hidden symbols excluded),
        each in its *effective* form so matched rows fold their trimmed
        round-trip values into the totals footer.  The footer recomputes
        immediately when a row is hidden or matched."""
        aggs = [self._effective_agg(s) for s in self._order if s in self._agg]
        return [a for a in aggs if a is not None]

    def _effective_agg(self, key: tuple[str, str]) -> dict | None:
        """The aggregate as rendered.  For a matched key the larger leg is
        trimmed to the smaller by FIFO pairing — the earliest buys pair
        with the earliest sells, so the excess dropped from the larger
        leg is always its latest fills — leaving Net flat and the P&L
        columns reflecting only the closed portion; every other row
        returns its raw aggregate unchanged."""
        agg = self._agg.get(key)
        if agg is None or key not in self._matched_keys:
            return agg
        b_q = agg.get("buys_qty", 0.0)
        s_q = agg.get("sells_qty", 0.0)
        if b_q == s_q:
            return agg
        paired = self._fifo_paired(key)
        if paired is None:
            return agg
        eff = dict(agg)
        eff["buys_qty"] = paired["buys_qty"]
        eff["sells_qty"] = paired["sells_qty"]
        eff["buys_notional"] = paired["buys_notional"]
        eff["sells_notional"] = paired["sells_notional"]
        return eff

    # ── Realized-P&L row rendering (paired round-trips only) ────────────────

    def _fills_for_key(self, key: tuple[str, str]) -> list[dict]:
        """This key's currently-counted fills in time order (earliest
        first) — same filter set the aggregates are built from."""
        out = [f for f in self._fills.values()
               if self._passes_filter(f) and self._agg_key(f) == key]
        out.sort(key=lambda f: f.get("time_sort", "") or "")
        return out

    def _fifo_paired(self, key: tuple[str, str]) -> dict | None:
        """The paired (closed round-trip) portion of *key*, matched FIFO:
        the earliest buy fills pair with the earliest sell fills, so an
        open remainder is always the *latest* fills on the larger leg.
        Commission is the paired fills' own commission (partial fills
        prorated).  Returns ``None`` when nothing is paired."""
        buys: list[tuple[float, float, float]] = []   # (qty, price, comm)
        sells: list[tuple[float, float, float]] = []
        for f in self._fills_for_key(key):
            qty = float(f.get("shares", 0.0) or 0.0)
            if qty <= 0:
                continue
            price = float(f.get("price", 0.0) or 0.0)
            comm = float(f.get("commission") or 0.0)
            side = f.get("side", "")
            if side == "BOT":
                buys.append((qty, price, comm))
            elif side == "SLD":
                sells.append((qty, price, comm))
        matched = min(sum(q for q, _p, _c in buys),
                      sum(q for q, _p, _c in sells))
        if matched <= 0:
            return None

        def take(legs: list[tuple[float, float, float]],
                 target: float) -> tuple[float, float]:
            left = target
            notional = comm = 0.0
            for qty, price, fill_comm in legs:
                if left <= 0:
                    break
                use = min(qty, left)
                notional += use * price
                comm += fill_comm * (use / qty)
                left -= use
            return notional, comm

        b_notional, b_comm = take(buys, matched)
        s_notional, s_comm = take(sells, matched)
        agg = self._agg.get(key) or {}
        return {
            "symbol": agg.get("symbol", ""),
            "root_symbol": agg.get("root_symbol", ""),
            "buys_qty": matched,
            "sells_qty": matched,
            "buys_notional": b_notional,
            "sells_notional": s_notional,
            "commission": b_comm + s_comm,
            "multiplier": agg.get("multiplier", 1.0),
        }

    def _paired_aggs(self) -> list[dict]:
        pairs = (self._fifo_paired(k) for k in self._order
                 if k in self._agg)
        return [p for p in pairs if p is not None]

    def _realized_row_data(self, col: int, role: int) -> Any:
        """Render a cell on the synthetic "Realized P&L" footer row —
        same shape as the TOTAL row but computed over only the paired
        (buy-and-sell-matched) portion of every visible aggregate."""
        if role == Qt.ItemDataRole.DisplayRole:
            return self._realized_cell_text(col)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._realized_cell_color(col)
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

    def _realized_cell_text(self, col: int) -> str:
        if col == S_COL_SYMBOL:
            return "Realized P&L"
        paired = self._paired_aggs()
        if col == S_COL_TRANS_TOTAL:
            return self._fmt_compact(
                sum(self._trans_total(a) for a in paired))
        if col == S_COL_NET_TOTAL:
            return self._fmt_signed(
                sum(self._net_total(a) for a in paired))
        if col == S_COL_COMMISSION:
            s = sum(a.get("commission", 0.0) for a in paired)
            return f"{s:,.2f}" if s else ""
        if col == S_COL_NET_INCL_COMM:
            s = sum(self._net_total(a) - a.get("commission", 0.0)
                    for a in paired)
            return self._fmt_signed(s)
        return ""

    def _realized_cell_color(self, col: int) -> QColor | None:
        if col == S_COL_SYMBOL:
            return QColor(TEXT_BOLD)
        paired = self._paired_aggs()
        if col == S_COL_NET_TOTAL:
            v = sum(self._net_total(a) for a in paired)
        elif col == S_COL_NET_INCL_COMM:
            v = sum(self._net_total(a) - a.get("commission", 0.0)
                    for a in paired)
        elif col == S_COL_TRANS_TOTAL:
            return QColor(TEXT_BOLD)
        else:
            return QColor(TEXT)
        if v > 0:
            return QColor(CLR_BUY)
        if v < 0:
            return QColor(CLR_SELL)
        return QColor(TEXT_DIM)

    # ── Unrealized-P&L row rendering (open remainder, marked) ───────────────

    def _unrealized_parts(self) -> list[tuple[dict, dict | None]]:
        """(effective aggregate, FIFO-paired portion) per visible symbol.
        The unrealized row is each column's total minus its realized
        part, so P&L = Realized + Unrealized always reconciles."""
        parts = []
        for key in self._order:
            agg = self._effective_agg(key)
            if agg is not None:
                parts.append((agg, self._fifo_paired(key)))
        return parts

    def _unrealized_value(self, col: int) -> float:
        total = 0.0
        for agg, paired in self._unrealized_parts():
            if col == S_COL_TRANS_TOTAL:
                total += self._trans_total(agg) - (
                    self._trans_total(paired) if paired else 0.0)
            elif col == S_COL_NET_TOTAL:
                total += self._marked_net_total(agg) - (
                    self._net_total(paired) if paired else 0.0)
            elif col == S_COL_COMMISSION:
                total += agg.get("commission", 0.0) - (
                    paired.get("commission", 0.0) if paired else 0.0)
            elif col == S_COL_NET_INCL_COMM:
                total += (self._marked_net_total(agg)
                          - agg.get("commission", 0.0)) - (
                    (self._net_total(paired)
                     - paired.get("commission", 0.0)) if paired else 0.0)
        return total

    def _unrealized_row_data(self, col: int, role: int) -> Any:
        """Render a cell on the synthetic "Unrealized P&L" footer row —
        the open (unpaired) remainder of every visible aggregate, with
        its P&L marked to the last traded price."""
        if role == Qt.ItemDataRole.DisplayRole:
            return self._unrealized_cell_text(col)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._unrealized_cell_color(col)
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

    def _unrealized_cell_text(self, col: int) -> str:
        if col == S_COL_SYMBOL:
            return "Unrealized P&L"
        if col == S_COL_TRANS_TOTAL:
            return self._fmt_compact(self._unrealized_value(col))
        if col == S_COL_NET_TOTAL:
            return self._fmt_signed(self._unrealized_value(col))
        if col == S_COL_COMMISSION:
            s = self._unrealized_value(col)
            return f"{s:,.2f}" if s else ""
        if col == S_COL_NET_INCL_COMM:
            return self._fmt_signed(self._unrealized_value(col))
        return ""

    def _unrealized_cell_color(self, col: int) -> QColor | None:
        if col == S_COL_SYMBOL:
            return QColor(TEXT_BOLD)
        if col == S_COL_TRANS_TOTAL:
            return QColor(TEXT_BOLD)
        if col in (S_COL_NET_TOTAL, S_COL_NET_INCL_COMM):
            v = self._unrealized_value(col)
            if v > 0:
                return QColor(CLR_BUY)
            if v < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        return QColor(TEXT)

    def _total_cell_text(self, col: int) -> str:
        if col == S_COL_SYMBOL:
            return "P&L"
        visible = self._visible_aggs()
        if col == S_COL_TRANS_TOTAL:
            return self._fmt_compact(
                sum(self._trans_total(a) for a in visible))
        if col == S_COL_NET_TOTAL:
            return self._fmt_signed(
                sum(self._marked_net_total(a) for a in visible))
        if col == S_COL_COMMISSION:
            s = sum(a.get("commission", 0.0) for a in visible)
            return f"{s:,.2f}" if s else ""
        if col == S_COL_NET_INCL_COMM:
            s = sum(self._marked_net_total(a) - a.get("commission", 0.0)
                    for a in visible)
            return self._fmt_signed(s)
        return ""

    def _total_cell_color(self, col: int) -> QColor | None:
        if col == S_COL_SYMBOL:
            return QColor(TEXT_BOLD)
        visible = self._visible_aggs()
        if col == S_COL_NET_TOTAL:
            v = sum(self._marked_net_total(a) for a in visible)
            if v > 0:
                return QColor(CLR_BUY)
            if v < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        if col == S_COL_NET_INCL_COMM:
            v = sum(self._marked_net_total(a) - a.get("commission", 0.0)
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
        if col == S_COL_TYPE:
            return agg.get("sec_type", "") or ""
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

    def _last_price_for(self, agg: dict) -> float | None:
        for key in (agg.get("root_symbol", ""),
                    (agg.get("symbol", "") or "").upper()):
            if key:
                p = self._last_prices.get(key)
                if p:
                    return p
        return None

    def _marked_net_total(self, agg: dict) -> float:
        """Cash-flow net with the open (unpaired) leg marked to the last
        traded price: a net-long leg is valued as if sold at the last
        price, a net-short leg as if bought back.  Falls back to the
        plain cash net when no live price has been seen for the symbol
        yet."""
        cash = self._net_total(agg)
        net_qty = agg.get("buys_qty", 0.0) - agg.get("sells_qty", 0.0)
        if net_qty == 0:
            return cash
        last = self._last_price_for(agg)
        if last is None:
            return cash
        return cash + net_qty * last * agg.get("multiplier", 1.0)

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
        if col in (S_COL_TYPE, S_COL_EXCHANGES):
            return QColor(TEXT_DIM)
        return QColor(TEXT)

    # ── Public mutation API ─────────────────────────────────────────────────

    @staticmethod
    def _agg_key(fill: dict) -> tuple[str, str]:
        """Aggregation key.  Prefer ``con_id`` so the same contract folds
        into one row even when IB echoes back ``contract.symbol`` as the
        local symbol on some fills (e.g. an NQ futures fill arriving with
        ``symbol="NQM6"`` while sibling fills carry ``symbol="NQ"``).
        Falls back to ``(symbol, sec_type)`` for legacy fills missing a
        conId.  A root symbol traded as both a stock and an option (e.g.
        TSLA) keeps separate rows because each contract has its own conId.
        """
        con_id = int(fill.get("con_id", 0) or 0)
        if con_id > 0:
            return (f"cid:{con_id}", fill.get("sec_type", "") or "")
        return (fill.get("symbol", "") or "—", fill.get("sec_type", "") or "")

    @staticmethod
    def _fill_display_symbol(fill: dict) -> str:
        """Display label for a fill: localSymbol (``NQM6``) when present,
        else the root symbol (``NQ``).  Matches the orders blotter and
        positions view so the same contract reads identically across
        every panel."""
        return (fill.get("local_symbol", "")
                or fill.get("symbol", "")
                or "—")

    def add_fill(self, fill: dict) -> None:
        """Fold a flattened fill into its symbol aggregate, or refresh in
        place if a prior fill with the same execId was already counted.

        Respects the date filter set via ``set_days_back``: fills are
        always stored in ``_fills`` (so a later filter change can pull
        them back in) but only aggregated into the visible rows when
        they fall inside the current window.
        """
        exec_id = fill.get("exec_id", "")
        key = self._agg_key(fill)

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
            prev_key = self._agg_key(prev)
            self._subtract_from_agg(prev_key, prev)
            if self._agg.get(prev_key, {}).get("fill_count", 0) == 0:
                self._drop_symbol_row(prev_key)

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

        # Add to aggregate.  Insert a new row if this key is new.
        if key not in self._agg:
            n = len(self._order)
            if n == 0:
                # First aggregate materialises all three footer rows
                # (P&L + Realized + Unrealized) too, so we insert rows
                # [0, 3] in a single transaction to keep Qt's row-count
                # bookkeeping consistent.
                self.beginInsertRows(QModelIndex(), 0, 3)
            else:
                self.beginInsertRows(QModelIndex(), n, n)
            self._agg[key] = self._empty_agg(fill)
            self._order.append(key)
            self.endInsertRows()

        self._add_to_agg(key, fill)

        # Refresh the updated row *and* the totals footer.  When the
        # symbol is user-hidden the row isn't in ``_order`` — we update
        # the stored aggregate but emit nothing because neither the
        # hidden row nor the totals (which exclude hidden rows) change
        # visually.
        if key in self._order:
            row = self._order.index(key)
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, _S_NUM_COLS - 1))
            total_row = len(self._order)
            self.dataChanged.emit(
                self.index(total_row, 0),
                self.index(total_row + 2, _S_NUM_COLS - 1))

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

    def set_hidden_fills(self, exec_ids) -> None:
        """Exclude *exec_ids* (fills hidden in the flat list view) from
        aggregation.  Driven by the panel from the flat proxy's hide set
        so a trade hidden in the list also vanishes from its summary
        aggregate."""
        new = {e for e in (exec_ids or ()) if e}
        if new == self._hidden_fills:
            return
        self._hidden_fills = new
        self._rebuild_from_fills()

    def set_last_price(self, symbol: str, price: float) -> None:
        """Feed a live last-trade price (root or local symbol).  The P&L
        footer row marks unclosed positions to it; per-symbol rows and
        the Realized P&L row stay cash/paired-based."""
        sym = (symbol or "").upper()
        if not sym or not price or price <= 0:
            return
        if self._last_prices.get(sym) == price:
            return
        self._last_prices[sym] = price
        if not self._order:
            return
        # Repaint the footer only when a visible aggregate is actually
        # open on this symbol — closed round-trips don't re-price.
        for a in self._visible_aggs():
            if a.get("buys_qty", 0.0) == a.get("sells_qty", 0.0):
                continue
            if sym in (a.get("root_symbol", ""),
                       (a.get("symbol", "") or "").upper()):
                # P&L and Unrealized P&L both mark to last price.
                total_row = len(self._order)
                self.dataChanged.emit(
                    self.index(total_row, 0),
                    self.index(total_row + 2, _S_NUM_COLS - 1))
                return

    def clear(self) -> None:
        if not self._order and not self._fills:
            return
        self.beginResetModel()
        self._agg.clear()
        self._order.clear()
        self._fills.clear()
        self._hidden_symbols.clear()
        self._hidden_fills.clear()
        self._matched_keys.clear()
        self._latest_fill_date = None
        self._anchor_date = None
        self.endResetModel()

    # ── Hide / restore (user-driven, temporary) ────────────────────────────

    def hide_symbol(self, key: tuple[str, str]) -> None:
        """Temporarily remove *key*'s row from the view and recalculate
        the totals footer without it.  The aggregate stays in ``_agg``
        so ``restore_hidden`` can bring it back; the hide is cleared on
        any filter-driven rebuild."""
        if not key or key in self._hidden_symbols:
            return
        if key not in self._order:
            return
        row = self._order.index(key)
        self.beginRemoveRows(QModelIndex(), row, row)
        self._order.pop(row)
        self._hidden_symbols.add(key)
        self.endRemoveRows()
        # The footer rows shifted up by one and their values changed.
        if self._order:
            total_row = len(self._order)
            self.dataChanged.emit(
                self.index(total_row, 0),
                self.index(total_row + 2, _S_NUM_COLS - 1))

    def restore_hidden(self) -> None:
        """Un-hide every user-hidden row.  Order after restore is not
        guaranteed to match the original — restored keys land at the
        tail of ``_order``."""
        if not self._hidden_symbols:
            return
        self.beginResetModel()
        for k in list(self._hidden_symbols):
            if k in self._agg and k not in self._order:
                self._order.append(k)
        self._hidden_symbols.clear()
        self.endResetModel()

    def hidden_count(self) -> int:
        return len(self._hidden_symbols)

    def visible_fill_count(self) -> int:
        """Total number of fills folded into the currently-visible
        aggregates (user-hidden symbols excluded).  Mirrors the flat
        view's trade count so hiding a summary row drops the counter in
        the control bar too."""
        return sum(
            int(self._agg[s].get("fill_count", 0))
            for s in self._order if s in self._agg
        )

    # ── Match buy & sell (user-driven, per row) ─────────────────────────────

    def can_match(self, key: tuple[str, str]) -> bool:
        """True when *key* has both a buy and a sell leg of unequal size,
        so trimming the larger to the smaller is meaningful."""
        agg = self._agg.get(key)
        if agg is None:
            return False
        b = agg.get("buys_qty", 0.0)
        s = agg.get("sells_qty", 0.0)
        return b > 0 and s > 0 and b != s

    def is_matched(self, key: tuple[str, str]) -> bool:
        return key in self._matched_keys

    def match_symbol(self, key: tuple[str, str]) -> None:
        """Trim *key* to its matched round-trip and repaint the row +
        totals footer.  No-op if the legs already balance or one side is
        empty."""
        if not key or key in self._matched_keys or not self.can_match(key):
            return
        self._matched_keys.add(key)
        self._emit_row_and_total(key)

    def unmatch_symbol(self, key: tuple[str, str]) -> None:
        """Restore the full (untrimmed) buy/sell legs for *key*."""
        if key not in self._matched_keys:
            return
        self._matched_keys.discard(key)
        self._emit_row_and_total(key)

    def _emit_row_and_total(self, key: tuple[str, str]) -> None:
        """Repaint *key*'s visible row and both footer rows."""
        if key not in self._order:
            return
        row = self._order.index(key)
        self.dataChanged.emit(
            self.index(row, 0), self.index(row, _S_NUM_COLS - 1))
        total_row = len(self._order)
        self.dataChanged.emit(
            self.index(total_row, 0),
            self.index(total_row + 2, _S_NUM_COLS - 1))

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
        if self._hidden_fills:
            ek = fill.get("exec_id", "")
            if ek and ek in self._hidden_fills:
                return False
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
            key = self._agg_key(fill)
            if key not in self._agg:
                self._agg[key] = self._empty_agg(fill)
                self._order.append(key)
            self._add_to_agg(key, fill)
        self.endResetModel()

    # ── Internal aggregation helpers ────────────────────────────────────────

    @staticmethod
    def _empty_agg(fill: dict) -> dict:
        return {
            "symbol": TradesSummaryModel._fill_display_symbol(fill),
            # Root symbol ("MNQ", "NVDA") — live price feeds emit the
            # root, while the display symbol may be the local ("MNQU6").
            "root_symbol": (fill.get("symbol", "") or "").upper(),
            "sec_type": fill.get("sec_type", "") or "",
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

    def _add_to_agg(self, key: tuple[str, str], fill: dict) -> None:
        agg = self._agg[key]
        # Upgrade the display label only when a later fill carries a
        # populated localSymbol that the initial fill lacked — this keeps
        # the row's label in sync with orders/positions without ever
        # downgrading a localSymbol back to a bare root.
        local = fill.get("local_symbol", "") or ""
        if local and agg.get("symbol", "") != local:
            agg["symbol"] = local
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

    def _subtract_from_agg(self, key: tuple[str, str], fill: dict) -> None:
        agg = self._agg.get(key)
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

    def _drop_symbol_row(self, key: tuple[str, str]) -> None:
        # Hidden rows aren't in ``_order`` but still have a stored
        # aggregate — drop from internal state only; no row signals
        # needed because the view isn't showing them.
        self._matched_keys.discard(key)
        if key in self._hidden_symbols:
            self._hidden_symbols.discard(key)
            self._agg.pop(key, None)
            return
        if key not in self._order:
            return
        row = self._order.index(key)
        if len(self._order) == 1:
            # Dropping the last aggregate also hides the three footer
            # rows (P&L + Realized + Unrealized) so the view goes back
            # to empty — remove all four rows in one transaction.
            self.beginRemoveRows(QModelIndex(), 0, 3)
        else:
            self.beginRemoveRows(QModelIndex(), row, row)
        self._order.pop(row)
        self._agg.pop(key, None)
        self.endRemoveRows()
