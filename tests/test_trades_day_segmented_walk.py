"""Per-session-day FIFO segmentation in the Trades summary walk.

The window-relative walk (``TradesSummaryModel._walk_fills``) runs ONE
INDEPENDENT FIFO BOOK PER SESSION DAY, so "Last N Days" is Σ of the N
daily walks rather than one book spanning the window.  Two consequences:

* fills never pair across days, so a close of a pre-window position can
  no longer be "closed" by a genuine new position opened days later —
  the phantom round trip that made multi-day R.P&L meaningless;
* the mirror case — a position genuinely carried between days — has its
  legs in different books, so its profit reports through U.P&L instead.
  The P&L TOTAL is identical either way: R + U depends only on the total
  notionals and the net open quantity, never on how the fills paired.

Day boundaries are trading sessions, not calendar midnight: futures-family
contracts roll at 18:00 ET (``session_day``), so an evening open and the
next morning's close stay in one book.  The date-window FILTER keeps
using the ET calendar date — segmentation decides who shares a book, not
who is admitted.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import src.ui.widgets.trades_filter_proxy as tfp
import src.ui.widgets.trades_summary_model as tsm
from src.ui.widgets.trades_summary_model import TradesSummaryModel, session_day

_ET = ZoneInfo("America/New_York")

# Wed 2026-06-03 is "today"; Mon 06-01 and Tue 06-02 sit inside a
# 3-trading-day window.
_MON = "2026-06-01T10:00:00"
_TUE = "2026-06-02T10:00:00"
_WED = "2026-06-03T10:00:00"


@pytest.fixture(autouse=True)
def _pin_wednesday(monkeypatch):
    pinned = date(2026, 6, 3)
    monkeypatch.setattr(tfp, "today_et", lambda: pinned)
    monkeypatch.setattr(tsm, "today_et", lambda: pinned)
    return pinned


def _fill(exec_id, side, shares, price, t, *, realized_pnl=None,
          sec_type="STK", symbol="AAA", con_id=1, account="U111",
          commission=None):
    d = {
        "exec_id": exec_id,
        "con_id": con_id,
        "symbol": symbol,
        "local_symbol": symbol,
        "sec_type": sec_type,
        "side": side,
        "shares": shares,
        "price": price,
        "exchange": "NASDAQ",
        "time_sort": t,
        "account_id": account,
    }
    if realized_pnl is not None:
        d["realized_pnl"] = realized_pnl
    if commission is not None:
        d["commission"] = commission
    return d


def _model(*fills, last=None, days_back=3):
    m = TradesSummaryModel()
    m.set_days_back(days_back)
    for f in fills:
        m.add_fill(f)
    if last is not None:
        m.set_last_price("AAA", last)
    return m


def _ru(m, idx=0):
    """(R.P&L, U.P&L) for one summary row."""
    key = m._order[idx]
    return (m._realized_total(key),
            m._unrealized_total(key, m._effective_agg(key)))


class TestSessionDay:
    """``session_day`` — the boundary the walk segments on."""

    def _dt(self, y, mo, d, h, mi=0):
        return datetime(y, mo, d, h, mi, tzinfo=_ET)

    def test_futures_evening_belongs_to_the_next_trading_date(self):
        assert session_day(self._dt(2026, 6, 1, 18, 30), "FUT") == \
            date(2026, 6, 2)

    def test_futures_before_six_pm_stays_put(self):
        assert session_day(self._dt(2026, 6, 1, 17, 59), "FUT") == \
            date(2026, 6, 1)

    def test_sunday_evening_session_is_monday(self):
        assert session_day(self._dt(2026, 5, 31, 19, 5), "FUT") == \
            date(2026, 6, 1)

    def test_friday_evening_edge_skips_the_weekend(self):
        assert session_day(self._dt(2026, 5, 29, 18, 30), "FUT") == \
            date(2026, 6, 1)

    def test_futures_options_roll_with_the_underlying(self):
        assert session_day(self._dt(2026, 6, 1, 20, 0), "FOP") == \
            date(2026, 6, 2)

    def test_stocks_never_roll(self):
        """An 18:00 rollover would split extended-hours equity trading,
        which belongs to the day it printed on."""
        assert session_day(self._dt(2026, 6, 1, 19, 30), "STK") == \
            date(2026, 6, 1)
        assert session_day(self._dt(2026, 6, 1, 19, 30), "OPT") == \
            date(2026, 6, 1)

    def test_no_timestamp_is_no_session(self):
        assert session_day(None, "FUT") is None

    def test_stamped_onto_the_fill(self):
        f = _fill("e1", "BOT", 1, 100.0, "2026-06-01T18:30:00",
                  sec_type="FUT")
        tsm.stamp_fill(f)
        assert f["_ts_date"] == date(2026, 6, 1)      # filter uses this
        assert f["_ts_session"] == date(2026, 6, 2)   # the walk uses this


class TestNoCrossDayPairing:
    def test_pre_window_close_no_longer_pairs_with_a_later_day(self):
        """The headline fix.  A long carried in from before the window is
        sold Monday; Wednesday opens a genuine new long.  The two used to
        pair into a phantom −$100 round trip — now Monday's sell and
        Wednesday's buy sit in their own day books."""
        m = _model(
            _fill("e1", "SLD", 1, 20000.0, _MON, realized_pnl=2000.0),
            _fill("e2", "BOT", 1, 20050.0, _WED),
            last=20080.0,
        )
        r, u = _ru(m)
        assert r == 0.0                     # was (20000−20050) = −50
        # Mon short marks (20000−20080) = −80, Wed long
        # (20080−20050) = +30.
        assert u == pytest.approx(-50.0)
        assert r + u == pytest.approx(-50.0)

    def test_each_day_realizes_its_own_round_trip(self):
        """Two self-contained day trades: both realize, and R is their
        sum — the number "Last 3 Days" should have shown all along."""
        m = _model(
            _fill("e1", "BOT", 10, 100.0, _MON),
            _fill("e2", "SLD", 10, 103.0, "2026-06-01T14:00:00"),
            _fill("e3", "BOT", 10, 105.0, _WED),
            _fill("e4", "SLD", 10, 109.0, "2026-06-03T14:00:00"),
            last=200.0,          # must not reach either number
        )
        r, u = _ru(m)
        assert r == pytest.approx(30.0 + 40.0)
        assert u == 0.0

    def test_one_day_window_is_unchanged(self):
        """Segmentation is invisible at days_back=1 — there was only ever
        one day in the book."""
        m = _model(
            _fill("e1", "BOT", 4, 100.0, _WED),
            _fill("e2", "SLD", 10, 110.0, "2026-06-03T14:00:00"),
            last=105.0, days_back=1,
        )
        r, u = _ru(m)
        assert r == pytest.approx((110.0 - 100.0) * 4)
        assert u == pytest.approx((110.0 - 105.0) * 6)


class TestPnlTotalIsInvariant:
    """R + U telescopes to (total sells − total buys) + net × last, so
    repartitioning fills between the paired and open halves can move
    value between the R and U columns but never change P&L."""

    def _fills(self):
        return [
            _fill("e1", "BOT", 5, 100.0, _MON),
            _fill("e2", "SLD", 3, 110.0, _TUE),
            _fill("e3", "SLD", 4, 108.0, _WED),
            _fill("e4", "BOT", 2, 101.0, "2026-06-03T14:00:00"),
        ]

    def test_total_matches_the_closed_form(self):
        m = _model(*self._fills(), last=105.0)
        r, u = _ru(m)
        buys = 5 * 100.0 + 2 * 101.0
        sells = 3 * 110.0 + 4 * 108.0
        net = (5 + 2) - (3 + 4)
        assert r + u == pytest.approx(sells - buys + net * 105.0)

    def test_total_is_the_same_at_every_window_width(self):
        totals = []
        for days in (3, 5, 0):
            m = _model(*self._fills(), last=105.0, days_back=days)
            r, u = _ru(m)
            totals.append(r + u)
        assert totals[0] == pytest.approx(totals[1]) == pytest.approx(
            totals[2])


class TestCarriedPositionKeepsItsValue:
    def test_cross_day_round_trip_reports_through_upnl(self):
        """Bought Monday, sold Wednesday: not a day trade, so the legs
        land in different books.  The leftover long and leftover short
        cancel in the net quantity — no mark is applied and their
        notionals net to the real +$300."""
        m = _model(
            _fill("e1", "BOT", 100, 50.0, _MON),
            _fill("e2", "SLD", 100, 53.0, _WED),
            last=99.0,          # irrelevant: net is flat
        )
        r, u = _ru(m)
        assert r == 0.0
        assert u == pytest.approx(300.0)
        assert r + u == pytest.approx(300.0)

    def test_futures_overnight_session_stays_one_day_trade(self):
        """MNQ opened Monday 18:30 ET and closed Tuesday 09:00 ET is one
        session — it must still realize, or the fix would split every
        overnight futures trade."""
        m = _model(
            _fill("e1", "BOT", 2, 20000.0, "2026-06-01T18:30:00",
                  sec_type="FUT", symbol="MNQ") | {"multiplier": 2},
            _fill("e2", "SLD", 2, 20050.0, "2026-06-02T09:00:00",
                  sec_type="FUT", symbol="MNQ") | {"multiplier": 2},
        )
        r, u = _ru(m)
        assert r == pytest.approx((20050.0 - 20000.0) * 2 * 2)
        assert u == 0.0

    def test_same_two_prints_on_a_stock_are_two_days(self):
        """Identical timestamps, sec_type STK — no 18:00 rollover, so the
        legs split.  Total still holds."""
        m = _model(
            _fill("e1", "BOT", 2, 20000.0, "2026-06-01T18:30:00"),
            _fill("e2", "SLD", 2, 20050.0, "2026-06-02T09:00:00"),
        )
        r, u = _ru(m)
        assert r == 0.0
        assert u == pytest.approx((20050.0 - 20000.0) * 2)


class TestPerAccountBooksStillSeparate:
    def test_segmentation_does_not_pool_accounts(self):
        """Same day, same contract, two accounts: U111 day-trades it,
        U222 only opens.  Day segmentation must not merge them."""
        m = _model(
            _fill("e1", "BOT", 10, 100.0, _WED, account="U111"),
            _fill("e2", "SLD", 10, 104.0, "2026-06-03T11:00:00",
                  account="U111"),
            _fill("e3", "BOT", 10, 100.0, _WED, account="U222"),
            last=102.0,
        )
        r, u = _ru(m)
        assert r == pytest.approx(40.0)      # U111's round trip only
        assert u == pytest.approx(20.0)      # U222's open long


class TestStatusBarTotals:
    def test_pnl_totals_uses_the_segmented_walk(self):
        """The status-bar aggregate shares ``_walk_fills``, so it reports
        the same R/U split as the rows."""
        m = _model(
            _fill("e1", "SLD", 1, 20000.0, _MON, realized_pnl=2000.0),
            _fill("e2", "BOT", 1, 20050.0, _WED),
            last=20080.0,
        )
        r, u, total, _c = m.pnl_totals(days_back=3)
        assert r == 0.0
        assert u == pytest.approx(-50.0)
        assert total == pytest.approx(-50.0)


class TestUnsegmentedConsumersUnchanged:
    def test_match_buy_sell_still_pairs_across_the_whole_window(self):
        """"Match buy & sell" means "pair the legs across the window and
        drop the remainder", so it reads the unsegmented ``ws_`` walk —
        Net goes flat and R.P&L is the whole-window pairing."""
        m = _model(
            _fill("e1", "BOT", 10, 100.0, _MON),
            _fill("e2", "SLD", 4, 110.0, _WED),
            last=90.0,
        )
        key = m._order[0]
        m.match_symbol(key)
        assert m.is_matched(key)
        eff = m._effective_agg(key)
        assert eff["buys_qty"] == eff["sells_qty"] == 4
        r, u = _ru(m)
        assert r == pytest.approx((110.0 - 100.0) * 4)
        assert u == 0.0

    def test_account_level_section_is_untouched_by_segmentation(self):
        """The cost-basis walk takes IB's avg-cost realized whole on each
        closing fill — day-independent by construction, so a multi-day
        window sums the days exactly."""
        m = _model(
            _fill("e1", "SLD", 1, 20000.0, _MON, realized_pnl=2000.0),
            _fill("e2", "SLD", 1, 20100.0, _WED, realized_pnl=500.0),
            last=19000.0,
        )
        assert m._cb_realized_total(m._order[0]) == pytest.approx(2500.0)
