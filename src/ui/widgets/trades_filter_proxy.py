"""
TradesFilterProxy – shared QSortFilterProxyModel for the flat trades
and summary views.

Filters on two axes:
  * Text search — interpreted in one of two modes:
      - If the input parses as a datetime (e.g. ``Apr21 11:44:40``,
        ``11:44``, ``Apr21``) it becomes a "fills at or after this
        moment" threshold on the flat view.
      - Otherwise it's a case-insensitive substring match against the
        per-view key set (symbol / side / account / sec_type / exchange
        / order_ref for flat; symbol / exchanges for summary).
  * Date range — keep rows whose ``time_sort`` ISO timestamp is within
    the last *N* trading days (ET).  "Today" is strict — the window is
    always anchored to the current ET date — and the lookback counts
    trading days (Mon–Fri), so "Last 2 days" on a Monday reaches back
    to Friday instead of wasting the window on the weekend.  The
    summary model has no per-fill timestamp, so date filtering is a
    no-op on it; the flat view is the intended target.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, Signal

from .table_theme import IsFooterRowRole, SortValueRole

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


def today_et() -> date:
    """Current ET calendar date.  Module-level so tests can pin it."""
    return datetime.now(_ET).date()


def trading_days_cutoff(anchor: date, days_back: int) -> date:
    """Earliest calendar date inside a "last N days" window ending at
    *anchor*, counting only trading days (Mon–Fri) — a weekend never
    consumes part of the window.  ``days_back=1`` → *anchor* itself;
    2 on a Monday → the preceding Friday.  Weekend-dated fills (Sunday
    futures session) still pass because the check is ``date >= cutoff``,
    not membership in the trading-day set."""
    d = anchor
    remaining = max(0, int(days_back) - 1)
    while remaining > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:            # Mon–Fri
            remaining -= 1
    return d

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

    Fills in missing pieces from the current ET clock: month/day-only
    inputs get the current year at 00:00 ET; time-only inputs get
    today's ET date.  Month abbreviations are accepted case-insensitively
    (``APR21``, ``apr21``, ``Apr21`` all work)."""
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
    now = datetime.now(_ET)
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


class TradesFilterProxy(QSortFilterProxyModel):
    """Proxy model wiring the control-bar search box + date range to the
    flat/summary trades sources."""

    # Emitted whenever the user-hidden fill set changes (hide / restore /
    # auto-clear on filter change).  The panel listens so it can mirror the
    # hidden exec_ids into the summary model and keep the two views in sync.
    hiddenKeysChanged = Signal()

    def __init__(self, parent=None, *, is_summary: bool = False):
        super().__init__(parent)
        self._is_summary = is_summary
        # Header-click sorting compares raw values (floats/ints/strings)
        # rather than the formatted display text, so 9.99 < 10.00 and
        # "+1,234.56" doesn't sort as a string.
        self.setSortRole(SortValueRole)
        self._search: str = ""
        # When the user types a parseable datetime, the proxy switches
        # into "time threshold" mode: flat rows are kept only if their
        # ``time_sort`` is ≥ this moment.  ``None`` means substring mode.
        self._time_threshold: datetime | None = None
        # Set by the control-bar combo's session-of-day presets
        # (Morning / Afternoon / Evening).  Takes priority over the
        # search-text-parsed threshold above.
        self._preset_threshold: datetime | None = None
        self._days_back: int = 1  # "Today" by default
        # Anchor date used by the date filter — strictly the current ET
        # date ("Today" never shows a previous session's fills).
        # Recomputed on row inserts / model reset so an app left running
        # overnight picks up the midnight rollover.
        self._anchor_date: date = today_et()
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
        self._selected_accounts: set[str] = set()

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
            new_search = ""
            new_threshold = parsed if not self._is_summary else None
        else:
            new_search = raw.lower()
            new_threshold = None
        if new_search == self._search and new_threshold == self._time_threshold:
            return
        self._search = new_search
        self._time_threshold = new_threshold
        self.invalidateFilter()

    def setDaysBack(self, days: int) -> None:
        """1 = today, 2 = last 2 trading days, …, 7 = last 7 trading
        days (weekends don't consume the window)."""
        days = max(1, int(days or 1))
        if days == self._days_back:
            return
        self._days_back = days
        self.invalidateFilter()

    def setSelectedAccounts(self, accounts) -> None:
        """Restrict rows to fills whose ``account_id`` is in *accounts*
        (raw IB account ids).  Empty/None shows every account."""
        new = {a for a in (accounts or ()) if a}
        if new == self._selected_accounts:
            return
        self._selected_accounts = new
        self.invalidateFilter()

    def setPresetThreshold(self, dt: datetime | None) -> None:
        """Set the session-of-day preset threshold (Morning / Afternoon /
        Evening).  ``None`` clears it."""
        if dt == self._preset_threshold:
            return
        self._preset_threshold = dt
        self.invalidateFilter()

    def daysBack(self) -> int:
        """Current date window (1 = today, N = last N days)."""
        return self._days_back

    def presetThreshold(self) -> datetime | None:
        return self._preset_threshold

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

    def lessThan(self, left: QModelIndex,
                 right: QModelIndex) -> bool:  # noqa: N802 (Qt style)
        """Default sort-role comparison, except the summary's totals
        footer (the row answering ``IsFooterRowRole``) is pinned to the
        bottom whichever column/direction is active.  Qt renders
        descending order by inverting this comparison, so the footer's
        answer has to flip with the current sort order to stay last."""
        src = self.sourceModel()
        if src is not None:
            if src.data(left, IsFooterRowRole):
                return self.sortOrder() == Qt.SortOrder.DescendingOrder
            if src.data(right, IsFooterRowRole):
                return self.sortOrder() == Qt.SortOrder.AscendingOrder
        return super().lessThan(left, right)

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
        if self._selected_accounts:
            acct = data.get("account_id", "")
            if acct and acct not in self._selected_accounts:
                return False

        # Time-threshold filter — show only fills at or after this
        # moment.  The session-of-day preset (from the combo) beats the
        # search-parsed threshold so substring search can stay active
        # alongside a Morning/Afternoon/Evening selection.  Rows with
        # no time_sort are left visible (safer default than dropping
        # them silently).
        effective_threshold = (self._preset_threshold
                               if self._preset_threshold is not None
                               else self._time_threshold)
        if effective_threshold is not None:
            # Prefer the parse-once stamp the summary model puts on the
            # shared fill dict (see ``stamp_fill``) — re-parsing here per
            # row per filter pass was a measurable per-tick cost.
            if "_ts_dt" in data:
                ts = data["_ts_dt"]
            else:
                ts = None
                time_sort = data.get("time_sort", "")
                if time_sort:
                    try:
                        ts = datetime.fromisoformat(time_sort)
                    except ValueError:
                        ts = None
                    if ts is not None and ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_ET)
            if ts is not None and ts < effective_threshold:
                return False

        # Substring search — match across the per-view key set.  Keys
        # whose value is a set/list (e.g. summary "exchanges") are
        # flattened to a space-joined lowercase string so substring
        # matching still works ("cme" matches {"CME"}).  Runs alongside
        # the preset threshold so the user can narrow by symbol while a
        # session filter is active.
        if self._search:
            keys = _SEARCH_KEYS_SUMMARY if self._is_summary else _SEARCH_KEYS_FLAT
            matched = False
            for k in keys:
                v = data.get(k)
                if v is None or v == "":
                    continue
                if isinstance(v, (set, list, tuple)):
                    s = " ".join(str(x) for x in v).lower()
                else:
                    s = str(v).lower()
                if s and self._search in s:
                    matched = True
                    break
            if not matched:
                return False

        # Date filter – flat model only.  Skipped when either threshold
        # is active (a preset or a typed moment is strictly more
        # restrictive than the relative "last N days" window, so
        # applying both would just double-filter the same rows).
        if (not self._is_summary
                and self._days_back > 0
                and effective_threshold is None):
            local = data.get("_ts_date")   # stamp_fill parse-once stamp
            if local is not None:
                if local < trading_days_cutoff(self._anchor_date,
                                               self._days_back):
                    return False
            elif "_ts_date" not in data:
                time_sort = data.get("time_sort", "")
                if time_sort and not self._within_days(
                        time_sort, self._days_back):
                    return False

        return True

    # ── Date-anchor bookkeeping ─────────────────────────────────────────────

    def _on_rows_inserted(self, *_args) -> None:
        prev = self._anchor_date
        self._recompute_anchor()
        if prev != self._anchor_date:
            # A newer fill advanced the anchor – re-evaluate visible rows.
            self.invalidateFilter()

    def _recompute_anchor(self) -> None:
        """Strict "Today": the window is always anchored to the current
        ET date — a fresh morning shows an empty panel, never yesterday's
        fills.  (The lookback presets count trading days, so yesterday's
        session is one combo step away.)"""
        self._anchor_date = today_et()

    def _within_days(self, iso_time: str, days_back: int) -> bool:
        try:
            ts = datetime.fromisoformat(iso_time)
        except ValueError:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_ET)
        local = ts.astimezone(_ET).date()
        return local >= trading_days_cutoff(self._anchor_date, days_back)
