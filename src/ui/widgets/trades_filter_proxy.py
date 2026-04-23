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
    the last *N* trading days (ET).  The summary model has no per-fill
    timestamp, so date filtering is a no-op on it; the flat view is the
    intended target.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

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
    "symbol", "side", "account", "sec_type", "exchange", "order_ref",
)
_SEARCH_KEYS_SUMMARY = ("symbol", "sec_type", "exchanges")


class TradesFilterProxy(QSortFilterProxyModel):
    """Proxy model wiring the control-bar search box + date range to the
    flat/summary trades sources."""

    def __init__(self, parent=None, *, is_summary: bool = False):
        super().__init__(parent)
        self._is_summary = is_summary
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
        # Anchor date used by the date filter.  Defined as
        # ``max(today_ET, latest_fill_date)`` so opening the app on a
        # weekend / holiday still shows the most recent session instead
        # of a blank panel.  Recomputed when rows are inserted into the
        # source model.
        self._anchor_date: date = datetime.now(_ET).date()

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
        """1 = today, 2 = last 2 days, …, 7 = last 7 days."""
        days = max(1, int(days or 1))
        if days == self._days_back:
            return
        self._days_back = days
        self.invalidateFilter()

    def setPresetThreshold(self, dt: datetime | None) -> None:
        """Set the session-of-day preset threshold (Morning / Afternoon /
        Evening).  ``None`` clears it."""
        if dt == self._preset_threshold:
            return
        self._preset_threshold = dt
        self.invalidateFilter()

    def presetThreshold(self) -> datetime | None:
        return self._preset_threshold

    def anchorDate(self) -> date:
        """Session day the date filter is anchored to — ``today_ET``
        during live trading, rolled back to the most recent fill date
        on weekends / holidays.  Exposed so the panel can compute the
        preset threshold against the same day the Today window uses."""
        return self._anchor_date

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
            time_sort = data.get("time_sort", "")
            if time_sort:
                try:
                    ts = datetime.fromisoformat(time_sort)
                except ValueError:
                    ts = None
                if ts is not None:
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_ET)
                    if ts < effective_threshold:
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
            time_sort = data.get("time_sort", "")
            if time_sort and not self._within_days(time_sort, self._days_back):
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
        model = self.sourceModel()
        today = datetime.now(_ET).date()
        latest: date | None = None
        if model is not None:
            row_data = getattr(model, "row_data", None)
            if row_data is not None:
                for i in range(model.rowCount()):
                    d = row_data(i)
                    if not d:
                        continue
                    ts = d.get("time_sort", "")
                    if not ts:
                        continue
                    try:
                        parsed = datetime.fromisoformat(ts)
                    except ValueError:
                        continue
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=_ET)
                    local = parsed.astimezone(_ET).date()
                    if latest is None or local > latest:
                        latest = local
        # Anchor = today during live trading; but if no fill has happened
        # today yet (weekend / holiday / before session open), roll the
        # "Today" window back to the most recent fill date instead of
        # leaving the panel blank.
        if latest is not None and latest < today:
            self._anchor_date = latest
        else:
            self._anchor_date = today

    def _within_days(self, iso_time: str, days_back: int) -> bool:
        try:
            ts = datetime.fromisoformat(iso_time)
        except ValueError:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_ET)
        local = ts.astimezone(_ET).date()
        cutoff: date = self._anchor_date - timedelta(days=days_back - 1)
        return local >= cutoff
