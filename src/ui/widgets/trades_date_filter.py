"""
Trades date filter — the single window type behind every entry in the
control bar's date dropdown.

Before this module the same "is this fill in frame?" question was
answered in three places with three slightly different implementations:
``TradesFilterProxy.filterAcceptsRow``, ``TradesSummaryModel.
_passes_time_filter`` and the ``in_frame`` closure inside
``TradesSummaryModel.pnl_totals``.  Each carried its own
``days_back`` + ``threshold`` pair, its own ``_ts_dt`` fallback parse and
its own idea of what an unparseable timestamp meant, so the flat view,
the summary rows and the status-bar aggregate could drift apart.  They
now all evaluate one :class:`TradeWindow`.

A window is a half-open ET interval ``[start, end)``.  Either bound may
be ``None`` for "unbounded in that direction", which is what lets one
type cover every option:

===================  ==========================  ====================
option               start                       end
===================  ==========================  ====================
Evening              today 18:00                 —
Noon                 today 12:00                 —
Morning              today 09:30                 —
Today                today 00:00                 —
Thu, Aug 20          Aug 20 00:00                Aug 21 00:00
Last N Days          today − (N−1) days 00:00    —
Week to Date         Monday of this week 00:00   —
===================  ==========================  ====================

**Every window counts CALENDAR days, weekends included.**  The previous
implementation counted trading days (Mon–Fri), so "Last 2 Days" on a
Monday reached back to Friday and a weekend fill could not be selected
at all — futures trade Sunday evening, so those fills were unreachable.
"Last 2 Days" on a Monday is now Sunday + Monday, and the recent-day
list names Saturday and Sunday like any other date.

(Weekend skipping still exists in one unrelated place: ``session_day``
in ``trades_summary_model`` rolls a Friday-evening futures print to the
following Monday, because the exchange really is shut for those two
days.  That decides which FIFO book a fill lands in, not whether it is
in frame.)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# How many individual past days the dropdown names ("Thu, Aug 20", …).
# Calendar days, so the list always covers the same span of wall-clock
# time and always includes the weekend when one falls inside it.
RECENT_DAY_COUNT = 6

# Span of the rolling "Last N Days" entries, inclusive of today.
LAST_DAYS_MIN = 2
LAST_DAYS_MAX = 7

# Session-of-day starts, ET.  Morning is the US equities open, Noon the
# midday mark, Evening the CME electronic re-open (also after the US
# cash close, so it covers the evening + overnight book).
_MORNING = (9, 30)
_NOON = (12, 0)
_EVENING = (18, 0)

# Default selection — the whole current day, no time-of-day cutoff.
DEFAULT_KEY = "today"

# Individual-day keys carry their date so the selection survives a combo
# rebuild: ``day:2026-08-20``.
_DAY_KEY_PREFIX = "day:"


def today_et() -> date:
    """Current ET calendar date.  Module-level so tests can pin it."""
    return datetime.now(_ET).date()


def start_of_day(d: date) -> datetime:
    """Midnight ET on *d*."""
    return datetime(d.year, d.month, d.day, tzinfo=_ET)


def _at(d: date, hm: tuple[int, int]) -> datetime:
    return datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=_ET)


def week_start(d: date) -> date:
    """Monday of *d*'s week (ISO: Monday starts the week, Sunday ends
    it, so Sunday looks back six days rather than starting a new week)."""
    return d - timedelta(days=d.weekday())


def day_label(d: date) -> str:
    """A recent-day entry's display text — ``"Thu, Aug 20"``.  Built by
    hand rather than with ``%-d`` / ``%#d``, which are platform-specific
    and would render ``Aug 05`` on the wrong OS."""
    return f"{d:%a, %b} {d.day}"


def day_key(d: date) -> str:
    """Stable itemData for a recent-day entry (``day:2026-08-20``)."""
    return f"{_DAY_KEY_PREFIX}{d.isoformat()}"


def parse_day_key(key: str) -> date | None:
    """The date inside a ``day:`` key, or ``None`` if *key* isn't one."""
    if not key.startswith(_DAY_KEY_PREFIX):
        return None
    try:
        return date.fromisoformat(key[len(_DAY_KEY_PREFIX):])
    except ValueError:
        return None


def fill_dt(data: dict) -> datetime | None:
    """A fill's ET-aware timestamp.

    Prefers the parse-once ``_ts_dt`` stamp ``stamp_fill`` writes onto
    the shared fill dict — re-parsing ``time_sort`` per row per filter
    pass was a measurable cost once a session held thousands of fills.
    ``None`` covers "no timestamp" and "unparseable" alike; every caller
    treats those the same way (the fill stays visible).
    """
    if "_ts_dt" in data:
        return data["_ts_dt"]
    raw = data.get("time_sort", "")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=_ET) if dt.tzinfo is None else dt


@dataclass(frozen=True)
class TradeWindow:
    """A half-open ET interval ``[start, end)`` plus how to name it.

    ``start``/``end`` of ``None`` mean unbounded in that direction, so
    ``TradeWindow.unbounded()`` accepts everything and a session preset
    accepts everything from its cutoff onward.

    ``has_time_of_day`` marks the presets whose start is a clock time
    rather than midnight (Morning / Noon / Evening).  The panel uses it
    to resolve the one collision in the UI: a datetime typed into the
    search box narrows the view, but must not override a session preset
    the user explicitly picked from the combo.
    """

    key: str
    label: str
    start: datetime | None = None
    end: datetime | None = None
    has_time_of_day: bool = False

    # ── Construction ────────────────────────────────────────────────────

    @classmethod
    def unbounded(cls, key: str = "all", label: str = "All") -> TradeWindow:
        return cls(key, label)

    @classmethod
    def session(cls, key: str, label: str, anchor: date,
                hm: tuple[int, int]) -> TradeWindow:
        """Today from a clock time onward (Morning / Noon / Evening)."""
        return cls(key, label, start=_at(anchor, hm), has_time_of_day=True)

    @classmethod
    def single_day(cls, d: date) -> TradeWindow:
        """One calendar day, bounded on both sides.

        Bounded on purpose: "since Aug 19" is what the Last N Days
        entries are for, so a named day means that day alone and the two
        groups complement each other instead of overlapping.
        """
        return cls(day_key(d), day_label(d),
                   start=start_of_day(d),
                   end=start_of_day(d + timedelta(days=1)))

    @classmethod
    def last_days(cls, n: int, anchor: date) -> TradeWindow:
        """The last *n* CALENDAR days ending today — ``n=1`` is today
        alone, ``n=2`` is today and yesterday (Sunday included when
        today is Monday).  ``n <= 0`` is unbounded."""
        n = int(n)
        if n <= 0:
            return cls.unbounded()
        key = DEFAULT_KEY if n == 1 else f"last_{n}"
        label = "Today" if n == 1 else f"Last {n} Days"
        return cls(key, label,
                   start=start_of_day(anchor - timedelta(days=n - 1)))

    @classmethod
    def week_to_date(cls, anchor: date) -> TradeWindow:
        """Monday of the current week through now."""
        return cls("wtd", "Week to Date",
                   start=start_of_day(week_start(anchor)))

    @classmethod
    def from_threshold(cls, dt: datetime) -> TradeWindow:
        """Everything at or after *dt* — how a datetime typed into the
        search box ("Apr21 11:44", "11:44") becomes a window."""
        return cls("search", "Search", start=dt)

    # ── Evaluation ──────────────────────────────────────────────────────

    def contains(self, dt: datetime | None) -> bool:
        """Is *dt* in frame?  A ``None`` timestamp always passes — a fill
        whose time we could not read is shown rather than silently
        dropped, which is what all three predecessors did."""
        if dt is None:
            return True
        if self.start is not None and dt < self.start:
            return False
        if self.end is not None and dt >= self.end:
            return False
        return True

    def contains_fill(self, data: dict) -> bool:
        """``contains`` for a fill dict, via the ``_ts_dt`` stamp."""
        return self.contains(fill_dt(data))

    @property
    def is_unbounded(self) -> bool:
        return self.start is None and self.end is None

    # ``pnl_totals`` memoises on the window, so it has to be hashable and
    # compare by value — which the frozen dataclass already gives us.
    def cache_key(self) -> tuple[Any, ...]:
        return (self.key, self.start, self.end)


def window_for_key(key: str, anchor: date | None = None) -> TradeWindow:
    """Resolve a combo itemData key to its window.

    Unknown keys fall back to Today rather than raising — the key can
    come from a stale persisted selection or from a ``day:`` entry that
    has since rolled off the end of the recent-day list.
    """
    anchor = anchor or today_et()
    if key == "evening":
        return TradeWindow.session("evening", "Evening", anchor, _EVENING)
    # "afternoon" is the pre-refactor key for the same preset; accepted so
    # a selection restored from an older session still resolves.
    if key in ("noon", "afternoon"):
        return TradeWindow.session("noon", "Noon", anchor, _NOON)
    if key == "morning":
        return TradeWindow.session("morning", "Morning", anchor, _MORNING)
    if key == "wtd":
        return TradeWindow.week_to_date(anchor)
    if key == "all":
        return TradeWindow.unbounded()
    d = parse_day_key(key)
    if d is not None:
        return TradeWindow.single_day(d)
    if key.startswith("last_"):
        try:
            return TradeWindow.last_days(int(key[5:]), anchor)
        except ValueError:
            pass
    return TradeWindow.last_days(1, anchor)


def date_filter_groups(anchor: date | None = None) -> list[list[TradeWindow]]:
    """Every dropdown entry, grouped for separator placement.

    Order runs narrowest-to-widest, which is also most- to least-used
    during live trading: the session presets and Today, then the named
    recent days, then the rolling windows, then week-to-date.
    """
    anchor = anchor or today_et()
    sessions = [
        TradeWindow.session("evening", "Evening", anchor, _EVENING),
        TradeWindow.session("noon", "Noon", anchor, _NOON),
        TradeWindow.session("morning", "Morning", anchor, _MORNING),
        TradeWindow.last_days(1, anchor),          # "Today"
    ]
    recent = [
        TradeWindow.single_day(anchor - timedelta(days=i))
        for i in range(1, RECENT_DAY_COUNT + 1)
    ]
    rolling = [
        TradeWindow.last_days(n, anchor)
        for n in range(LAST_DAYS_MIN, LAST_DAYS_MAX + 1)
    ]
    return [sessions, recent, rolling, [TradeWindow.week_to_date(anchor)]]
