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
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from src.ui.theme import Theme
from src.ui.widgets.trades_filter_proxy import today_et, trading_days_cutoff
from src.utils.helpers import format_option_symbol

from .table_theme import CELL_FONT_BOLD, IsFooterRowRole, header_alignment

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


def portfolio_mark_keys(pos: dict) -> tuple[str, ...]:
    """Price-cache keys a portfolio row's ``market_price`` should refresh.

    ``_last_price_for`` resolves a summary group through several keys —
    root symbol first (written by the L1 tick feeds), then the display
    symbol.  A mark fed under only one of them can be shadowed by a
    stale entry under another (an L1 tick that landed once before the
    market-data stream died leaves its root price in the cache
    forever), so the portfolio feed refreshes every key the lookup may
    hit:

    * stocks/futures — the local symbol (``MNQU6``) **and** the root
      (``MNQ``);
    * options — the raw OCC local symbol and its ``format_option_symbol``
      display form (group meta uses the formatted form, per-row
      aggregates the raw one), but **never** the root: the root feed
      carries the underlying's price, which must not mark a premium.
    """
    local = pos.get("local_symbol", "") or pos.get("symbol", "") or ""
    if not local:
        return ()
    if (pos.get("sec_type") or "").upper() in ("OPT", "FOP"):
        keys = {local, format_option_symbol(local)}
    else:
        keys = {local, pos.get("symbol", "") or ""}
    return tuple(k for k in keys if k)

# ── Column indices ────────────────────────────────────────────────────────────
S_COL_SYMBOL        = 0
S_COL_TYPE          = 1
S_COL_BUYS          = 2
S_COL_SELLS         = 3
S_COL_NET           = 4
S_COL_AVG_BUY       = 5
S_COL_AVG_SELL      = 6
S_COL_TRANS_TOTAL   = 7
S_COL_PAIRED_PNL    = 8    # R.P&L — FIFO-paired (realized) portion
S_COL_UNPAIRED_PNL  = 9    # U.P&L — open remainder, marked to last
S_COL_NET_TOTAL     = 10   # P&L — R.P&L + U.P&L
S_COL_COMMISSION    = 11
S_COL_NET_INCL_COMM = 12
S_COL_REALIZED      = 13
S_COL_EXCHANGES     = 14

S_HEADERS: list[str] = [
    "Symbol", "Type", "Buys", "Sells", "Net",
    "Avg buy price", "Avg sell price", "Trans total",
    "R.P&L", "U.P&L", "P&L", "Comm", "Net P&L", "Rlz P&L", "Exchanges",
]

# Column key used by the preferences dialog / column visibility state
S_COL_KEYS: list[str] = [
    "symbol", "sec_type", "buys", "sells", "net", "avg_buy", "avg_sell",
    "trans_total", "paired_pnl", "unpaired_pnl", "net_total",
    "commission", "net_incl_comm", "realized_pnl", "exchanges",
]

# Fill-level fields the search box matches against before aggregation.
# Mirrors the flat view's key set so typing ``U123`` (an account) or
# ``NASDAQ`` (an exchange) works the same way in both views — the
# summary just folds the matching subset into symbol aggregates.
_FILL_SEARCH_KEYS: tuple[str, ...] = (
    "symbol", "local_symbol", "side", "account", "account_id",
    "sec_type", "exchange", "order_ref",
)

_S_NUM_COLS = len(S_HEADERS)

# Components of a FIFO walk over one contract's fills (see
# ``TradesSummaryModel._walk_account``).  Every value is per-point —
# the contract multiplier is applied by the callers that turn these
# into dollars.
_WALK_KEYS: tuple[str, ...] = (
    "paired_qty", "paired_buys_notional", "paired_sells_notional",
    "paired_comm", "closing_realized", "closing_comm",
    "open_buys_qty", "open_buys_notional",
    "open_sells_qty", "open_sells_notional",
)


def _empty_walk() -> dict[str, float]:
    return {k: 0.0 for k in _WALK_KEYS}


_S_RIGHT_ALIGN_COLS = {
    S_COL_BUYS, S_COL_SELLS, S_COL_NET,
    S_COL_AVG_BUY, S_COL_AVG_SELL, S_COL_TRANS_TOTAL,
    S_COL_PAIRED_PNL, S_COL_UNPAIRED_PNL,
    S_COL_NET_TOTAL, S_COL_COMMISSION, S_COL_NET_INCL_COMM,
    S_COL_REALIZED,
}

# Theme tokens (keep in sync with trades_model)
CLR_BUY    = Theme.POS_GAIN
CLR_SELL   = Theme.POS_LOSS
TEXT       = Theme.POS_TEXT
TEXT_DIM   = Theme.POS_TEXT_MUTED
TEXT_BOLD  = Theme.POS_TEXT
# Subtle tint for the footer/total row so it reads as a separate
# band from the per-symbol rows above without shouting.  Same band as
# the Positions account headers and Summary totals.
BG_TOTAL   = Theme.POS_BG_ACCT_BAND


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
        # ``_anchor_date`` is strictly today_ET and the lookback counts
        # trading days (mirrors the rule in TradesFilterProxy so the two
        # views stay in sync).  Default 1 = "Today" to match
        # TradesFilterProxy's default so a fresh app shows the same
        # window in both flat and summary views.
        self._days_back: int = 1
        self._anchor_date: date = today_et()
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
        # Checked accounts from the Account List panel (raw IB ids,
        # matched against each fill's ``account_id``).  Aggregates span
        # accounts so this must gate fills BEFORE aggregation — a proxy
        # over the aggregate rows couldn't split a mixed-account bucket.
        # Empty set = show all, mirroring the positions view.
        self._selected_accounts: set[str] = set()
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

        # Shared cell fonts — same family, weight and size as every other
        # table in the two tabs (family + weight only; the size comes from
        # the table's stylesheet).
        self._symbol_font = CELL_FONT_BOLD
        # Bold font used across every cell of the totals/footer row.
        self._total_font = CELL_FONT_BOLD

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
        # One extra row at the bottom is the "P&L" totals footer (its
        # R.P&L / U.P&L columns carry the realized/unrealized split).
        # Hidden entirely when the model has no aggregates so an empty
        # view doesn't display a stray footer band.
        return n + 1 if n else 0

    def _is_total_row(self, row: int) -> bool:
        return bool(self._order) and row == len(self._order)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else _S_NUM_COLS

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation != Qt.Orientation.Horizontal \
                or not 0 <= section < _S_NUM_COLS:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return S_HEADERS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            # Captions sit over their values.
            return header_alignment(section, _S_RIGHT_ALIGN_COLS)
        return None

    def data(self, index: QModelIndex,
             role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()

        # Available on every row, footer included — the row delegate asks
        # each row whether it is the totals band.
        if role == IsFooterRowRole:
            return self._is_total_row(row)

        if self._is_total_row(row):
            return self._total_row_data(col, role)

        if row < 0 or row >= len(self._order):
            return None
        key = self._order[row]
        agg = self._effective_agg(key)
        if agg is None:
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            return self._cell_text(agg, col, key)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._cell_color(agg, col, key)
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
        walk = self._walk_for_key(key)
        if walk["paired_qty"] <= 0:
            return agg
        eff = dict(agg)
        eff["buys_qty"] = walk["paired_qty"]
        eff["sells_qty"] = walk["paired_qty"]
        eff["buys_notional"] = walk["paired_buys_notional"]
        eff["sells_notional"] = walk["paired_sells_notional"]
        return eff

    # ── Realized-P&L row rendering (paired round-trips only) ────────────────

    def _fills_for_key(self, key: tuple[str, str]) -> list[dict]:
        """This key's currently-counted fills in time order (earliest
        first) — same filter set the aggregates are built from."""
        out = [f for f in self._fills.values()
               if self._passes_filter(f) and self._agg_key(f) == key]
        out.sort(key=lambda f: f.get("time_sort", "") or "")
        return out

    def _walk_for_key(self, key: tuple[str, str]) -> dict[str, float]:
        """``_walk_fills`` over this row's currently-counted fills."""
        return self._walk_fills(self._fills_for_key(key))

    @classmethod
    def _walk_fills(cls, fills: list[dict]) -> dict[str, float]:
        """Split *fills* into closed round trips, closes of positions
        opened before the window, and the still-open remainder.

        Each ACCOUNT gets its own book.  A summary row aggregates one
        contract across every account, but P&L can't: pooling the
        accounts lets one account's close pair against another's opening
        fill, which both invents a round trip nobody traded and hides the
        second account's real position from U.P&L.  Walking per account
        keeps each book honest; the parts are summed afterwards because
        every number here is linear in the fills — including the mark
        (see ``_mark_open``), so a long in one account and a short in
        another still net correctly.
        """
        by_account: dict[str, list[dict]] = {}
        for f in fills:
            by_account.setdefault(f.get("account_id", "") or "", []).append(f)
        out = _empty_walk()
        for account_fills in by_account.values():
            account_fills.sort(key=lambda f: f.get("time_sort", "") or "")
            for k, v in cls._walk_account(account_fills).items():
                out[k] += v
        return out

    @staticmethod
    def _walk_account(fills: list[dict]) -> dict[str, float]:
        """Chronological FIFO walk over ONE account's fills in ONE
        contract (*fills* time-ordered, earliest first).

        Order matters: a fill can only close a lot that was already open
        when it printed.  Matching by leg totals instead — earliest buys
        against earliest sells, whatever their order — pairs a 10:00
        close of an overnight long with a 10:30 opening buy into a short
        round trip that never happened, and drops IB's real realized
        number on the floor.

        Three outcomes per fill, in that order:

        * it closes in-window lots on the other side → a **paired** round
          trip, valued gross from both legs' own fill prices,
        * whatever is left closed a position opened *outside* the window
          — recognised by a nonzero ``realized_pnl``, which IB's
          commissionReport stamps on every closing execution (computed
          against the account's average cost, a basis we can't see) →
          **closing**, taken from IB and prorated,
        * otherwise it opens a lot → the **open** remainder, marked to
          the last price by ``_mark_open``.

        IB's ``realized_pnl`` is already NET of both legs' commissions,
        so ``closing_comm`` carries back the part of that deduction this
        window can see (the closing fill's own commission).  Callers add
        it to keep every reported P&L gross, so the Comm column can
        subtract commission exactly once instead of twice.
        """
        out = _empty_walk()
        # Open in-window lots, FIFO.  Always all on the same side: a lot
        # only opens once nothing on the other side is left to close.
        # Each entry is [qty, price, side, commission per unit].
        lots: list[list] = []
        for f in fills:
            side = f.get("side", "")
            if side not in ("BOT", "SLD"):
                continue
            qty = float(f.get("shares", 0.0) or 0.0)
            if qty <= 0:
                continue
            price = float(f.get("price", 0.0) or 0.0)
            comm = float(f.get("commission") or 0.0)
            comm_unit = comm / qty
            rpnl = f.get("realized_pnl", None)

            left = qty
            while left > 0 and lots and lots[0][2] != side:
                lot = lots[0]
                use = min(lot[0], left)
                if side == "BOT":
                    out["paired_buys_notional"] += use * price
                    out["paired_sells_notional"] += use * lot[1]
                else:
                    out["paired_sells_notional"] += use * price
                    out["paired_buys_notional"] += use * lot[1]
                out["paired_qty"] += use
                out["paired_comm"] += use * (comm_unit + lot[3])
                lot[0] -= use
                left -= use
                if lot[0] <= 0:
                    lots.pop(0)

            if left <= 0:
                continue
            if rpnl:
                # Closing remainder — IB's number, prorated over the part
                # of the fill that found nothing to pair with.  Only a
                # NONZERO realized P&L marks a closing fill: live
                # commissionReports stamp realizedPNL = 0.0 on opening
                # executions too, so `is not None` would classify every
                # opening leg as a $0 close and pin U.P&L at zero.  (A
                # genuine scratch close at exactly $0.00 falls through to
                # marking — same as an opening leg, and harmlessly close
                # to truth.)
                frac = left / qty
                out["closing_realized"] += float(rpnl) * frac
                out["closing_comm"] += comm * frac
            else:
                # Opening leg — including a closing fill whose
                # commissionReport hasn't landed yet.  It marks to last
                # until the report arrives and the re-emitted fill sends
                # the walk round again: self-healing, never
                # double-counted.
                lots.append([left, price, side, comm_unit])

        for qty, price, side, _comm_unit in lots:
            if side == "BOT":
                out["open_buys_qty"] += qty
                out["open_buys_notional"] += qty * price
            else:
                out["open_sells_qty"] += qty
                out["open_sells_notional"] += qty * price
        return out

    # ── P&L built on the walk ───────────────────────────────────────────────

    @staticmethod
    def _paired_net(walk: dict[str, float], multiplier: float) -> float:
        """Gross cash flow of the closed round trips, multiplier-scaled
        so futures (NQ × 20, MNQ × 2, …) report in dollars."""
        return ((walk["paired_sells_notional"] - walk["paired_buys_notional"])
                * multiplier)

    def _mark_open(self, walk: dict[str, float], agg: dict) -> float:
        """The walk's open remainder marked to the last price — a
        net-long remainder valued as if sold, a net-short as if bought
        back.  Falls back to the newest fill price when no live quote has
        been seen: without a mark, an all-buys position would report its
        entire cost as unrealized loss."""
        mult = agg.get("multiplier", 1.0)
        cash = (walk["open_sells_notional"]
                - walk["open_buys_notional"]) * mult
        open_net = walk["open_buys_qty"] - walk["open_sells_qty"]
        if open_net == 0:
            return cash
        last = self._last_price_for(agg)
        if last is None:
            last = agg.get("last_fill_price", 0.0)
        if not last:
            return cash
        return cash + open_net * last * mult

    def _key_multiplier(self, key: tuple[str, str]) -> float:
        mult = (self._agg.get(key) or {}).get("multiplier", 1.0)
        return float(mult) if mult else 1.0

    def _realized_total(self, key: tuple[str, str]) -> float:
        """R.P&L for one row: the in-window round trips plus IB's realized
        P&L on fills that closed a position opened before the window —
        GROSS, with the commission IB folded into its realized number
        added back so ``P&L − Comm`` deducts commission exactly once.  A
        matched row shows the trimmed round trip only — that's the point
        of matching."""
        walk = self._walk_for_key(key)
        realized = self._paired_net(walk, self._key_multiplier(key))
        if self.is_matched(key):
            return realized
        return realized + walk["closing_realized"] + walk["closing_comm"]

    def _unrealized_total(self, key: tuple[str, str], agg: dict) -> float:
        """U.P&L for one row: only the *opening* remainder, marked to the
        last price."""
        if self.is_matched(key):
            return 0.0
        return self._mark_open(self._walk_for_key(key), agg)

    def _row_pnl_total(self, key: tuple[str, str], agg: dict) -> float:
        """P&L column: R.P&L + U.P&L, so the three always reconcile."""
        return self._realized_total(key) + self._unrealized_total(key, agg)

    def _visible_key_aggs(self) -> list[tuple[tuple[str, str], dict]]:
        out = []
        for key in self._order:
            agg = self._effective_agg(key)
            if agg is not None:
                out.append((key, agg))
        return out

    # ── Status-bar aggregate (today's session, by account set) ──────────────

    @staticmethod
    def _group_multiplier(fills: list[dict]) -> float:
        """Contract multiplier for a fill group — first fill carrying
        one, with the OPT/FOP = 100 default (same rules as
        ``_add_to_agg``)."""
        mult = 1.0
        for f in fills:
            m = f.get("multiplier")
            if m is None:
                continue
            try:
                mf = float(m)
            except (TypeError, ValueError):
                continue
            if mf > 0:
                mult = mf
                break
        if mult == 1.0 and any((f.get("sec_type") or "").upper()
                               in ("OPT", "FOP") for f in fills):
            mult = 100.0
        return mult

    def pnl_totals_today(self, accounts=None
                         ) -> tuple[float, float, float, float]:
        """``pnl_totals`` over today's ET session only."""
        return self.pnl_totals(accounts, days_back=1)

    def pnl_totals(self, accounts=None, days_back: int = 1,
                   threshold: datetime | None = None,
                   ) -> tuple[float, float, float, float]:
        """(R.P&L, U.P&L, P&L, commission) for *accounts* (raw IB ids via
        ``account_id``; empty/None = every account) over a time frame:
        *threshold* keeps fills at or after that ET moment (the control
        bar's Morning/Noon/Evening presets) and overrides the day
        window; otherwise *days_back* counts trading days back from
        today ET (1 = today, 2 = today + the previous trading day,
        0/negative = everything).

        Deliberately independent of the view's search text and account
        filter — this feeds the always-on status-bar aggregate.  Fills
        the user has HIDDEN are excluded, though: a hide is session-level
        ("stop counting this transaction") and drops the fill from every
        calculation until Restore hidden.  Same math as the summary rows
        (``_walk_fills``): per-account round trips, plus IB's realized on
        fills that closed a position opened before the window, plus the
        opening remainder marked to the last cached price (newest fill
        price when no quote is cached).  The commission element sums the
        in-frame fills' reported commissions (TWS only sends a
        commissionReport for a subset of fills, so this is "commissions
        reported so far" — same caveat as the summary Comm column); the
        R/U/P&L elements stay gross so they keep matching the summary
        rows, and net-of-commission = P&L − commission.
        """
        sel = {a for a in (accounts or ()) if a}
        anchor = self._compute_anchor()

        def in_frame(f: dict) -> bool:
            if threshold is not None:
                ts = f.get("time_sort", "")
                if not ts:
                    return True
                try:
                    parsed = datetime.fromisoformat(ts)
                except ValueError:
                    return True
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=_ET)
                return parsed >= threshold
            if days_back <= 0:
                return True
            local = self._fill_local_date(f)
            if local is None:
                return True
            return local >= trading_days_cutoff(anchor, days_back)

        groups: dict[tuple[str, str], list[dict]] = {}
        comm_total = 0.0
        for f in self._fills.values():
            if self._hidden_fills and (
                    f.get("exec_id", "") in self._hidden_fills):
                continue
            if sel:
                aid = f.get("account_id", "")
                if aid and aid not in sel:
                    continue
            if not in_frame(f):
                continue
            groups.setdefault(self._agg_key(f), []).append(f)
            try:
                comm_total += float(f.get("commission") or 0.0)
            except (TypeError, ValueError):
                pass

        r_total = u_total = 0.0
        for fills in groups.values():
            fills.sort(key=lambda f: f.get("time_sort", "") or "")
            newest = fills[-1]
            meta = {
                "symbol": self._fill_display_symbol(newest),
                "root_symbol": (newest.get("symbol", "") or "").upper(),
                "sec_type": newest.get("sec_type", "") or "",
                "multiplier": self._group_multiplier(fills),
                # No-quote mark fallback, same rule as the summary rows'
                # aggregate: the newest fill's own price.
                "last_fill_price": float(newest.get("price", 0.0) or 0.0),
            }
            walk = self._walk_fills(fills)
            r_total += (self._paired_net(walk, meta["multiplier"])
                        + walk["closing_realized"] + walk["closing_comm"])
            u_total += self._mark_open(walk, meta)
        return r_total, u_total, r_total + u_total, comm_total

    def _total_value(self, col: int) -> float | None:
        """Footer number for the P&L-family columns, ``None`` for columns
        the footer leaves blank.  Summed per visible row from the same
        helpers the row cells use, so footer ≡ Σ rows by construction."""
        pairs = self._visible_key_aggs()
        if col == S_COL_PAIRED_PNL:
            return sum(self._realized_total(k) for k, _a in pairs)
        if col == S_COL_UNPAIRED_PNL:
            return sum(self._unrealized_total(k, a) for k, a in pairs)
        if col == S_COL_NET_TOTAL:
            return sum(self._row_pnl_total(k, a) for k, a in pairs)
        if col == S_COL_NET_INCL_COMM:
            return sum(self._row_pnl_total(k, a) - a.get("commission", 0.0)
                       for k, a in pairs)
        return None

    def _total_cell_text(self, col: int) -> str:
        if col == S_COL_SYMBOL:
            return "P&L"
        if col == S_COL_TRANS_TOTAL:
            return self._fmt_compact(
                sum(self._trans_total(a) for a in self._visible_aggs()))
        if col == S_COL_COMMISSION:
            s = sum(a.get("commission", 0.0) for a in self._visible_aggs())
            return f"{s:,.2f}" if s else ""
        v = self._total_value(col)
        return self._fmt_signed(v) if v is not None else ""

    def _total_cell_color(self, col: int) -> QColor | None:
        if col == S_COL_SYMBOL:
            return QColor(TEXT_BOLD)
        if col == S_COL_TRANS_TOTAL:
            return QColor(TEXT_BOLD)
        v = self._total_value(col)
        if v is not None:
            if v > 0:
                return QColor(CLR_BUY)
            if v < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
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

    def _paired_pnl(self, key: tuple[str, str] | None) -> float:
        """R.P&L for one row — the in-window round trips plus IB's
        realized P&L on closing fills (see ``_walk_account``)."""
        if key is None:
            return 0.0
        return self._realized_total(key)

    def _cell_text(self, agg: dict, col: int, key=None) -> str:
        if col == S_COL_SYMBOL:
            # agg["symbol"] stays the raw local symbol (price-cache key);
            # prettify OCC option symbols only at render time.
            return format_option_symbol(agg.get("symbol", ""))
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
        if col == S_COL_PAIRED_PNL:
            return self._fmt_signed(self._paired_pnl(key))
        if col == S_COL_UNPAIRED_PNL:
            return self._fmt_signed(
                self._unrealized_total(key, agg) if key else 0.0)
        if col == S_COL_NET_TOTAL:
            # P&L = R.P&L + U.P&L — paired + IB-realized closes + the
            # marked opening remainder, not the raw cash flow.
            return self._fmt_signed(
                self._row_pnl_total(key, agg) if key else 0.0)
        if col == S_COL_COMMISSION:
            c = agg.get("commission", 0.0)
            return f"{c:,.2f}" if c else ""
        if col == S_COL_NET_INCL_COMM:
            return self._fmt_signed(
                (self._row_pnl_total(key, agg) if key else 0.0)
                - agg.get("commission", 0.0))
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
        if (agg.get("sec_type") or "").upper() in ("OPT", "FOP"):
            # Only an exact local-symbol quote may mark an option — the
            # root-symbol feed carries the UNDERLYING's price, which
            # would mark a $0.86 contract at e.g. 192.00 × 100.  No
            # option quote in the cache → fall back to cash-based.
            p = self._last_prices.get((agg.get("symbol", "") or "").upper())
            return p if p else None
        for key in (agg.get("root_symbol", ""),
                    (agg.get("symbol", "") or "").upper()):
            if key:
                p = self._last_prices.get(key)
                if p:
                    return p
        return None

    @staticmethod
    def _trans_total(agg: dict) -> float:
        """Gross transaction volume — total buy notional + total sell
        notional, multiplier-scaled.  Unlike P&L this is always
        non-negative and indicates how much capital changed hands."""
        total = agg.get("buys_notional", 0.0) + agg.get("sells_notional", 0.0)
        return total * agg.get("multiplier", 1.0)

    def _cell_color(self, agg: dict, col: int, key=None) -> QColor | None:
        if col == S_COL_SYMBOL:
            return QColor(TEXT_BOLD)
        if col == S_COL_NET:
            net = agg.get("buys_qty", 0.0) - agg.get("sells_qty", 0.0)
            if net > 0:
                return QColor(CLR_BUY)
            if net < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        if col in (S_COL_PAIRED_PNL, S_COL_UNPAIRED_PNL,
                   S_COL_NET_TOTAL, S_COL_NET_INCL_COMM):
            if col == S_COL_PAIRED_PNL:
                v = self._paired_pnl(key)
            elif col == S_COL_UNPAIRED_PNL:
                v = self._unrealized_total(key, agg) if key else 0.0
            elif col == S_COL_NET_TOTAL:
                v = self._row_pnl_total(key, agg) if key else 0.0
            else:
                v = ((self._row_pnl_total(key, agg) if key else 0.0)
                     - agg.get("commission", 0.0))
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
        return format_option_symbol(
            fill.get("local_symbol", "")
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

        # Detect an anchor advance (the midnight ET rollover — a fresh
        # session's first fill arrives and "today" has moved forward).
        # A full rebuild is the simplest correct response — yesterday's
        # aggregates may need to drop out of a 1-day window and
        # rebuilding is O(n_fills).
        prev_anchor = self._anchor_date
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
                # First aggregate materialises the totals row too, so we
                # insert rows [0, 1] in a single transaction to keep
                # Qt's row-count bookkeeping consistent.
                self.beginInsertRows(QModelIndex(), 0, 1)
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
                self.index(total_row, _S_NUM_COLS - 1))

    def set_days_back(self, days: int) -> None:
        """Change the date window (1 = today, 2 = last 2 trading days,
        …).  0 or negative means "show every fill regardless of time"."""
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

    def set_selected_accounts(self, accounts) -> None:
        """Re-aggregate using only fills whose ``account_id`` is in
        *accounts* (raw IB ids).  Empty/None shows every account."""
        new = {a for a in (accounts or ()) if a}
        if new == self._selected_accounts:
            return
        self._selected_accounts = new
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
        # Repaint rows whose symbol has an open leg (their U.P&L / P&L
        # columns mark to last price) plus the footer bands.  Closed
        # round-trips don't re-price.
        hit = False
        for row, k in enumerate(self._order):
            a = self._effective_agg(k)
            if a is None \
                    or a.get("buys_qty", 0.0) == a.get("sells_qty", 0.0):
                continue
            if sym in (a.get("root_symbol", ""),
                       (a.get("symbol", "") or "").upper()):
                hit = True
                self.dataChanged.emit(
                    self.index(row, 0),
                    self.index(row, _S_NUM_COLS - 1))
        if hit:
            total_row = len(self._order)
            self.dataChanged.emit(
                self.index(total_row, 0),
                self.index(total_row, _S_NUM_COLS - 1))

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
        self._anchor_date = self._compute_anchor()
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
        # The totals footer shifted up by one and its values changed.
        if self._order:
            total_row = len(self._order)
            self.dataChanged.emit(
                self.index(total_row, 0),
                self.index(total_row, _S_NUM_COLS - 1))

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
            self.index(total_row, _S_NUM_COLS - 1))

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

    def _compute_anchor(self) -> date:
        """Rule mirrors ``TradesFilterProxy``: strict "Today" — the
        window is always anchored to the current ET date."""
        return today_et()

    def _passes_filter(self, fill: dict) -> bool:
        if self._hidden_fills:
            ek = fill.get("exec_id", "")
            if ek and ek in self._hidden_fills:
                return False
        if self._selected_accounts:
            acct = fill.get("account_id", "")
            # Fills without an account_id stay visible rather than being
            # silently dropped (mirrors TradesFilterProxy).
            if acct and acct not in self._selected_accounts:
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

        if self._days_back <= 0:
            return True
        local_date = self._fill_local_date(fill)
        if local_date is None:
            return True
        return local_date >= trading_days_cutoff(
            self._anchor_date, self._days_back)

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
            # Most recent fill price (by time_sort) — the mark-to-market
            # fallback for symbols with no live quote subscription, so an
            # open position never shows its full cost as unrealized loss.
            "last_fill_price": 0.0,
            "last_fill_sort": "",
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
        # Option fills occasionally arrive without contract.multiplier —
        # a standard equity option delivers 100 shares, so without this
        # a 0.86 premium would fold into P&L as $0.86 instead of $86.
        # Non-standard multipliers (minis, adjusted) always arrive
        # explicitly and take the branch above instead.
        if agg.get("multiplier", 1.0) == 1.0 \
                and (fill.get("sec_type") or "").upper() in ("OPT", "FOP"):
            agg["multiplier"] = 100.0
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
        # Track the newest fill's price as the no-quote mark fallback.
        # Fills can replay out of order (snapshot + live), so compare by
        # time_sort rather than arrival order.
        if price > 0:
            ts = fill.get("time_sort", "") or ""
            if ts >= (agg.get("last_fill_sort", "") or ""):
                agg["last_fill_price"] = price
                agg["last_fill_sort"] = ts

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
            # Dropping the last aggregate also hides the totals footer
            # so the view goes back to empty — remove both rows in one
            # transaction.
            self.beginRemoveRows(QModelIndex(), 0, 1)
        else:
            self.beginRemoveRows(QModelIndex(), row, row)
        self._order.pop(row)
        self._agg.pop(key, None)
        self.endRemoveRows()
