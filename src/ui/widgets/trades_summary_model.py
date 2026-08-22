"""
TradesSummaryModel – one row per symbol, aggregated across every fill.

Columns: Financial Instrument, Buys, Sells, Net, Avg Buy Price,
Avg Sell Price, Net Total, Commission, Net Incl. Comm, Realized P&L,
Exchange List.

The model owns the aggregate state.  ``add_fill`` either updates an
existing row (dedupe by execId) or folds a new fill into its symbol's
bucket and emits the right ``beginInsertRows`` / ``dataChanged`` pairs.

The table stacks two labeled sections, each closed by its own "TOTAL"
row: "TRADE LEVEL" (window-relative rows — what did this window's
trading make?  Its FIFO walk runs as one independent book PER SESSION
DAY, so a multi-day window is Σ of the daily walks and fills never pair
across days — see ``_walk_fills``) then "ACCOUNT LEVEL" (the same keys
with their COST-BASIS numbers: every closing fill contributes IB's avg-cost
``realized_pnl`` directly, so the section reconciles with the flat
view's Rlz P&L column and shows P&L against what was actually paid —
even when the close also had same-window buys).  Both sections are
built from the same aggregates and filters; only the walk differs.  The
cost-basis section is REALIZED-ONLY: its U.P&L / P&L cells stay blank
(a homemade mark next to IB's authoritative realized number would read
as IB's unrealized — the Positions tab carries the real one) and
Net P&L = R.P&L − Comm.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from src.ui.theme import Theme
from src.ui.widgets.trades_date_filter import TradeWindow, fill_dt, today_et
from src.utils.helpers import (
    format_option_symbol, format_option_symbol_two_line)

from .table_theme import (
    CELL_FONT_BOLD, IsFooterRowRole, SectionRankRole, SortValueRole,
    header_alignment)

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

# Labels on the bands that open the two sections and on their totals
# rows (each section closes with its own "TOTAL" line).
TRADE_SECTION_LABEL = "TRADE LEVEL"
ACCT_SECTION_LABEL = "ACCOUNT LEVEL"
DAY_TOTAL_LABEL = "TOTAL"
ACCT_TOTAL_LABEL = "TOTAL"

# Components of a FIFO walk over one contract's fills (see
# ``TradesSummaryModel._walk_account``).  Every value is per-point —
# the contract multiplier is applied by the callers that turn these
# into dollars.
_WALK_KEYS: tuple[str, ...] = (
    # Window-relative walk, run as ONE INDEPENDENT FIFO BOOK PER SESSION
    # DAY (see ``_walk_fills``) — feeds R.P&L / U.P&L / P&L.
    "paired_qty", "paired_buys_notional", "paired_sells_notional",
    "paired_comm",
    "open_buys_qty", "open_buys_notional",
    "open_sells_qty", "open_sells_notional",
    # ``ws_``-prefixed twins: the SAME walk run once over the whole
    # window (no day segmentation).  Only the "match buy & sell" trim
    # reads these — it has to pair the legs down to a flat Net, which a
    # day-segmented walk can't promise (see ``_effective_agg``).
    "ws_paired_qty", "ws_paired_buys_notional", "ws_paired_sells_notional",
    "ws_paired_comm",
    "ws_open_buys_qty", "ws_open_buys_notional",
    "ws_open_sells_qty", "ws_open_sells_notional",
    # ``cb_``-prefixed twins from the cost-basis walk
    # (``_walk_account_cost_basis``), which feeds the "Acct P&L" footer
    # row.  Same shapes, except ``cb_closing_realized``/``cb_closing_comm``
    # are IB dollar amounts (no multiplier).
    "cb_paired_qty", "cb_paired_buys_notional", "cb_paired_sells_notional",
    "cb_paired_comm", "cb_closing_realized", "cb_closing_comm",
    "cb_open_buys_qty", "cb_open_buys_notional",
    "cb_open_sells_qty", "cb_open_sells_notional",
)


# The eight components ONE walk over ONE fill list produces, unprefixed.
# ``_walk_fills`` runs the same walk in several framings and folds each
# result in under its own prefix ("" / "ws_" / "cb_"), so the per-walk
# helpers must emit bare keys — seeding them from ``_WALK_KEYS`` would
# re-prefix already-prefixed names.
_LEG_WALK_KEYS: tuple[str, ...] = (
    "paired_qty", "paired_buys_notional", "paired_sells_notional",
    "paired_comm",
    "open_buys_qty", "open_buys_notional",
    "open_sells_qty", "open_sells_notional",
)


def _empty_walk() -> dict[str, float]:
    return {k: 0.0 for k in _WALK_KEYS}


def _empty_leg_walk() -> dict[str, float]:
    return {k: 0.0 for k in _LEG_WALK_KEYS}


# Contracts whose trading day rolls at 18:00 ET (the CME/ICE electronic
# session opens for the NEXT trading date), so the P&L walk keeps an
# evening open and the following morning's close in ONE day book.
# Everything else (stocks, equity options) keeps the ET calendar date —
# an 18:00 rollover there would split extended-hours trading, which
# belongs to the day it printed on.
_SESSION_ROLLOVER_SEC_TYPES = frozenset({"FUT", "FOP", "CONTFUT"})


def session_day(dt: datetime | None, sec_type: str) -> date | None:
    """Trading-session date of *dt* — the day whose book this fill
    belongs to, which is what the P&L walk segments on.

    For futures-family contracts an 18:00 ET print opens the *next*
    trading date's session, so it rolls forward (and skips the weekend:
    Sunday 18:00 → Monday, and the Friday-evening edge lands on Monday
    rather than Saturday).  Every other sec_type returns the plain ET
    calendar date.

    NOTE this is deliberately NOT what the date-window *filter* uses —
    that stays on the ET calendar date (``_ts_date``) so the summary
    admits exactly the fills the flat Trades list does.  The session day
    only decides which fills share a FIFO book once they are in.
    """
    if dt is None:
        return None
    et = dt.astimezone(_ET)
    d = et.date()
    if (et.hour >= 18
            and (sec_type or "").upper() in _SESSION_ROLLOVER_SEC_TYPES):
        d += timedelta(days=1)
        while d.weekday() >= 5:            # Sat/Sun → the next Monday
            d += timedelta(days=1)
    return d


def _compute_agg_key(fill: dict) -> tuple[str, str]:
    """Fresh aggregation key for *fill* — see ``_agg_key`` for the rules."""
    con_id = int(fill.get("con_id", 0) or 0)
    if con_id > 0:
        return (f"cid:{con_id}", fill.get("sec_type", "") or "")
    return (fill.get("symbol", "") or "—", fill.get("sec_type", "") or "")


def stamp_fill(fill: dict) -> None:
    """Precompute per-fill derived values ONCE, at store time.

    The P&L walks and filters used to re-run ``datetime.fromisoformat``
    (plus a tz conversion) and rebuild the agg-key string for every fill
    on every access — the dominant per-fill cost once a session holds
    thousands of fills.  Stamps live on the fill dict itself (shared
    with the flat ``TradesModel``, so both views benefit); underscore
    keys are invisible to the column renderers, which read named keys.
    A ``None`` ``_ts_dt`` covers both "no timestamp" and "unparseable"
    — the filters treat the two identically (the fill passes).

    Two date stamps, on purpose: ``_ts_date`` is the ET calendar date
    the window filter admits fills by, ``_ts_session`` the trading-session
    date the P&L walk segments FIFO books by (see ``session_day``).
    """
    ts = fill.get("time_sort", "")
    dt = None
    if ts:
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            dt = None
        else:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_ET)
    fill["_ts_dt"] = dt
    fill["_ts_date"] = dt.astimezone(_ET).date() if dt is not None else None
    fill["_ts_session"] = session_day(dt, fill.get("sec_type", "") or "")
    fill["_agg_key"] = _compute_agg_key(fill)


# Roles the P&L-family dataChanged emits actually change.  Naming them
# (instead of the empty "everything changed" vector) lets the sort/filter
# proxy skip work for roles it doesn't watch; SortValueRole stays listed
# so an active P&L-column sort keeps live-reordering.
_PNL_ROLES = [
    Qt.ItemDataRole.DisplayRole,
    Qt.ItemDataRole.ForegroundRole,
    SortValueRole,
]

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
# Section-header label color — same as the column-header captions.
CLR_SECTION = Theme.POS_LABEL


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
        # Keys of the cost-basis section's per-symbol rows, mirroring
        # ``_order``.  Kept as its own list so structural changes can be
        # announced to Qt as TWO transactions (window row, then cb row —
        # the two rows are never adjacent) with the row→key mapping
        # internally consistent inside each begin/end pair.
        self._order_cb: list[tuple[str, str]] = []
        # execId → raw fill dict — keeps the last flattened fill per execId so
        # a re-snapshot that re-emits the same fill doesn't double-count it
        self._fills: dict[str, dict] = {}
        # The date dropdown's selection, as one window (see
        # ``trades_date_filter``).  Replaces the old ``days_back`` +
        # ``time_threshold`` pair: those could disagree and every caller
        # had to know which one won.  Defaults to Today, matching
        # TradesFilterProxy, so a fresh app shows the same frame in the
        # flat and summary views.  ``_anchor_date`` is strictly today_ET.
        self._anchor_date: date = today_et()
        self._window: TradeWindow = TradeWindow.last_days(
            1, self._anchor_date)
        # Set when the search box parses as a datetime.  Overrides the
        # combo window unless that window is a session preset — same
        # precedence the flat proxy applies (``effectiveWindow``).
        self._search_window: TradeWindow | None = None
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
        # Connection labels currently disconnected.  Their fills stay in
        # ``_fills`` but are excluded from every aggregate, so a
        # reconnect restores the numbers — see ``offline_connections``.
        self._offline_labels: set[str] = set()
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

        # ── P&L caches ────────────────────────────────────────────────
        # A key's FIFO walk depends only on (its fills, the filter state,
        # the hidden set) — never on prices, which enter through the O(1)
        # ``_mark_open`` term.  So walks cache per key and price ticks
        # touch no fills at all.  Without this, every P&L cell repaint
        # re-scanned and re-walked EVERY stored fill (the footer row cost
        # 18·rows·fills fill-visits per paint — ~0.9 s at 5 000 fills —
        # on every tick and every fill: the app's slow-over-a-session
        # signature).
        # key → {exec_id: fill}; mirrors ``_fills``, not the visible set.
        self._fills_by_key: dict[tuple[str, str], dict[str, dict]] = {}
        # key → walk dict from ``_walk_fills``.  Treat cached walks as
        # immutable.  Invalidation: popped per-key on ``add_fill`` /
        # ``_drop_symbol_row``; cleared wholesale by
        # ``_rebuild_from_fills`` (every filter setter routes there) and
        # ``clear``.  hide/match/restore and prices never invalidate.
        self._walk_cache: dict[tuple[str, str], dict[str, float]] = {}
        # Bumped whenever the stored fill set or the hidden set changes —
        # the price-independent half of ``pnl_totals`` memoizes on it.
        self._fills_rev: int = 0
        self._pnl_memo: tuple | None = None

        # Shared cell fonts — same family, weight and size as every other
        # table in the two tabs (family + weight only; the size comes from
        # the table's stylesheet).
        self._symbol_font = CELL_FONT_BOLD
        # Bold font used across every cell of the totals/footer row.
        self._total_font = CELL_FONT_BOLD

    # ── Proxy helpers ───────────────────────────────────────────────────────

    def row_data(self, row: int) -> dict | None:
        # Cost-basis rows answer their key's aggregate too, so the sort
        # proxy's substring search filters both sections identically and
        # the panel's hide/match flows work from either section.  Band
        # rows (headers, totals, spacer) stay None.
        key = self._win_key_at(row)
        if key is None:
            key = self._cb_key_at(row)
        if key is not None:
            return self._agg.get(key)
        return None

    def row_key(self, row: int) -> tuple[str, str] | None:
        # Aggregation key for ``hide_symbol`` lookups.  The key uses
        # ``cid:<con_id>`` (see ``_agg_key``) and is *not* derivable from
        # the agg dict's display fields, so callers that want to hide a
        # row must round-trip through this helper.
        key = self._win_key_at(row)
        if key is not None:
            return key
        return self._cb_key_at(row)

    def column_keys(self) -> list[str]:
        return list(S_COL_KEYS)

    def column_headers(self) -> list[str]:
        return list(S_HEADERS)

    # ── Qt model plumbing ───────────────────────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        n = len(self._order)
        m = len(self._order_cb)
        # Layout: the "TRADE LEVEL" header band · n window-relative
        # symbol rows · that section's "TOTAL" row · the "ACCOUNT LEVEL"
        # header band · m cost-basis symbol rows · that section's
        # "TOTAL" row.  Hidden entirely when the model has no aggregates
        # so an empty view doesn't display stray bands.
        return n + m + 4 if (n or m) else 0

    def _is_trade_header(self, row: int) -> bool:
        """The band that opens the window-relative section — row 0."""
        return bool(self._order) and row == 0

    def _win_key_at(self, row: int) -> tuple[str, str] | None:
        """Key of the window-relative symbol row at *row* (rows sit
        below the trade-level header), ``None`` outside that section."""
        i = row - 1
        if 0 <= i < len(self._order):
            return self._order[i]
        return None

    def _is_total_row(self, row: int) -> bool:
        """The "TOTAL" row closing the window section."""
        return bool(self._order) and row == len(self._order) + 1

    def _is_acct_header(self, row: int) -> bool:
        """The band that opens the cost-basis section."""
        return bool(self._order) and row == len(self._order) + 2

    def _is_cost_row(self, row: int) -> bool:
        """The cost-basis section's "TOTAL" row — the table's last."""
        return bool(self._order) \
            and row == len(self._order) + 3 + len(self._order_cb)

    def _cb_key_at(self, row: int) -> tuple[str, str] | None:
        """Key of the cost-basis symbol row at *row*, ``None`` when *row*
        isn't in the cost-basis section."""
        j = row - len(self._order) - 3
        if 0 <= j < len(self._order_cb):
            return self._order_cb[j]
        return None

    def _section_rank(self, row: int) -> int:
        """Ordering band for the sort proxy (see ``SectionRankRole``).
        The trade-level header shares rank 0 with rankless models' rows
        by construction — it is the only summary row there, so it never
        competes within its rank."""
        if self._is_trade_header(row):
            return 0
        if self._is_total_row(row):
            return 2
        if self._is_acct_header(row):
            return 3
        if self._is_cost_row(row):
            return 5
        if self._cb_key_at(row) is not None:
            return 4
        return 1

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

        # Available on every row — the row delegate asks each row whether
        # it is a band (the section headers and the totals rows; symbol
        # rows in both sections are ordinary data rows), and the sort
        # proxy asks each row for its section rank.
        if role == IsFooterRowRole:
            return (self._is_trade_header(row) or self._is_total_row(row)
                    or self._is_acct_header(row) or self._is_cost_row(row))
        if role == SectionRankRole:
            return self._section_rank(row)

        if self._is_trade_header(row):
            return self._trade_header_data(col, role)
        if self._is_total_row(row):
            return self._total_row_data(col, role)
        if self._is_acct_header(row):
            return self._acct_header_data(col, role)
        if self._is_cost_row(row):
            return self._cost_row_data(col, role)
        if self._cb_key_at(row) is not None:
            return self._cb_row_data(row, col, role)

        key = self._win_key_at(row)
        if key is None:
            return None
        agg = self._effective_agg(key)
        if agg is None:
            return None

        if role == SortValueRole:
            return self._sort_value(agg, col, key)
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
        """Render a cell on the synthetic "TOTAL" row that sums the
        window section.  Plain row background (bold text and the
        no-hover band role set it apart); only the P&L-family columns
        carry a number — every other column stays blank so the row reads
        as a focused summary rather than a second data row."""
        if role == Qt.ItemDataRole.DisplayRole:
            return self._total_cell_text(col)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._total_cell_color(col)
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
        returns its raw aggregate unchanged.

        Reads the ``ws_`` (whole-window, unsegmented) walk on purpose:
        "match buy & sell" promises a flat Net, and the day-segmented
        walk pairs only within each session day, so it would leave the
        legs unequal whenever a position was carried between days."""
        agg = self._agg.get(key)
        if agg is None or key not in self._matched_keys:
            return agg
        b_q = agg.get("buys_qty", 0.0)
        s_q = agg.get("sells_qty", 0.0)
        if b_q == s_q:
            return agg
        walk = self._walk_for_key(key)
        if walk["ws_paired_qty"] <= 0:
            return agg
        eff = dict(agg)
        eff["buys_qty"] = walk["ws_paired_qty"]
        eff["sells_qty"] = walk["ws_paired_qty"]
        eff["buys_notional"] = walk["ws_paired_buys_notional"]
        eff["sells_notional"] = walk["ws_paired_sells_notional"]
        return eff

    # ── Realized-P&L row rendering (paired round-trips only) ────────────────

    def _fills_for_key(self, key: tuple[str, str]) -> list[dict]:
        """This key's currently-counted fills in time order (earliest
        first) — same filter set the aggregates are built from."""
        bucket = self._fills_by_key.get(key)
        if not bucket:
            return []
        out = [f for f in bucket.values() if self._passes_filter(f)]
        out.sort(key=lambda f: f.get("time_sort", "") or "")
        return out

    def _walk_for_key(self, key: tuple[str, str]) -> dict[str, float]:
        """``_walk_fills`` over this row's currently-counted fills.
        Cached per key (see ``_walk_cache``) — do not mutate the result."""
        walk = self._walk_cache.get(key)
        if walk is None:
            walk = self._walk_fills(self._fills_for_key(key))
            self._walk_cache[key] = walk
        return walk

    @classmethod
    def _walk_fills(cls, fills: list[dict]) -> dict[str, float]:
        """Split *fills* into closed round trips and the still-open
        remainder — window-relative, nothing outside the fills given.

        Each ACCOUNT gets its own book.  A summary row aggregates one
        contract across every account, but P&L can't: pooling the
        accounts lets one account's close pair against another's opening
        fill, which both invents a round trip nobody traded and hides the
        second account's real position from U.P&L.  Walking per account
        keeps each book honest; the parts are summed afterwards because
        every number here is linear in the fills — including the mark
        (see ``_mark_open``), so a long in one account and a short in
        another still net correctly.

        Within an account the window-relative walk is segmented further:
        one INDEPENDENT FIFO BOOK PER SESSION DAY (``session_day``).  A
        multi-day window ("Last 3 Days") is therefore Σ of each day's
        own FIFO, not one book spanning the window — Monday's fills can
        no longer pair with Wednesday's.  That kills the cross-day
        phantom round trip (a Monday close of a position opened before
        the window used to open a window-short that Wednesday's genuine
        new buy would "close"), and makes R.P&L mean the same thing at
        every window width: what this day's trading closed.

        The trade-off is the mirror case — a position genuinely opened
        Monday and closed Wednesday is not a day trade, so its two legs
        land in different books and its profit reports as U.P&L rather
        than R.P&L.  The **P&L total is unaffected**: R + U depends only
        on the total notionals and the net open quantity, never on how
        the fills pair (``_paired_net`` + ``_mark_open`` telescope), so
        segmentation only moves value between the R and U columns.
        Marking that carried leg to its own day's settlement would put
        it back in R, but IB gives us no per-day settlement price here.

        Two walks are NOT segmented, deliberately:

        * the cost-basis walk (``cb_``) takes IB's avg-cost realized
          whole on every closing fill, a basis that is already
          day-independent and reconciles with the flat view's Rlz P&L
          column — segmenting it could only damage its scratch-close
          fallback,
        * the whole-window twin (``ws_``), which exists solely so the
          "match buy & sell" trim can still pair down to a flat Net.
        """
        by_account: dict[str, list[dict]] = {}
        for f in fills:
            by_account.setdefault(f.get("account_id", "") or "", []).append(f)
        out = _empty_walk()
        for account_fills in by_account.values():
            account_fills.sort(key=lambda f: f.get("time_sort", "") or "")
            for k, v in cls._walk_account_cost_basis(account_fills).items():
                out["cb_" + k] += v
            for k, v in cls._walk_account(account_fills).items():
                out["ws_" + k] += v
            for day_fills in cls._by_session_day(account_fills):
                for k, v in cls._walk_account(day_fills).items():
                    out[k] += v
        return out

    @staticmethod
    def _by_session_day(fills: list[dict]) -> list[list[dict]]:
        """*fills* (already time-ordered) split into per-session-day
        groups, each still in time order, oldest day first.

        Groups on the ``_ts_session`` stamp, falling back to a live
        ``session_day`` computation for a fill some other path stored
        before the stamp existed.  Fills with no usable timestamp share
        one ``None`` bucket rather than being dropped — they are already
        treated as in-window everywhere else, so they keep pairing
        against each other.
        """
        groups: dict[Any, list[dict]] = {}
        for f in fills:
            if "_ts_session" in f:
                day = f["_ts_session"]
            else:
                day = session_day(fill_dt(f),
                                  f.get("sec_type", "") or "")
            groups.setdefault(day, []).append(f)
        return [groups[k] for k in sorted(
            groups, key=lambda d: (d is None, d or date.min))]

    @staticmethod
    def _walk_account(fills: list[dict]) -> dict[str, float]:
        """Chronological FIFO walk over ONE account's fills in ONE
        contract (*fills* time-ordered, earliest first).

        FRAME-RELATIVE by design: the walk sees nothing outside the
        fills it is given.  Each fill either closes lots on the other
        side that are already in this frame → a **paired** round trip,
        valued gross from both legs' own fill prices — or opens a lot →
        the **open** remainder, marked to the current price by
        ``_mark_open``.

        ``_walk_fills`` calls this once per SESSION DAY for the
        window-relative numbers, so "the frame" is normally one trading
        day and R.P&L means "what this day's trading closed".  A fill
        that really closed a position opened before its own day (the
        morning sale of an overnight long) opens a day-short here and
        marks to the current price; NET 0 within the day always means
        U.P&L 0 for that day.  IB's own realized number (computed
        against the account's average cost, a basis that reaches outside
        any frame) is deliberately NOT folded in; it stays visible in
        the separate Rlz P&L column, and the "Acct P&L" footer row folds
        it in via ``_walk_account_cost_basis``.
        """
        out = _empty_leg_walk()
        # Open in-frame lots, FIFO.  Always all on the same side: a lot
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

            if left > 0:
                lots.append([left, price, side, comm_unit])

        for qty, price, side, _comm_unit in lots:
            if side == "BOT":
                out["open_buys_qty"] += qty
                out["open_buys_notional"] += qty * price
            else:
                out["open_sells_qty"] += qty
                out["open_sells_notional"] += qty * price
        return out

    @staticmethod
    def _walk_account_cost_basis(fills: list[dict]) -> dict[str, float]:
        """Cost-basis variant of ``_walk_account`` — feeds the "Acct P&L"
        footer row.  IB's avg-cost realized is taken DIRECTLY: a fill
        carrying a nonzero ``realized_pnl`` (stamped by IB's
        commissionReport on every closing execution, computed against
        the account's average cost — a basis we can't see) contributes
        that number as-is, so the section reconciles with the flat
        view's Rlz P&L column instead of re-pairing against in-window
        buys.

        Three outcomes per fill, in that order:

        * it carries a nonzero ``realized_pnl`` → **closing**, valued
          whole from IB — it never pairs against in-window lots (IB
          already valued the close against the account's average cost;
          pairing it here too would both double-count and disagree with
          the per-fill Rlz P&L column).  It does still RETIRE the lots
          it closed: leaving them in the book lets the next fill that
          opens on the other side pair against inventory IB has already
          realized,
        * it closes in-window lots on the other side → a **paired**
          round trip, valued gross from both legs' own fill prices —
          reached only by fills IB stamped $0 on (scratch closes, or
          closes whose commissionReport hasn't landed yet),
        * otherwise it opens a lot → the **open** remainder.  (Tracked
          in the walk for completeness, but not displayed: the Acct P&L
          section is realized-only.)

        Only a NONZERO realized P&L marks a closing fill: live
        commissionReports stamp realizedPNL = 0.0 on opening executions
        too, so ``is not None`` would classify every opening leg as a $0
        close and pin U.P&L at zero.  (A genuine scratch close at exactly
        $0.00 falls through to pairing — harmlessly close to truth.)  A
        closing fill whose commissionReport hasn't landed yet also falls
        through: it pairs in-window (or sits in the undisplayed open
        remainder) until the report arrives and the re-emitted fill
        sends the walk round again — self-healing, never double-counted.

        IB's ``realized_pnl`` is already NET of both legs' commissions,
        so ``closing_comm`` carries back the part of that deduction this
        window can see: the closing fill's own commission, plus the
        commission on each in-window opening lot the close retires.
        Callers add it to keep every reported P&L gross, so the Comm
        column can subtract each commission exactly once instead of
        twice.  A close of pre-window inventory retires no lots and so
        adds back only its own — its opening commission never entered
        the Comm column either, so the two stay in step.

        Returns unprefixed keys; ``_walk_fills`` folds them into the
        shared walk dict under the ``cb_`` prefix.
        """
        out = {
            "paired_qty": 0.0, "paired_buys_notional": 0.0,
            "paired_sells_notional": 0.0, "paired_comm": 0.0,
            "closing_realized": 0.0, "closing_comm": 0.0,
            "open_buys_qty": 0.0, "open_buys_notional": 0.0,
            "open_sells_qty": 0.0, "open_sells_notional": 0.0,
        }
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

            if rpnl:
                out["closing_realized"] += float(rpnl)
                out["closing_comm"] += comm
                # Retire the in-window lots this fill closed.  Its P&L
                # still comes whole from IB — the walk only needs the
                # inventory gone, because a later fill that OPENS on the
                # other side carries realized_pnl 0 and so reaches the
                # pairing branch below: left in the book, these lots pair
                # against it and fabricate a round trip nobody traded,
                # valued across the entire price move since.
                #
                # Each retired lot also carries back its own commission:
                # IB's realized is net of BOTH legs, and the opening leg
                # is in this window's Comm column, so without this the
                # Comm column deducts it a second time.  A close of
                # inventory opened BEFORE the window retires no lots and
                # adds nothing back — correct, since that commission is
                # absent from the Comm column too.
                left = qty
                while left > 0 and lots and lots[0][2] != side:
                    lot = lots[0]
                    use = min(lot[0], left)
                    out["closing_comm"] += use * lot[3]
                    lot[0] -= use
                    left -= use
                    if lot[0] <= 0:
                        lots.pop(0)
                # Any remainder closed pre-window inventory or flipped
                # the position; either way it opens nothing here.  A
                # later fill closing the flipped side carries its own IB
                # realized number, so inventing a lot for the remainder
                # would only re-create the phantom pairing above in
                # mirror image.
                continue

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

            if left > 0:
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
    def _paired_net(walk: dict[str, float], multiplier: float,
                    prefix: str = "") -> float:
        """Gross cash flow of the closed round trips, multiplier-scaled
        so futures (NQ × 20, MNQ × 2, …) report in dollars.  *prefix*
        selects the walk variant ("" = window-relative, "cb_" =
        cost-basis)."""
        return ((walk[prefix + "paired_sells_notional"]
                 - walk[prefix + "paired_buys_notional"])
                * multiplier)

    def _mark_open(self, walk: dict[str, float], agg: dict,
                   prefix: str = "") -> float:
        """The walk's open remainder marked to the last price — a
        net-long remainder valued as if sold, a net-short as if bought
        back.  Falls back to the newest fill price when no live quote has
        been seen: without a mark, an all-buys position would report its
        entire cost as unrealized loss.  *prefix* selects the walk
        variant ("" = window-relative, "cb_" = cost-basis)."""
        mult = agg.get("multiplier", 1.0)
        cash = (walk[prefix + "open_sells_notional"]
                - walk[prefix + "open_buys_notional"]) * mult
        open_net = (walk[prefix + "open_buys_qty"]
                    - walk[prefix + "open_sells_qty"])
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
        """R.P&L for one row: Σ over session days of that day's closed
        round trips, GROSS (commission is deducted once, by the Comm
        column).  A position carried between days is NOT a round trip
        here — its value marks through U.P&L instead, leaving the P&L
        column unchanged (see ``_walk_fills``).

        A MATCHED row is the one exception: "match buy & sell" means
        "pair the legs across the whole window and drop the remainder",
        so it reads the unsegmented ``ws_`` walk — the same pairing
        ``_effective_agg`` trims to, and the same number this row
        reported before day segmentation."""
        walk = self._walk_for_key(key)
        prefix = "ws_" if self.is_matched(key) else ""
        return self._paired_net(walk, self._key_multiplier(key), prefix)

    def _unrealized_total(self, key: tuple[str, str], agg: dict) -> float:
        """U.P&L for one row: every session day's unpaired remainder,
        pooled and marked to the last price.  The pooling is what keeps
        a genuine cross-day round trip whole — Monday's leftover long
        and Wednesday's leftover short cancel in the net quantity, so no
        mark is applied and their two notionals net to the real
        profit."""
        if self.is_matched(key):
            return 0.0
        return self._mark_open(self._walk_for_key(key), agg)

    def _row_pnl_total(self, key: tuple[str, str], agg: dict) -> float:
        """P&L column: R.P&L + U.P&L, so the three always reconcile."""
        return self._realized_total(key) + self._unrealized_total(key, agg)

    # ── Cost-basis P&L (the "Acct P&L" footer row) ──────────────────────────

    def _cb_realized_total(self, key: tuple[str, str]) -> float:
        """Cost-basis R.P&L for one key: IB's avg-cost realized on every
        closing fill, plus the in-window round trips of fills IB stamped
        $0 on (scratch closes / reports not yet landed) — GROSS, with
        the commission IB folded into its realized number added back so
        ``P&L − Comm`` deducts commission exactly once.  Matching does
        NOT trim this section: IB's realized is what the account
        actually booked, unchanged by a display-side trim (the window
        section above carries the trimmed round-trip view)."""
        walk = self._walk_for_key(key)
        realized = self._paired_net(
            walk, self._key_multiplier(key), prefix="cb_")
        return (realized + walk["cb_closing_realized"]
                + walk["cb_closing_comm"])


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
        return self.pnl_totals(accounts, TradeWindow.last_days(
            1, self._compute_anchor()))

    def pnl_totals(self, accounts=None, window: TradeWindow | None = None,
                   ) -> tuple[float, float, float, float]:
        """(R.P&L, U.P&L, P&L, commission) for *accounts* (raw IB ids via
        ``account_id``; empty/None = every account) over *window* — any
        entry from the control bar's date dropdown (see
        ``trades_date_filter``).  ``None`` means today.

        Deliberately independent of the view's search text and account
        filter — this feeds the always-on status-bar aggregate.  Fills
        the user has HIDDEN are excluded, though: a hide is session-level
        ("stop counting this transaction") and drops the fill from every
        calculation until Restore hidden.  Same math as the summary rows
        (``_walk_fills``): per-account in-frame round trips, plus the
        unmatched remainder marked to the last cached price (newest fill
        price when no quote is cached).  The commission element sums the
        in-frame fills' reported commissions (TWS only sends a
        commissionReport for a subset of fills, so this is "commissions
        reported so far" — same caveat as the summary Comm column); the
        R/U/P&L elements stay gross so they keep matching the summary
        rows, and net-of-commission = P&L − commission.
        """
        sel = {a for a in (accounts or ()) if a}
        anchor = self._compute_anchor()
        if window is None:
            window = TradeWindow.last_days(1, anchor)

        # Memoize the price-independent half.  This runs on the status
        # bar's 1 s timer forever, so the O(fills) scan+group+walk must
        # not repeat when nothing but prices moved — the mark term below
        # reads ``_last_prices`` live, so ticks are reflected without
        # invalidation.  The anchor in the key makes midnight rollover
        # invalidate naturally.
        memo_key = (self._fills_rev, frozenset(sel),
                    window.cache_key(), anchor,
                    frozenset(self._offline_labels))
        if self._pnl_memo is not None and self._pnl_memo[0] == memo_key:
            _, groups_list, comm_total, r_total = self._pnl_memo
            u_total = sum(self._mark_open(w, m) for w, m in groups_list)
            return r_total, u_total, r_total + u_total, comm_total

        in_frame = window.contains_fill

        groups: dict[tuple[str, str], list[dict]] = {}
        comm_total = 0.0
        for f in self._fills.values():
            if (f.get("label", "") or "") in self._offline_labels:
                continue
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
        groups_list: list[tuple[dict, dict]] = []
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
            groups_list.append((walk, meta))
            r_total += self._paired_net(walk, meta["multiplier"])
            u_total += self._mark_open(walk, meta)
        # groups_list keeps the grouping loop's order, so a memo hit sums
        # the mark terms in the identical order — float-exact results.
        self._pnl_memo = (memo_key, groups_list, comm_total, r_total)
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
            return DAY_TOTAL_LABEL
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

    # ── Cost-basis ("Acct P&L") footer-row rendering ────────────────────────

    def _trade_header_data(self, col: int, role: int) -> Any:
        """Render the band that opens the window-relative section — the
        "TRADE LEVEL" label in the Symbol cell (the view merges the band
        into ONE full-width cell, so nothing outside column 0 would
        show).  Label color matches the column-header captions; banded
        chrome via ``IsFooterRowRole``."""
        if role == Qt.ItemDataRole.DisplayRole:
            return TRADE_SECTION_LABEL if col == S_COL_SYMBOL else ""
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor(CLR_SECTION)
        if role == Qt.ItemDataRole.BackgroundRole:
            return QColor(BG_TOTAL)
        if role == Qt.ItemDataRole.FontRole:
            return self._total_font
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter)
        return None

    def _acct_header_data(self, col: int, role: int) -> Any:
        """Render the band that opens the cost-basis section — the
        "ACCOUNT LEVEL" label, merged into one full-width cell by the
        view.  Sits below the blank spacer row so the two sections read
        as distinct blocks."""
        if role == Qt.ItemDataRole.DisplayRole:
            return ACCT_SECTION_LABEL if col == S_COL_SYMBOL else ""
        if role == Qt.ItemDataRole.ForegroundRole:
            return QColor(CLR_SECTION)
        if role == Qt.ItemDataRole.BackgroundRole:
            return QColor(BG_TOTAL)
        if role == Qt.ItemDataRole.FontRole:
            return self._total_font
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter)
        return None

    def _cost_row_data(self, col: int, role: int) -> Any:
        """Render a cell on the cost-basis section's "TOTAL" row, where
        a fill that closed a position opened before the window
        contributes IB's avg-cost realized P&L instead of opening a
        window-short.  Plain row background, same as the window
        section's TOTAL."""
        if role == Qt.ItemDataRole.DisplayRole:
            return self._cost_cell_text(col)
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._cost_cell_color(col)
        if role == Qt.ItemDataRole.FontRole:
            return self._total_font
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in _S_RIGHT_ALIGN_COLS:
                return int(Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter)
        return None

    def _cost_value(self, col: int) -> float | None:
        """Cost-basis footer number for the R.P&L / Net P&L columns,
        ``None`` for columns the row leaves blank (the section is
        realized-only — see ``_cb_cell_value``).  Summed over the same
        visible set as the "P&L" footer, so hides / matches / account
        filters gate both rows identically."""
        pairs = self._visible_key_aggs()
        if col == S_COL_PAIRED_PNL:
            return sum(self._cb_realized_total(k) for k, _a in pairs)
        if col == S_COL_NET_INCL_COMM:
            return sum(self._cb_realized_total(k) - a.get("commission", 0.0)
                       for k, a in pairs)
        return None

    def _cost_cell_text(self, col: int) -> str:
        if col == S_COL_SYMBOL:
            return ACCT_TOTAL_LABEL
        # Trans total / Comm sum the visible rows the same way the "P&L"
        # footer does — identical values, both sections fold the same
        # fills.
        if col == S_COL_TRANS_TOTAL:
            return self._fmt_compact(
                sum(self._trans_total(a) for a in self._visible_aggs()))
        if col == S_COL_COMMISSION:
            s = sum(a.get("commission", 0.0) for a in self._visible_aggs())
            return f"{s:,.2f}" if s else ""
        v = self._cost_value(col)
        return self._fmt_signed(v) if v is not None else ""

    def _cost_cell_color(self, col: int) -> QColor | None:
        if col == S_COL_SYMBOL:
            return QColor(TEXT_BOLD)
        if col == S_COL_TRANS_TOTAL:
            return QColor(TEXT_BOLD)
        v = self._cost_value(col)
        if v is not None:
            if v > 0:
                return QColor(CLR_BUY)
            if v < 0:
                return QColor(CLR_SELL)
            return QColor(TEXT_DIM)
        return QColor(TEXT)

    def _cb_cell_value(self, key: tuple[str, str], agg: dict,
                       col: int) -> float | None:
        """Cost-basis number for a per-symbol row's R.P&L / Net P&L
        cell, ``None`` for every other column.  The section is
        REALIZED-ONLY: an unrealized mark of our own next to IB's
        authoritative realized number reads as IB's unrealized (it
        isn't — see the Positions tab for that), so U.P&L and P&L stay
        blank and Net P&L = R.P&L − Comm."""
        if col == S_COL_PAIRED_PNL:
            return self._cb_realized_total(key)
        if col == S_COL_NET_INCL_COMM:
            return (self._cb_realized_total(key)
                    - agg.get("commission", 0.0))
        return None

    def _cb_row_data(self, row: int, col: int, role: int) -> Any:
        """Render a cell on a cost-basis per-symbol row — same key set
        and order as the window section above.  R.P&L / Net P&L come
        from the cost-basis walk; U.P&L and P&L stay BLANK (realized-only
        section, see ``_cb_cell_value``); every other column delegates to
        the window row's own renderers (both sections fold the same
        fills, so quantities, averages, commission and venues are
        identical by construction).  Ordinary row chrome (no totals
        band); the "P&L" footer above separates the two sections."""
        key = self._cb_key_at(row)
        agg = self._effective_agg(key) if key is not None else None
        if agg is None:
            return None
        blank = col in (S_COL_UNPAIRED_PNL, S_COL_NET_TOTAL)
        if role == Qt.ItemDataRole.DisplayRole:
            if blank:
                return ""
            v = self._cb_cell_value(key, agg, col)
            if v is not None:
                return self._fmt_signed(v)
            return self._cell_text(agg, col, key)
        if role == SortValueRole:
            if blank:
                return ""
            v = self._cb_cell_value(key, agg, col)
            if v is not None:
                return v
            return self._sort_value(agg, col, key)
        if role == Qt.ItemDataRole.ForegroundRole:
            if blank:
                return QColor(TEXT_DIM)
            v = self._cb_cell_value(key, agg, col)
            if v is not None:
                if v > 0:
                    return QColor(CLR_BUY)
                if v < 0:
                    return QColor(CLR_SELL)
                return QColor(TEXT_DIM)
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

    def _sort_value(self, agg: dict, col: int, key=None):
        """Raw sortable value for a cell — same numbers the display
        formats, so the sort proxy orders numerically.  The totals footer
        never reaches here (its ``data`` path answers ``None`` and the
        proxy pins it to the bottom regardless of sort)."""
        if col == S_COL_SYMBOL:
            return (agg.get("symbol", "") or "").upper()
        if col == S_COL_TYPE:
            return agg.get("sec_type", "") or ""
        if col == S_COL_BUYS:
            return agg.get("buys_qty", 0.0)
        if col == S_COL_SELLS:
            return agg.get("sells_qty", 0.0)
        if col == S_COL_NET:
            return agg.get("buys_qty", 0.0) - agg.get("sells_qty", 0.0)
        if col == S_COL_AVG_BUY:
            b_q = agg.get("buys_qty", 0.0)
            return agg.get("buys_notional", 0.0) / b_q if b_q else 0.0
        if col == S_COL_AVG_SELL:
            s_q = agg.get("sells_qty", 0.0)
            return agg.get("sells_notional", 0.0) / s_q if s_q else 0.0
        if col == S_COL_TRANS_TOTAL:
            return self._trans_total(agg)
        if col == S_COL_PAIRED_PNL:
            return self._paired_pnl(key)
        if col == S_COL_UNPAIRED_PNL:
            return self._unrealized_total(key, agg) if key else 0.0
        if col == S_COL_NET_TOTAL:
            return self._row_pnl_total(key, agg) if key else 0.0
        if col == S_COL_COMMISSION:
            return agg.get("commission", 0.0)
        if col == S_COL_NET_INCL_COMM:
            return ((self._row_pnl_total(key, agg) if key else 0.0)
                    - agg.get("commission", 0.0))
        if col == S_COL_REALIZED:
            return agg.get("realized_pnl", 0.0)
        if col == S_COL_EXCHANGES:
            return ", ".join(sorted(agg.get("exchanges", set())))
        return ""

    def _cell_text(self, agg: dict, col: int, key=None) -> str:
        if col == S_COL_SYMBOL:
            # agg["symbol"] stays the raw local symbol (price-cache key);
            # prettify OCC option symbols only at render time.  Options
            # stack root+expiry over strike+right — the shared delegate
            # paints newline'd cells as two small-font lines.
            return format_option_symbol_two_line(agg.get("symbol", ""))
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
        cached = fill.get("_agg_key")
        if cached is not None:
            return cached
        return _compute_agg_key(fill)

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

        Respects the date filter set via ``set_window``: fills are
        always stored in ``_fills`` (so a later filter change can pull
        them back in) but only aggregated into the visible rows when
        they fall inside the current window.
        """
        if "_agg_key" not in fill:
            stamp_fill(fill)
        exec_id = fill.get("exec_id", "")
        key = self._agg_key(fill)

        # Invalidate the walk cache before any early return — a stored
        # fill changes this key's walk whether or not it is currently
        # visible, and a re-emit may have migrated keys.
        self._walk_cache.pop(key, None)
        self._fills_rev += 1
        self._pnl_memo = None

        # Detect an anchor advance (the midnight ET rollover — a fresh
        # session's first fill arrives and "today" has moved forward).
        # A full rebuild is the simplest correct response — yesterday's
        # aggregates may need to drop out of a 1-day window and
        # rebuilding is O(n_fills).
        prev_anchor = self._anchor_date
        self._anchor_date = self._compute_anchor()
        # An unbounded window admits everything, so a date change can't
        # move any fill in or out of it — skip the rebuild.
        anchor_changed = (not self._window.is_unbounded
                          and prev_anchor != self._anchor_date)

        # Re-snapshot case: same execId arrived again.  Roll back the old
        # contribution and re-apply with the new data (commission may now
        # have arrived for the same execId).  Only subtract if the prior
        # fill was actually counted (i.e. it passed the filter).
        prev = self._fills.get(exec_id) if exec_id else None
        if prev is not None:
            prev_key = self._agg_key(prev)
            if prev_key != key:
                self._walk_cache.pop(prev_key, None)
            bucket = self._fills_by_key.get(prev_key)
            if bucket is not None:
                bucket.pop(exec_id, None)
                if not bucket:
                    del self._fills_by_key[prev_key]
            if self._passes_filter(prev):
                self._subtract_from_agg(prev_key, prev)
                if self._agg.get(prev_key, {}).get("fill_count", 0) == 0:
                    self._drop_symbol_row(prev_key)

        if exec_id:
            self._fills[exec_id] = fill
            self._fills_by_key.setdefault(key, {})[exec_id] = fill

        if anchor_changed:
            # Rebuild from all fills under the new anchor; we've already
            # stored the new fill above so it's included in the rebuild.
            self._rebuild_from_fills()
            return

        # Gate incremental aggregation on the filter — fills outside the
        # window are kept in _fills but not reflected in the grid.
        if not self._passes_filter(fill):
            return

        # Add to aggregate.  Insert new rows if this key is new — the
        # window row and its cost-basis twin are never adjacent, so they
        # go in as two transactions (rowCount stays consistent inside
        # each: ``_order`` and ``_order_cb`` are mutated separately).
        if key not in self._agg:
            n = len(self._order)
            if n == 0:
                # First aggregate materialises every band row too — rows
                # [0, 4] (trade header + window row + its TOTAL +
                # account header + the cost-basis TOTAL) in one
                # transaction, the cost-basis row in the next.
                self.beginInsertRows(QModelIndex(), 0, 4)
            else:
                # New window row lands at the end of its section, one
                # below the trade-level header band.
                self.beginInsertRows(QModelIndex(), n + 1, n + 1)
            self._agg[key] = self._empty_agg(fill)
            self._order.append(key)
            self.endInsertRows()
            pos = len(self._order) + 3 + len(self._order_cb)
            self.beginInsertRows(QModelIndex(), pos, pos)
            self._order_cb.append(key)
            self.endInsertRows()

        self._add_to_agg(key, fill)

        # Refresh the updated rows *and* the totals footers.  When the
        # symbol is user-hidden the rows aren't in the order lists — we
        # update the stored aggregate but emit nothing because neither
        # the hidden rows nor the totals (which exclude hidden rows)
        # change visually.
        if key in self._order:
            self._emit_pnl_rows(key)

    def set_window(self, window: TradeWindow) -> None:
        """Change the date window — the control bar's dropdown selection
        (see ``trades_date_filter``).  Re-aggregates from the stored
        fills, so a widened window pulls back fills that were filtered
        out earlier."""
        if window is None or window == self._window:
            return
        self._window = window
        self._rebuild_from_fills()

    def set_search_window(self, window: TradeWindow | None) -> None:
        """Set the window parsed out of the search box (``None`` clears
        it).  It overrides the combo window unless that window is a
        session preset, mirroring ``TradesFilterProxy.effectiveWindow``
        so the flat view and the summary totals never disagree."""
        if window == self._search_window:
            return
        self._search_window = window
        self._rebuild_from_fills()

    def window(self) -> TradeWindow:
        """The combo's window, before any typed search narrows it."""
        return self._window

    def effective_window(self) -> TradeWindow:
        """The window fills are actually tested against."""
        if (self._search_window is not None
                and not self._window.has_time_of_day):
            return self._search_window
        return self._window

    def set_search_text(self, text: str) -> None:
        """Substring filter applied to the fill stream before
        aggregation.  Callers should pass the raw substring (datetime
        queries are routed through ``set_search_window`` instead)."""
        new_search = (text or "").strip().lower()
        if new_search == self._search_text:
            return
        self._search_text = new_search
        self._rebuild_from_fills()

    def set_offline_labels(self, labels) -> None:
        """Exclude fills reported by these (disconnected) connections.

        Re-aggregates from the stored fills; nothing is discarded, so
        reconnecting restores the previous totals exactly."""
        new = {str(x) for x in (labels or ()) if x}
        if new == self._offline_labels:
            return
        self._offline_labels = new
        self._rebuild_from_fills()

    def offline_labels(self) -> set[str]:
        return set(self._offline_labels)

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
        # Hidden fills also gate ``pnl_totals``, which doesn't see the
        # other view filters — bump its revision explicitly.
        self._fills_rev += 1
        self._pnl_memo = None
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
        # Repaint rows whose symbol has an open window leg (their U.P&L /
        # P&L columns mark to last price) plus the footer bands.  Closed
        # round-trips don't re-price, and the cost-basis section is
        # realized-only — nothing in it depends on the price.
        for k in list(self._order):
            a = self._effective_agg(k)
            if a is None \
                    or a.get("buys_qty", 0.0) == a.get("sells_qty", 0.0):
                continue
            if sym in (a.get("root_symbol", ""),
                       (a.get("symbol", "") or "").upper()):
                self._emit_pnl_rows(k)

    def clear(self) -> None:
        if not self._order and not self._fills:
            return
        self.beginResetModel()
        self._agg.clear()
        self._order.clear()
        self._order_cb.clear()
        self._fills.clear()
        self._fills_by_key.clear()
        self._walk_cache.clear()
        self._fills_rev += 1
        self._pnl_memo = None
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
        i = self._order.index(key)
        self._remove_cb_row(key)
        if len(self._order) == 1:
            # Hiding the last visible aggregate also hides the band
            # rows — remove all five in one transaction so Qt's
            # row-count bookkeeping stays consistent.
            self.beginRemoveRows(QModelIndex(), 0, 4)
        else:
            self.beginRemoveRows(QModelIndex(), i + 1, i + 1)
        self._order.pop(i)
        self._hidden_symbols.add(key)
        self.endRemoveRows()
        # The footer rows shifted up and their values changed.
        if self._order:
            self._emit_pnl_rows(None)

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
        self._order_cb = list(self._order)
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
        """Repaint *key*'s visible rows and both footer rows."""
        if key not in self._order:
            return
        self._emit_pnl_rows(key)

    def _emit_pnl_rows(self, key: tuple[str, str] | None) -> None:
        """Repaint the P&L-family cells of *key*'s window and cost-basis
        rows (when visible) plus both totals footers.  ``None`` repaints
        the footers only."""
        n = len(self._order)
        m = len(self._order_cb)
        if not (n or m):
            return
        if key is not None:
            if key in self._order:
                row = 1 + self._order.index(key)
                self.dataChanged.emit(
                    self.index(row, 0),
                    self.index(row, _S_NUM_COLS - 1), _PNL_ROLES)
            if key in self._order_cb:
                row = n + 3 + self._order_cb.index(key)
                self.dataChanged.emit(
                    self.index(row, 0),
                    self.index(row, _S_NUM_COLS - 1), _PNL_ROLES)
        # The two TOTAL rows are separated by the cost-basis section,
        # so each gets its own rectangle (the header bands carry no
        # numbers and never need a repaint here).
        total_row = n + 1
        self.dataChanged.emit(
            self.index(total_row, 0),
            self.index(total_row, _S_NUM_COLS - 1), _PNL_ROLES)
        acct_row = n + 3 + m
        self.dataChanged.emit(
            self.index(acct_row, 0),
            self.index(acct_row, _S_NUM_COLS - 1), _PNL_ROLES)

    # ── Date-filter helpers ─────────────────────────────────────────────────

    def _compute_anchor(self) -> date:
        """Rule mirrors ``TradesFilterProxy``: strict "Today" — the
        window is always anchored to the current ET date."""
        return today_et()

    def _passes_filter(self, fill: dict) -> bool:
        if self._offline_labels:
            if (fill.get("label", "") or "") in self._offline_labels:
                return False
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
        """One window, one call — the same predicate the flat proxy and
        ``pnl_totals`` run, so the three views cannot drift apart."""
        return self.effective_window().contains_fill(fill)

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
        self._walk_cache.clear()   # filter state feeding the walks changed
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
        self._order_cb = list(self._order)
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
        self._walk_cache.pop(key, None)
        if key in self._hidden_symbols:
            self._hidden_symbols.discard(key)
            self._agg.pop(key, None)
            return
        if key not in self._order:
            return
        i = self._order.index(key)
        self._remove_cb_row(key)
        if len(self._order) == 1:
            # Dropping the last aggregate also hides the band rows so
            # the view goes back to empty — remove all five in one
            # transaction.
            self.beginRemoveRows(QModelIndex(), 0, 4)
        else:
            self.beginRemoveRows(QModelIndex(), i + 1, i + 1)
        self._order.pop(i)
        self._agg.pop(key, None)
        self.endRemoveRows()

    def _remove_cb_row(self, key: tuple[str, str]) -> None:
        """Remove *key*'s cost-basis section row (its own transaction —
        the window-row removal follows separately).  Removed FIRST so
        the window section's indices are still stable while this row's
        position is computed."""
        if key not in self._order_cb:
            return
        j = self._order_cb.index(key)
        cb_row = len(self._order) + 3 + j
        self.beginRemoveRows(QModelIndex(), cb_row, cb_row)
        self._order_cb.pop(j)
        self.endRemoveRows()
