"""Trades date filter — one :class:`TradeWindow` behind every dropdown
entry, counting CALENDAR days.

"Today" is strict: the window is always anchored to the current ET date,
so a fresh morning shows an empty panel rather than yesterday's session.

Everything else counts calendar days and **includes weekends**.  The
previous implementation counted trading days (Mon–Fri): "Last 2 Days" on
a Monday reached back to Friday, and a Saturday or Sunday fill could not
be selected on its own at all — which futures traders notice, because the
Sunday evening session is real trading.  Now "Last 2 Days" on a Monday is
Sunday + Monday, and the recent-day list names weekend dates like any
other.

The three views (flat proxy, summary rows, status-bar aggregate) all
evaluate the same window object, so a case proven on one holds on all.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import src.ui.widgets.trades_date_filter as tdf
import src.ui.widgets.trades_filter_proxy as tfp
import src.ui.widgets.trades_summary_model as tsm
from src.ui.widgets.trades_date_filter import (
    LAST_DAYS_MAX,
    LAST_DAYS_MIN,
    RECENT_DAY_COUNT,
    TradeWindow,
    date_filter_groups,
    day_key,
    day_label,
    week_start,
    window_for_key,
)
from src.ui.widgets.trades_filter_proxy import TradesFilterProxy
from src.ui.widgets.trades_model import TradesModel
from src.ui.widgets.trades_summary_model import TradesSummaryModel

_ET = ZoneInfo("America/New_York")

_THU = "2026-05-28T10:00:00"     # Thursday
_FRI = "2026-05-29T10:00:00"     # Friday
_SAT = "2026-05-30T11:00:00"     # Saturday
_SUN = "2026-05-31T19:05:00"     # Sunday (futures evening session)
_MON = "2026-06-01T10:00:00"     # Monday

_MONDAY = date(2026, 6, 1)


@pytest.fixture
def _monday(monkeypatch):
    """Pin "today" to Monday 2026-06-01 everywhere the clock is read."""
    monkeypatch.setattr(tdf, "today_et", lambda: _MONDAY)
    monkeypatch.setattr(tfp, "today_et", lambda: _MONDAY)
    monkeypatch.setattr(tsm, "today_et", lambda: _MONDAY)
    return _MONDAY


def _fill(exec_id, t, **over):
    base = {
        "exec_id": exec_id,
        "con_id": 1,
        "symbol": "AAA",
        "local_symbol": "AAA",
        "sec_type": "STK",
        "side": "BOT",
        "shares": 10,
        "price": 100.0,
        "exchange": "NASDAQ",
        "time_sort": t,
    }
    base.update(over)
    return base


def _et(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=_ET)


class TestLastDaysCountsCalendarDays:
    """The headline change: weekends consume the window like any day."""

    def test_one_day_is_today(self):
        w = TradeWindow.last_days(1, _MONDAY)
        assert w.start == _et(2026, 6, 1)
        assert w.end is None

    def test_two_days_from_monday_reaches_sunday_not_friday(self):
        """Was Friday under trading-day counting."""
        assert TradeWindow.last_days(2, _MONDAY).start == _et(2026, 5, 31)

    def test_three_days_from_monday_reaches_saturday(self):
        assert TradeWindow.last_days(3, _MONDAY).start == _et(2026, 5, 30)

    def test_four_days_from_monday_reaches_friday(self):
        assert TradeWindow.last_days(4, _MONDAY).start == _et(2026, 5, 29)

    def test_seven_days_is_exactly_seven_calendar_days(self):
        w = TradeWindow.last_days(LAST_DAYS_MAX, _MONDAY)
        assert w.start == _et(2026, 5, 26)
        assert (_MONDAY - w.start.date()).days == LAST_DAYS_MAX - 1

    def test_zero_or_negative_is_unbounded(self):
        for n in (0, -3):
            w = TradeWindow.last_days(n, _MONDAY)
            assert w.is_unbounded
            assert w.contains(_et(1999, 1, 1))


class TestSingleDayIsBounded:
    """A named day means that day alone — "since that day" is what the
    Last N Days entries are for."""

    def test_bounds_are_midnight_to_midnight(self):
        w = TradeWindow.single_day(date(2026, 5, 29))
        assert w.start == _et(2026, 5, 29)
        assert w.end == _et(2026, 5, 30)

    def test_excludes_the_following_day(self):
        w = TradeWindow.single_day(date(2026, 5, 29))
        assert w.contains(_et(2026, 5, 29, 23, 59))
        assert not w.contains(_et(2026, 5, 30, 0, 0))
        assert not w.contains(_et(2026, 5, 28, 23, 59))

    def test_weekend_days_are_selectable(self):
        """The whole point of dropping trading-day counting: a Sunday
        futures session is a day you can pick."""
        w = TradeWindow.single_day(date(2026, 5, 31))
        assert w.contains(datetime.fromisoformat(_SUN).replace(tzinfo=_ET))

    def test_key_round_trips(self):
        d = date(2026, 5, 31)
        assert window_for_key(day_key(d)) == TradeWindow.single_day(d)

    def test_label_format(self):
        assert day_label(date(2026, 8, 20)) == "Thu, Aug 20"
        assert day_label(date(2026, 8, 5)) == "Wed, Aug 5"


class TestWeekToDate:
    def test_monday_starts_the_week(self):
        assert week_start(date(2026, 8, 21)) == date(2026, 8, 17)

    def test_on_monday_it_is_just_today(self):
        assert TradeWindow.week_to_date(_MONDAY).start == _et(2026, 6, 1)

    def test_sunday_looks_back_to_the_same_week_monday(self):
        """ISO weeks: Sunday ends the week it belongs to, it doesn't
        start a new one."""
        assert week_start(date(2026, 6, 7)) == date(2026, 6, 1)

    def test_friday_reaches_monday(self):
        assert TradeWindow.week_to_date(date(2026, 8, 21)).start == \
            _et(2026, 8, 17)


class TestSessionPresets:
    @pytest.mark.parametrize("key,hm", [
        ("evening", (18, 0)), ("noon", (12, 0)), ("morning", (9, 30)),
    ])
    def test_start_times(self, key, hm):
        w = window_for_key(key, _MONDAY)
        assert w.start == _et(2026, 6, 1, *hm)
        assert w.end is None
        assert w.has_time_of_day

    def test_legacy_afternoon_key_still_resolves_to_noon(self):
        assert window_for_key("afternoon", _MONDAY) == \
            window_for_key("noon", _MONDAY)

    def test_day_and_span_windows_have_no_time_of_day(self):
        assert not window_for_key("today", _MONDAY).has_time_of_day
        assert not window_for_key("last_3", _MONDAY).has_time_of_day
        assert not window_for_key("wtd", _MONDAY).has_time_of_day
        assert not TradeWindow.single_day(_MONDAY).has_time_of_day


class TestWindowForKey:
    def test_unknown_key_falls_back_to_today(self):
        """A stale persisted key, or a ``day:`` entry that has rolled off
        the list, must not raise."""
        for bad in ("", "nonsense", "last_", "day:not-a-date", "last_x"):
            assert window_for_key(bad, _MONDAY) == \
                TradeWindow.last_days(1, _MONDAY)

    def test_missing_timestamp_always_passes(self):
        """Every predecessor kept un-timestamped fills visible rather
        than dropping them silently."""
        assert window_for_key("morning", _MONDAY).contains(None)
        assert TradeWindow.single_day(_MONDAY).contains_fill({})


class TestDropdownContents:
    """The list the user actually sees, for the worked example: today is
    Friday 2026-08-21."""

    FRIDAY = date(2026, 8, 21)

    def _labels(self):
        return [[w.label for w in g] for g in date_filter_groups(self.FRIDAY)]

    def test_group_order(self):
        sessions, recent, rolling, wtd = self._labels()
        assert sessions == ["Evening", "Noon", "Morning", "Today"]
        assert rolling == [f"Last {n} Days"
                           for n in range(LAST_DAYS_MIN, LAST_DAYS_MAX + 1)]
        assert wtd == ["Week to Date"]

    def test_recent_days_match_the_spec(self):
        """Six calendar days back from Friday Aug 21 — the weekend is in
        the list, not skipped over."""
        _s, recent, _r, _w = self._labels()
        assert recent == [
            "Thu, Aug 20", "Wed, Aug 19", "Tue, Aug 18",
            "Mon, Aug 17", "Sun, Aug 16", "Sat, Aug 15",
        ]
        assert len(recent) == RECENT_DAY_COUNT

    def test_every_key_is_unique_and_resolvable(self):
        for group in date_filter_groups(self.FRIDAY):
            for w in group:
                assert window_for_key(w.key, self.FRIDAY) == w
        keys = [w.key for g in date_filter_groups(self.FRIDAY) for w in g]
        assert len(keys) == len(set(keys))


class TestFlatProxy:
    def _proxy(self) -> tuple[TradesFilterProxy, TradesModel]:
        model = TradesModel()
        proxy = TradesFilterProxy(is_summary=False)
        proxy.setSourceModel(model)
        return proxy, model

    def test_today_never_rolls_back_to_last_session(self, qtbot, _monday):
        proxy, model = self._proxy()
        model.add_fill(_fill("e1", _FRI))
        assert proxy.rowCount() == 0          # Friday fill ≠ "Today"

    def test_last_2_days_from_monday_admits_sunday_not_friday(
            self, qtbot, _monday):
        proxy, model = self._proxy()
        model.add_fill(_fill("e1", _FRI))
        model.add_fill(_fill("e2", _SUN, sec_type="FUT", symbol="MNQ"))
        model.add_fill(_fill("e3", _MON))
        assert proxy.rowCount() == 1                      # Monday only
        proxy.setWindow(TradeWindow.last_days(2, _MONDAY))
        assert proxy.rowCount() == 2                      # + Sunday
        proxy.setWindow(TradeWindow.last_days(4, _MONDAY))
        assert proxy.rowCount() == 3                      # + Friday

    def test_a_named_day_shows_only_that_day(self, qtbot, _monday):
        proxy, model = self._proxy()
        for i, t in enumerate((_THU, _FRI, _SAT, _SUN, _MON)):
            model.add_fill(_fill(f"e{i}", t))
        proxy.setWindow(TradeWindow.single_day(date(2026, 5, 29)))
        assert proxy.rowCount() == 1
        proxy.setWindow(TradeWindow.single_day(date(2026, 5, 30)))
        assert proxy.rowCount() == 1                      # Saturday
        proxy.setWindow(TradeWindow.single_day(date(2026, 5, 27)))
        assert proxy.rowCount() == 0                      # nothing that day

    def test_week_to_date_on_monday_is_today_only(self, qtbot, _monday):
        proxy, model = self._proxy()
        model.add_fill(_fill("e1", _SUN))
        model.add_fill(_fill("e2", _MON))
        proxy.setWindow(TradeWindow.week_to_date(_MONDAY))
        assert proxy.rowCount() == 1

    def test_anchor_date_is_today(self, qtbot, _monday):
        proxy, _model = self._proxy()
        assert proxy.anchorDate() == _monday

    def test_search_datetime_overrides_a_span_window(self, qtbot, _monday):
        proxy, model = self._proxy()
        model.add_fill(_fill("e1", "2026-06-01T09:45:00"))
        model.add_fill(_fill("e2", "2026-06-01T13:20:00"))
        proxy.setWindow(TradeWindow.last_days(4, _MONDAY))
        assert proxy.rowCount() == 2
        proxy.setSearchText("12:00")
        assert proxy.rowCount() == 1
        assert proxy.effectiveWindow().start == _et(2026, 6, 1, 12, 0)

    def test_a_session_preset_outranks_the_search_datetime(
            self, qtbot, _monday):
        """The combo is an explicit choice; a typed time must not
        silently undo it."""
        proxy, model = self._proxy()
        model.add_fill(_fill("e1", "2026-06-01T09:45:00"))
        preset = window_for_key("morning", _MONDAY)
        proxy.setWindow(preset)
        proxy.setSearchText("13:00")
        assert proxy.effectiveWindow() == preset
        assert proxy.rowCount() == 1


class TestSummaryPnlWindow:
    def test_pnl_totals_follows_the_window(self, _monday):
        # Two open shorts marked to last: Friday's AAA is worth +100
        # unrealized, Monday's BBB +57 — the U element shows which fills
        # the window admits.
        m = TradesSummaryModel()
        m.add_fill(_fill("e1", _FRI, side="SLD"))
        m.add_fill(_fill("e2", _MON, side="SLD", con_id=2, symbol="BBB",
                         local_symbol="BBB"))
        m.set_last_price("AAA", 90.0)     # (100 − 90) × 10 = +100
        m.set_last_price("BBB", 94.3)     # (100 − 94.3) × 10 = +57
        u = lambda w: m.pnl_totals(None, w)[1]
        assert u(TradeWindow.last_days(1, _MONDAY)) == pytest.approx(57.0)
        # Friday is FOUR calendar days back from Monday, not two.
        assert u(TradeWindow.last_days(2, _MONDAY)) == pytest.approx(57.0)
        assert u(TradeWindow.last_days(4, _MONDAY)) == pytest.approx(157.0)
        assert u(TradeWindow.unbounded()) == pytest.approx(157.0)
        assert u(TradeWindow.single_day(date(2026, 5, 29))) == \
            pytest.approx(100.0)

    def test_rows_follow_the_window(self, _monday):
        m = TradesSummaryModel()
        m.add_fill(_fill("e1", _FRI))
        m.add_fill(_fill("e2", _MON, con_id=2, symbol="BBB",
                         local_symbol="BBB"))
        m.set_window(TradeWindow.single_day(date(2026, 5, 29)))
        assert [k[0] for k in m._order] == ["cid:1"]
        m.set_window(TradeWindow.last_days(4, _MONDAY))
        assert len(m._order) == 2


class TestControlBarCombo:
    def test_combo_renders_every_group_with_separators(self, qtbot, _monday):
        from src.ui.widgets.trades_control_bar import TradesControlBar
        bar = TradesControlBar()
        qtbot.addWidget(bar)
        combo = bar._date_combo
        groups = date_filter_groups(_MONDAY)
        n_items = sum(len(g) for g in groups)
        assert combo.count() == n_items + (len(groups) - 1)   # + separators
        assert bar.filterKey() == "today"
        assert bar.filterLabel() == "Today"

    def test_selecting_a_named_day_reports_its_key(self, qtbot, _monday):
        from src.ui.widgets.trades_control_bar import TradesControlBar
        bar = TradesControlBar()
        qtbot.addWidget(bar)
        key = day_key(_MONDAY - timedelta(days=2))        # Saturday
        bar.setFilterKey(key)
        assert bar.filterKey() == key
        assert bar.filterLabel() == day_label(_MONDAY - timedelta(days=2))

    def test_rebuild_keeps_a_still_valid_selection(self, qtbot, _monday):
        from src.ui.widgets.trades_control_bar import TradesControlBar
        bar = TradesControlBar()
        qtbot.addWidget(bar)
        bar.setFilterKey("last_3")
        assert bar.rebuildDateOptions() is True
        assert bar.filterKey() == "last_3"

    def test_rebuild_drops_a_day_that_rolled_off_the_list(
            self, qtbot, monkeypatch):
        """The named days ARE dates, so they go stale at midnight.  A
        selection that no longer exists falls back to Today and says so,
        instead of silently filtering on a day the user can't see."""
        from src.ui.widgets.trades_control_bar import TradesControlBar
        monkeypatch.setattr(tdf, "today_et", lambda: _MONDAY)
        bar = TradesControlBar()
        qtbot.addWidget(bar)
        oldest = _MONDAY - timedelta(days=RECENT_DAY_COUNT)
        bar.setFilterKey(day_key(oldest))
        assert bar.filterKey() == day_key(oldest)
        # Clock advances one day; the oldest named day drops off the end.
        monkeypatch.setattr(tdf, "today_et",
                            lambda: _MONDAY + timedelta(days=1))
        assert bar.rebuildDateOptions() is False
        assert bar.filterKey() == "today"
