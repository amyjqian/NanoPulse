"""Strict-"Today" date window with trading-day lookback.

"Today" is always the current ET date — the window never rolls back to
the newest fill's date, so a fresh morning shows an empty panel rather
than yesterday's session.  "Last N days" counts *trading* days (Mon–Fri):
on a Monday, "Last 2 days" reaches back to Friday instead of wasting the
window on the weekend.  Weekend-dated fills (Sunday futures session)
still pass any window whose cutoff precedes them.
"""
from __future__ import annotations

from datetime import date

import pytest

import src.ui.widgets.trades_filter_proxy as tfp
import src.ui.widgets.trades_summary_model as tsm
from src.ui.widgets.trades_filter_proxy import (
    TradesFilterProxy,
    trading_days_cutoff,
)
from src.ui.widgets.trades_model import TradesModel
from src.ui.widgets.trades_summary_model import TradesSummaryModel

_FRI = "2026-05-29T10:00:00"     # Friday
_SUN = "2026-05-31T19:05:00"     # Sunday (futures evening session)
_MON = "2026-06-01T10:00:00"     # Monday


@pytest.fixture
def _monday(monkeypatch):
    """Pin "today" to Monday 2026-06-01 in both modules."""
    pinned = date(2026, 6, 1)
    monkeypatch.setattr(tfp, "today_et", lambda: pinned)
    monkeypatch.setattr(tsm, "today_et", lambda: pinned)
    return pinned


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


class TestTradingDaysCutoff:
    def test_one_day_is_anchor_itself(self):
        assert trading_days_cutoff(date(2026, 6, 1), 1) == date(2026, 6, 1)

    def test_two_days_from_monday_reach_friday(self):
        assert trading_days_cutoff(date(2026, 6, 1), 2) == date(2026, 5, 29)

    def test_midweek_counts_calendar_days(self):
        # Wednesday, 3 trading days → Monday of the same week.
        assert trading_days_cutoff(date(2026, 6, 3), 3) == date(2026, 6, 1)

    def test_seven_days_span_two_weekends(self):
        assert trading_days_cutoff(date(2026, 6, 1), 7) == date(2026, 5, 22)


class TestFlatProxyStrictToday:
    def _proxy(self, qtbot) -> tuple[TradesFilterProxy, TradesModel]:
        model = TradesModel()
        proxy = TradesFilterProxy(is_summary=False)
        proxy.setSourceModel(model)
        return proxy, model

    def test_today_never_rolls_back_to_last_session(self, qtbot, _monday):
        proxy, model = self._proxy(qtbot)
        model.add_fill(_fill("e1", _FRI))
        assert proxy.daysBack() == 1
        assert proxy.rowCount() == 0          # Friday fill ≠ "Today"

    def test_last_2_days_reaches_across_the_weekend(self, qtbot, _monday):
        proxy, model = self._proxy(qtbot)
        model.add_fill(_fill("e1", _FRI))
        model.add_fill(_fill("e2", _MON))
        assert proxy.rowCount() == 1          # Today → Monday fill only
        proxy.setDaysBack(2)
        assert proxy.rowCount() == 2          # Friday + Monday

    def test_sunday_session_fill_passes_the_window(self, qtbot, _monday):
        proxy, model = self._proxy(qtbot)
        model.add_fill(_fill("e1", _SUN, sec_type="FUT", symbol="MNQ",
                             local_symbol="MNQU6", exchange="CME"))
        assert proxy.rowCount() == 0          # Sunday ≠ Monday
        proxy.setDaysBack(2)                  # cutoff = Friday → Sunday passes
        assert proxy.rowCount() == 1

    def test_anchor_date_is_today(self, qtbot, _monday):
        proxy, _model = self._proxy(qtbot)
        assert proxy.anchorDate() == _monday


class TestSummaryPnlWindow:
    def test_pnl_totals_trading_day_lookback(self, _monday):
        # Two open shorts marked to last: Friday's AAA is worth +100
        # unrealized, Monday's BBB +57 — the U element shows which fills
        # the window admits.
        m = TradesSummaryModel()
        m.add_fill(_fill("e1", _FRI, side="SLD"))
        m.add_fill(_fill("e2", _MON, side="SLD", con_id=2, symbol="BBB",
                         local_symbol="BBB"))
        m.set_last_price("AAA", 90.0)     # (100 − 90) × 10 = +100
        m.set_last_price("BBB", 94.3)     # (100 − 94.3) × 10 = +57
        assert m.pnl_totals(days_back=1)[1] == pytest.approx(57.0)
        assert m.pnl_totals(days_back=2)[1] == pytest.approx(157.0)
        assert m.pnl_totals(days_back=0)[1] == pytest.approx(157.0)
