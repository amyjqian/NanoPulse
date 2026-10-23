"""
TradesFilterProxy – shared QSortFilterProxyModel for the flat trades
and summary views.

Filters on two axes:
  * Text search — interpreted in one of two modes:
      - If the input parses as a datetime (e.g. ``Apr21 11:44:40``,
        ``11:44``, ``Apr21``) it becomes a "fills at or after this
        moment" threshold on the flat view.
      - Otherwise it's a :class:`~.search_query.SearchQuery` — AND-ed
        case-insensitive terms, ``not x`` / ``-x`` to exclude — matched
        against the per-view key set (symbol / side / account /
        sec_type / exchange / order_ref for flat; symbol / exchanges
        for summary).
  * Date range — keep rows whose ``time_sort`` ISO timestamp falls in
    the active :class:`~.trades_date_filter.TradeWindow`, which is what
    every entry in the control bar's date dropdown resolves to.  See
    ``trades_date_filter`` for the window table; the short version is
    that "Today" is strict (always anchored to the current ET date) and
    every span counts CALENDAR days, weekends included.  The summary
    model has no per-fill timestamp — its rows are aggregates — so the
    window is applied inside ``TradesSummaryModel`` instead and skipped
    here; the flat view is this proxy's target.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, Signal

from .search_query import SearchQuery
from .table_theme import IsFooterRowRole, SectionRankRole, SortValueRole
from .trades_date_filter import TradeWindow, today_et

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


# ``today_et`` is re-exported from ``trades_date_filter`` (imported
# above) so this module stays the import site it has always been — and
# so tests that pin the clock by patching ``trades_filter_proxy.today_et``
# keep working, since the calls below resolve it through this module's
# own globals.

# Datetime formats accepted in the search box for the time-threshold
# mode.  Ordered so longer / more specific patterns match first (strptime
# is strict — "Apr21 11:44:40" won't match "%b%d" alone).
_TIME_FORMATS = (
    "%b%d %H:%M:%S",
    "%b %d %H:%M:%S",
    "%b%d %H:%M",
    "%b %d %H:%M",
    "%b%d",
    "%b %d",
    "%H:%M:%S",
    "%H:%M",
)


def _try_parse_time_query(text: str) -> datetime | None:
    """Return an ET-aware datetime if *text* looks like a time/date
    query, else ``None``.

    Fills in missing pieces from the current ET date: month/day-only
    inputs get the current year at 00:00 ET; time-only inputs get
    today's ET date.  Month abbreviations are accepted case-insensitively
    (``APR21``, ``apr21``, ``Apr21`` all work).

    Reads the date through ``today_et`` rather than calling
    ``datetime.now`` directly, so the module has exactly one clock and
    tests that pin the date pin this too."""
    raw = (text or "").strip()
    if not raw:
        return None
    # Normalize the month abbreviation's case so strptime's %b accepts
    # any casing the user types.  Only the first three chars — we don't
    # want to touch the rest of the input.
    if len(raw) >= 3 and raw[:3].isalpha():
        normalized = raw[:3].title() + raw[3:]
    else:
        normalized = raw
    now = today_et()
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        # Fill in missing fields from the current ET moment.
        if "%Y" not in fmt:
            dt = dt.replace(year=now.year)
        if "%b" not in fmt and "%m" not in fmt:
            dt = dt.replace(month=now.month, day=now.day)
        return dt.replace(tzinfo=_ET)
    return None

# Keys the search box matches, per underlying model type.
# The proxy asks the source model for ``row_data(row)`` and matches these.
# Flat view: every common identifying column a user might type.
# Summary view: symbol + the exchanges set (so "nasdaq" / "cme" filter).
_SEARCH_KEYS_FLAT = (
    "symbol", "local_symbol", "side", "account", "account_id",
    "sec_type", "exchange", "order_ref",
)
_SEARCH_KEYS_SUMMARY = ("symbol", "sec_type", "exchanges")


def _sort_value_less(left, right) -> bool:
    """Order two ``SortValueRole`` values for a *secondary* sort column.

    Only reached once the higher-priority columns tied, so it never has to
    reproduce Qt's own comparison — it just has to be total and not raise
    on the mixed types the models emit (floats, ints, strings, blanks).
    Blanks sort first, numbers before text.
    """
    if left is None or right is None:
        return left is None and right is not None
    l_num = isinstance(left, (int, float)) and not isinstance(left, bool)
    r_num = isinstance(right, (int, float)) and not isinstance(right, bool)
    if l_num and r_num:
        return left < right
    if l_num != r_num:
        return l_num
    return str(left).lower() < str(right).lower()


class TradesFilterProxy(QSortFilterProxyModel):
    """Proxy model wiring the control-bar search box + date range to the
    flat/summary trades sources."""

    # Emitted whenever the user-hidden fill set changes (hide / restore /
    # auto-clear on filter change).  The panel listens so it can mirror the
    # hidden exec_ids into the summary model and keep the two views in sync.
    hiddenKeysChanged = Signal()

    # Emitted when the ET calendar date advances under a running app (the
    # midnight rollover).  Every window is resolved against the anchor —
    # "Today" and the named recent days are literally yesterday's dates
    # once it moves — so the panel rebuilds the date combo and re-applies
    # the selection instead of leaving a stale window in place.
    anchorDateChanged = Signal()

    def __init__(self, parent=None, *, is_summary: bool = False):
        super().__init__(parent)
        self._is_summary = is_summary
        # Header-click sorting compares raw values (floats/ints/strings)
        # rather than the formatted display text, so 9.99 < 10.00 and
        # "+1,234.56" doesn't sort as a string.
        self.setSortRole(SortValueRole)
        self._search = SearchQuery()
        # When the user types a parseable datetime the proxy switches into
        # "time threshold" mode: the typed moment becomes a window of its
        # own and substring matching is off.  ``None`` = substring mode.
        self._search_window: TradeWindow | None = None
        # Anchor date used by the date filter — strictly the current ET
        # date ("Today" never shows a previous session's fills).
        # Recomputed on row inserts / model reset so an app left running
        # overnight picks up the midnight rollover.
        self._anchor_date: date = today_et()
        # The combo's selection.  Every dropdown entry — session preset,
        # named day, rolling span, week-to-date — is one of these, so the
        # row test below is a single ``contains`` call.
        self._window: TradeWindow = TradeWindow.last_days(
            1, self._anchor_date)
        # Fills the user has temporarily hidden (flat view), keyed by
        # ``exec_id`` so the hide stays attached to the right fill as rows
        # shift on new inserts.  SESSION-LEVEL: hides survive filter /
        # date-window changes and drop those fills from every calculation
        # (view, summary aggregates, status-bar P&L) until the user
        # explicitly restores them via ``restoreHidden``.
        self._hidden_keys: set[str] = set()
        # Checked accounts from the Account List panel, matched against
        # the row's raw ``account_id`` (the ``account`` column shows the
        # alias).  Empty set = show all, mirroring the positions view.
        # None = no filter (show all); set() = nothing checked (show
        # nothing); {...} = only those accounts.  Same tri-state as the
        # Positions models.
        self._selected_accounts: set[str] | None = None
        # Connection labels currently disconnected.  Their fills stay in
        # the model but are filtered out of the view, so a reconnect
        # restores them — see ``offline_connections``.
        self._offline_labels: set[str] = set()
        # Active sort as (column, order) in priority order — empty means
        # source order.  A second entry only appears when the user
        # ⇧-clicks another caption; see ``TradesPanel``.
        self._sort_specs: list[tuple[int, Qt.SortOrder]] = []

    # ── Public setters (hooked to the control bar) ──────────────────────────

    def setSourceModel(self, model) -> None:  # noqa: N802 (Qt style)
        prev = self.sourceModel()
        if prev is not None:
            try:
                prev.rowsInserted.disconnect(self._on_rows_inserted)
            except (RuntimeError, TypeError):
                pass
            try:
                prev.modelReset.disconnect(self._recompute_anchor)
            except (RuntimeError, TypeError):
                pass
        super().setSourceModel(model)
        if model is not None:
            model.rowsInserted.connect(self._on_rows_inserted)
            model.modelReset.connect(self._recompute_anchor)
        self._recompute_anchor()

    def setSearchText(self, text: str) -> None:
        raw = (text or "").strip()
        parsed = _try_parse_time_query(raw) if raw else None
        # Time-threshold input clears substring mode on both proxies: on
        # the flat view we apply the threshold directly; on the summary
        # view the threshold is handled by the underlying model (via
        # TradesSummaryModel.set_time_threshold from TradesPanel), so
        # the proxy just needs to not filter by the raw text.
        if parsed is not None:
            new_search = SearchQuery()
            new_window = (TradeWindow.from_threshold(parsed)
                          if not self._is_summary else None)
        else:
            new_search = SearchQuery.parse(raw)
            new_window = None
        if new_search == self._search and new_window == self._search_window:
            return
        self._search = new_search
        self._search_window = new_window
        self.invalidateFilter()

    def setWindow(self, window: TradeWindow) -> None:
        """Apply the date dropdown's selection.  One setter for every
        entry — the session presets, the named days, the rolling spans
        and week-to-date all arrive as a ``TradeWindow``."""
        if window is None or window == self._window:
            return
        self._window = window
        self.invalidateFilter()

    def setOfflineLabels(self, labels) -> None:
        """Hide fills reported by these (disconnected) connections.

        A filter, never a delete: the fills stay in the source model, so
        reconnecting brings them back.  That matters more here than for
        positions — IB replays only a limited window, so a fill actually
        discarded is gone for good.
        """
        new = {str(x) for x in (labels or ()) if x}
        if new == self._offline_labels:
            return
        self._offline_labels = new
        self.invalidateFilter()

    def offlineLabels(self) -> set[str]:
        return set(self._offline_labels)

    def setSelectedAccounts(self, accounts) -> None:
        """Restrict rows to fills whose ``account_id`` is in *accounts*
        (raw IB account ids).

        ``None`` clears the filter (every account).  An EMPTY set shows
        nothing: the Account List's checked accounts drive this, so
        "nothing checked" must read as "nothing selected", exactly as
        the Positions view does.
        """
        new = ({a for a in accounts if a} if accounts is not None
               else None)
        if new == self._selected_accounts:
            return
        self._selected_accounts = new
        self.invalidateFilter()

    def window(self) -> TradeWindow:
        """The combo's window, before any typed search narrows it."""
        return self._window

    def effectiveWindow(self) -> TradeWindow:
        """The window rows are actually tested against.

        A datetime typed into the search box replaces the combo's window
        — it is the more specific instruction, and applying both would
        just double-filter the same rows.  A session preset the user
        picked deliberately (Morning / Noon / Evening) outranks it, so
        substring-free time queries can't silently undo the combo.
        """
        if (self._search_window is not None
                and not self._window.has_time_of_day):
            return self._search_window
        return self._window

    # ── User-driven row hide / restore (flat view) ──────────────────────────

    def hideKeys(self, keys) -> None:
        """Temporarily hide every fill whose ``exec_id`` is in *keys*.
        Empty / unknown keys are ignored; a no-op set change skips the
        (expensive) re-filter."""
        new = {k for k in keys if k}
        if not new or new <= self._hidden_keys:
            return
        self._hidden_keys |= new
        self.hiddenKeysChanged.emit()
        self.invalidateFilter()

    def restoreHidden(self) -> None:
        if not self._hidden_keys:
            return
        self._clear_hidden_keys()
        self.invalidateFilter()

    def hiddenCount(self) -> int:
        return len(self._hidden_keys)

    def hiddenKeys(self) -> set[str]:
        """Copy of the currently-hidden ``exec_id`` set (the panel mirrors
        this into the summary model)."""
        return set(self._hidden_keys)

    def _clear_hidden_keys(self) -> None:
        """Drop every hidden key, emitting ``hiddenKeysChanged`` only when
        the set actually had entries."""
        if self._hidden_keys:
            self._hidden_keys.clear()
            self.hiddenKeysChanged.emit()

    def anchorDate(self) -> date:
        """Day the date filter is anchored to — strictly ``today_ET``.
        Exposed so the panel can compute the preset threshold against
        the same day the Today window uses."""
        return self._anchor_date

    # ── Sorting ─────────────────────────────────────────────────────────────

    # The Accounts table on the Connect tab decorates its captions with
    # these same glyphs.  Qt's own sort indicator is invisible on these
    # tables — ``TABLE_QSS`` styles ``QHeaderView::section``, and a
    # style-sheeted header draws no built-in arrow — so the arrow lives
    # in the caption text instead of ``setSortIndicatorShown``.
    SORT_ASC_GLYPH = "\u25b2"      # ▲
    SORT_DESC_GLYPH = "\u25bc"     # ▼

    def sortSpecs(self) -> list[tuple[int, Qt.SortOrder]]:  # noqa: N802
        """Active sort, as ``(column, order)`` in priority order.

        Empty = unsorted (the source model's natural order).  The list is
        the single source of truth for both the comparison in
        :meth:`lessThan` and the ▲/▼ in the caption strip.
        """
        return list(self._sort_specs)

    def setSortSpecs(  # noqa: N802
        self, specs: list[tuple[int, Qt.SortOrder]],
    ) -> None:
        self._sort_specs = [(int(c), o) for c, o in specs]
        # ``invalidate`` first: Qt's ``sort`` no-ops when the primary
        # column and order are unchanged, which is exactly the case when
        # the user only added or flipped a *secondary* column.
        self.invalidate()
        if self._sort_specs:
            first_col, first_order = self._sort_specs[0]
            self.sort(first_col, first_order)
        else:
            # sort(-1) is Qt's documented "back to source order".
            self.sort(-1)

    def sort(self, column: int,
             order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:  # noqa: N802
        """Re-sort, then repaint the caption strip.

        ``headerData`` reads the arrows off ``_sort_specs``, so the strip
        is stale until the header is told to re-ask — including for
        ``sort(-1)``, which drops them.
        """
        super().sort(column, order)
        cols = self.columnCount()
        if cols > 0:
            self.headerDataChanged.emit(
                Qt.Orientation.Horizontal, 0, cols - 1)

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        """Caption, plus this column's arrow when it is part of the sort.

        Secondary levels carry their rank ("▼2"), so a multi-column sort
        reads its own priority order off the strip — same convention as
        the Accounts table on the Connect tab.
        """
        base = super().headerData(section, orientation, role)
        if (role != Qt.ItemDataRole.DisplayRole
                or orientation != Qt.Orientation.Horizontal
                or base is None):
            return base
        for rank, (col, order) in enumerate(self._sort_specs, start=1):
            if col != section:
                continue
            arrow = (self.SORT_ASC_GLYPH
                     if order == Qt.SortOrder.AscendingOrder
                     else self.SORT_DESC_GLYPH)
            return f"{base} {arrow}" if rank == 1 else f"{base} {arrow}{rank}"
        return base

    def lessThan(self, left: QModelIndex,
                 right: QModelIndex) -> bool:  # noqa: N802 (Qt style)
        """Default sort-role comparison, except rows carrying a
        ``SectionRankRole`` (the summary's window rows / "P&L" footer /
        cost-basis rows / "Acct P&L" footer) are ordered by rank first,
        so the sections stay stacked whichever column/direction is
        active.  Qt renders descending order by inverting this
        comparison, so the rank answer has to flip with the current sort
        order to keep the stack in place; within a rank, rows sort
        normally (footer ranks hold one row each, so their source-order
        fallback never really runs).  Models without sections answer no
        rank — every row lands in rank 0 and compares as before."""
        src = self.sourceModel()
        if src is not None:
            l_rank = int(src.data(left, SectionRankRole) or 0)
            r_rank = int(src.data(right, SectionRankRole) or 0)
            if l_rank != r_rank:
                if self.sortOrder() == Qt.SortOrder.AscendingOrder:
                    return l_rank < r_rank
                return l_rank > r_rank
            if l_rank and src.data(left, IsFooterRowRole):
                if self.sortOrder() == Qt.SortOrder.AscendingOrder:
                    return left.row() < right.row()
                return left.row() > right.row()
        return self._compare_specs(left, right)

    def _compare_specs(self, left: QModelIndex,
                       right: QModelIndex) -> bool:
        """Walk the sort specs in priority order, first difference wins.

        With a single spec this is exactly Qt's own comparison — the
        common case stays byte-identical.  Additional specs only ever
        break a tie the ones above them left.

        Qt renders a descending PRIMARY by inverting whatever this
        returns, so the raw answer is right for spec 0 and every later
        spec has to pre-invert to survive that flip (once for its own
        direction, once for the primary's).
        """
        specs = self._sort_specs
        src = self.sourceModel()
        if src is None or len(specs) < 2:
            return super().lessThan(left, right)
        primary = specs[0][1]
        role = self.sortRole()
        for rank, (col, order) in enumerate(specs):
            l_raw = src.data(src.index(left.row(), col), role)
            r_raw = src.data(src.index(right.row(), col), role)
            if l_raw == r_raw:
                continue
            if rank == 0:
                return super().lessThan(left, right)
            result = _sort_value_less(l_raw, r_raw)
            if order == Qt.SortOrder.DescendingOrder:
                result = not result
            if primary == Qt.SortOrder.DescendingOrder:
                result = not result
            return result
        return False

    # ── Core filter ─────────────────────────────────────────────────────────

    def filterAcceptsRow(self, src_row: int,
                         src_parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if model is None or src_parent.isValid():
            return True

        row_data = getattr(model, "row_data", None)
        data = row_data(src_row) if row_data else None
        if data is None:
            return True

        # User-hidden fills (flat view) — keyed by exec_id so the hide is
        # stable as rows shift on new inserts.
        if self._hidden_keys:
            ek = data.get("exec_id", "")
            if ek and ek in self._hidden_keys:
                return False

        # Checked-accounts filter (flat view — summary aggregates span
        # accounts, so the summary model filters at aggregation time
        # instead).  Rows without an account_id are left visible rather
        # than silently dropped.
        if self._offline_labels:
            if (data.get("label", "") or "") in self._offline_labels:
                return False

        if self._selected_accounts is not None:
            if not self._selected_accounts:
                return False            # nothing checked → nothing shown
            acct = data.get("account_id", "")
            if acct and acct not in self._selected_accounts:
                return False

        # Date window — flat model only.  Summary rows are per-symbol
        # aggregates with no timestamp of their own, so the window is
        # applied fill-by-fill inside ``TradesSummaryModel`` instead;
        # running it here too would test a row that can never carry a
        # time and pass every one of them.  Fills whose timestamp is
        # missing or unparseable stay visible (see ``TradeWindow.
        # contains``) rather than being dropped silently.
        if not self._is_summary:
            if not self.effectiveWindow().contains_fill(data):
                return False

        # Text search — the query matches across the per-view key set
        # (``SearchQuery`` flattens set values such as the summary's
        # "exchanges", so "cme" matches {"CME"}).  Runs alongside the
        # preset threshold so the user can narrow by symbol while a
        # session filter is active.
        if self._search:
            keys = _SEARCH_KEYS_SUMMARY if self._is_summary else _SEARCH_KEYS_FLAT
            if not self._search.matches(data.get(k) for k in keys):
                return False

        return True

    # ── Date-anchor bookkeeping ─────────────────────────────────────────────

    def _on_rows_inserted(self, *_args) -> None:
        prev = self._anchor_date
        self._recompute_anchor()
        if prev != self._anchor_date:
            # A newer fill advanced the anchor – re-evaluate visible rows
            # and let the panel re-resolve the combo against the new date.
            self.invalidateFilter()
            self.anchorDateChanged.emit()

    def _recompute_anchor(self) -> None:
        """Strict "Today": the window is always anchored to the current
        ET date — a fresh morning shows an empty panel, never yesterday's
        fills.  (The lookback presets count trading days, so yesterday's
        session is one combo step away.)"""
        self._anchor_date = today_et()

