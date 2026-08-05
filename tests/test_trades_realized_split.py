"""Trades summary R/U P&L split with IB realized P&L on closing fills.

In-window buys and sells FIFO-pair as before.  An unpaired fill that
carries IB's ``realized_pnl`` (stamped by the commissionReport on every
execution that closes a position — computed against the account's
average cost) is a *closing* fill: its P&L goes to R.P&L directly from
IB, so selling a position bought outside the window shows the true
realized number instead of a fake short marked to the last price.  An
unpaired fill without ``realized_pnl`` is an *opening* leg and is marked
to the last price as before.  P&L = R.P&L + U.P&L always.
"""
from __future__ import annotations

import pytest

from src.ui.widgets.trades_summary_model import TradesSummaryModel

_DAY = "2026-05-29T10:00:00"

@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    """The date filter is strict-today (trading-day lookback), so pin
    "today" to the fixture fills' date — otherwise every fill would fall
    outside the default 1-day window whenever the suite runs later."""
    from datetime import date

    import src.ui.widgets.trades_filter_proxy as tfp
    import src.ui.widgets.trades_summary_model as tsm
    pinned = date(2026, 5, 29)          # the Friday the fills are dated
    monkeypatch.setattr(tfp, "today_et", lambda: pinned)
    monkeypatch.setattr(tsm, "today_et", lambda: pinned)




def _fill(exec_id, side, shares, price, *, realized_pnl=None, con_id=1,
          symbol="AAA", t=_DAY):
    d = {
        "exec_id": exec_id,
        "con_id": con_id,
        "symbol": symbol,
        "local_symbol": symbol,
        "sec_type": "STK",
        "side": side,
        "shares": shares,
        "price": price,
        "exchange": "NASDAQ",
        "time_sort": t,
    }
    if realized_pnl is not None:
        d["realized_pnl"] = realized_pnl
    return d


def _r(m, row=0):
    return m._realized_total(m.row_key(row))


def _u(m, row=0):
    key = m.row_key(row)
    return m._unrealized_total(key, m._effective_agg(key))


def test_close_of_overnight_position_uses_ib_realized():
    """Sell 6 bought yesterday: no in-window pair, IB realized wins.
    Nothing is open, so U.P&L is 0 even while the price keeps moving."""
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "SLD", 6, 110.0, realized_pnl=57.0))
    m.set_last_price("AAA", 95.0)  # must NOT affect either number
    assert _r(m) == 57.0
    assert _u(m) == 0.0


def test_naked_sell_marks_to_last():
    """Opening short (no realized_pnl on the fill): R.P&L stays 0 and
    the leg marks to last — profit as the price falls."""
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "SLD", 5, 110.0))
    m.set_last_price("AAA", 104.0)
    assert _r(m) == 0.0
    assert _u(m) == (110.0 - 104.0) * 5


def test_in_window_round_trip_not_double_counted():
    """A sell consumed by FIFO pairing must not ALSO contribute its IB
    realized_pnl — the round trip is already counted by the pairing."""
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "BOT", 5, 100.0))
    m.add_fill(_fill("e2", "SLD", 5, 110.0, realized_pnl=50.0,
                     t="2026-05-29T11:00:00"))
    assert _r(m) == 50.0   # (110 − 100) × 5 from pairing, not 100.0
    assert _u(m) == 0.0


def test_partially_paired_closing_fill_prorates_ib_realized():
    """Buy 4 today, sell 10 (6 of which closed an overnight long):
    4 pair in-window; the unpaired 6/10 of the sell prorates the fill's
    IB realized_pnl."""
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "BOT", 4, 100.0))
    m.add_fill(_fill("e2", "SLD", 10, 110.0, realized_pnl=600.0,
                     t="2026-05-29T11:00:00"))
    assert _r(m) == (110.0 - 100.0) * 4 + 600.0 * (6 / 10)
    assert _u(m) == 0.0


def test_opening_buys_mark_to_last():
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "BOT", 10, 100.0))
    m.set_last_price("AAA", 105.0)
    assert _r(m) == 0.0
    assert _u(m) == (105.0 - 100.0) * 10


def test_opening_fill_with_zero_realized_still_marks():
    """Live commissionReports stamp realized_pnl = 0.0 on OPENING fills
    too — a zero must not classify the leg as a $0 close, or U.P&L
    would sit at 0.00 forever."""
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "BOT", 10, 100.0, realized_pnl=0.0))
    m.set_last_price("AAA", 105.0)
    assert _r(m) == 0.0
    assert _u(m) == (105.0 - 100.0) * 10


def test_closing_fill_without_report_yet_falls_back_to_marking():
    """Until the commissionReport lands, a closing sell is
    indistinguishable from a naked short and marks to last; once the
    re-emitted fill carries realized_pnl it flips to R.P&L."""
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "SLD", 6, 110.0))          # report not yet in
    m.set_last_price("AAA", 108.0)
    assert _r(m) == 0.0
    assert _u(m) == (110.0 - 108.0) * 6
    m.add_fill(_fill("e1", "SLD", 6, 110.0, realized_pnl=57.0))
    assert _r(m) == 57.0
    assert _u(m) == 0.0


class TestPnlTotalsToday:
    """Status-bar aggregate: (R, U, P&L, commission) for today's fills
    by account set, independent of every view filter."""

    def _model(self):
        m = TradesSummaryModel()
        # U111: closed an overnight long (IB realized +57).
        m.add_fill(_fill("e1", "SLD", 6, 110.0, realized_pnl=57.0,
                         con_id=1) | {"account_id": "U111"})
        # U222: opened a long, marked to 103.
        m.add_fill(_fill("e2", "BOT", 10, 100.0, con_id=2,
                         symbol="BBB") | {"account_id": "U222"})
        m.set_last_price("BBB", 103.0)
        return m

    def test_all_accounts(self):
        r, u, total, comm = self._model().pnl_totals_today()
        assert r == 57.0
        assert u == 30.0
        assert total == 87.0
        assert comm == 0.0            # no commission reports in-frame

    def test_account_subset(self):
        m = self._model()
        assert m.pnl_totals_today({"U111"}) == (57.0, 0.0, 57.0, 0.0)
        assert m.pnl_totals_today({"U222"}) == (0.0, 30.0, 30.0, 0.0)

    def test_prior_day_fills_excluded(self):
        m = self._model()
        m.add_fill(_fill("e3", "SLD", 99, 500.0, realized_pnl=999.0,
                         con_id=3, symbol="OLD",
                         t="2026-05-28T10:00:00") | {"account_id": "U111"})
        assert m.pnl_totals_today()[0] == 57.0  # yesterday's 999 ignored

    def test_independent_of_view_filters(self):
        m = self._model()
        m.set_selected_accounts({"U222"})   # view filtered to one account
        m.set_search_text("zzz-no-match")   # view shows nothing
        m.set_hidden_fills({"e1"})          # e1 hidden in the view
        assert m.pnl_totals_today() == (57.0, 30.0, 87.0, 0.0)

    def test_empty_model(self):
        assert TradesSummaryModel().pnl_totals_today() == (0.0, 0.0, 0.0, 0.0)

    def test_days_back_widens_the_window(self):
        m = self._model()
        m.add_fill(_fill("e3", "SLD", 1, 500.0, realized_pnl=100.0,
                         con_id=3, symbol="OLD",
                         t="2026-05-28T10:00:00") | {"account_id": "U111"})
        assert m.pnl_totals(days_back=1)[0] == 57.0    # today only
        assert m.pnl_totals(days_back=2)[0] == 157.0   # + yesterday
        assert m.pnl_totals(days_back=0)[0] == 157.0   # 0 = everything

    def test_commission_sums_over_same_fill_set(self):
        m = TradesSummaryModel()
        m.add_fill(_fill("e1", "SLD", 6, 110.0, realized_pnl=57.0)
                   | {"account_id": "U111", "commission": 1.25})
        m.add_fill(_fill("e2", "BOT", 10, 100.0, con_id=2, symbol="BBB")
                   | {"account_id": "U222", "commission": 0.35})
        # Out-of-window fill's commission must not leak in.
        m.add_fill(_fill("e3", "SLD", 1, 500.0, con_id=3, symbol="OLD",
                         t="2026-05-28T10:00:00")
                   | {"account_id": "U111", "commission": 9.99})
        assert m.pnl_totals_today()[3] == pytest.approx(1.60)
        # Account filter applies to the commission element too.
        assert m.pnl_totals_today({"U111"})[3] == pytest.approx(1.25)
        # Gross P&L stays gross — commission is reported, not deducted.
        assert m.pnl_totals_today()[2] == 57.0

    def test_threshold_keeps_fills_at_or_after(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        m = self._model()   # both fills at 10:00 ET
        noon = datetime(2026, 5, 29, 12, 0,
                        tzinfo=ZoneInfo("America/New_York"))
        morning = datetime(2026, 5, 29, 9, 30,
                           tzinfo=ZoneInfo("America/New_York"))
        assert m.pnl_totals(threshold=noon) == (0.0, 0.0, 0.0, 0.0)
        assert m.pnl_totals(threshold=morning) == (57.0, 30.0, 87.0, 0.0)


def test_status_bar_pnl_display(qtbot):
    from src.ui.widgets.statusbar import StatusBar
    bar = StatusBar()
    qtbot.addWidget(bar)
    # Default caption is SEL; zero renders as a green unsigned 0.
    bar.set_pnl_summary((57.0, 0.0, 57.0))
    html = bar._pnl_lbl.text()
    assert "SEL" in html and "+57" in html
    assert ">0</span>" in html and "#3FB950" in html
    # Active group name replaces the generic SEL caption (upper-cased,
    # HTML-escaped); frame label from the trades date combo leads.
    bar.set_pnl_summary((57.0, 30.0, 87.0), label="Gamma <1>",
                        frame_label="Noon")
    html = bar._pnl_lbl.text()
    assert "GAMMA &lt;1&gt;" in html and "SEL" not in html
    assert "NOON" in html
    # 3-tuples render no commission segments — gross P&L is the total.
    assert "NET" not in html
    assert "P&amp;L" in html and "+87" in html
    # 4th element: C (neutral, cents) + NET (total − C, colored) REPLACE
    # the gross P&L segment — one total on the bar, the net one.
    bar.set_pnl_summary((57.0, 30.0, 87.0, 12.34))
    html = bar._pnl_lbl.text()
    assert "12.34" in html                          # commission, with cents
    assert "NET" in html and "+75" in html          # 87 − 12.34 → +75
    assert "P&amp;L" not in html and "+87" not in html
    bar.set_pnl_summary(None)                       # hidden entirely
    assert bar._pnl_lbl.text() == ""
    assert bar._pnl_sep.text() == ""


def test_panel_pnl_follows_date_combo(qtbot):
    """The status-bar aggregate follows the Trades control bar's time
    frame: Morning/Noon/Evening thresholds and Last-N-Days windows."""
    from src.ui.widgets.trades_panel import TradesPanel
    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel.add_fill(_fill("e1", "SLD", 6, 110.0, realized_pnl=57.0)
                   | {"account_id": "U111"})           # 10:00 ET
    panel.add_fill(_fill("e2", "SLD", 1, 100.0, realized_pnl=10.0,
                         con_id=2, symbol="BBB",
                         t="2026-05-29T13:00:00") | {"account_id": "U111"})
    assert panel.pnl_totals()[0] == 67.0               # Today (default)
    assert panel.pnl_timeframe_label() == "Today"

    panel._control_bar.setFilterKey("afternoon")       # Noon → 12:00 cutoff
    assert panel.pnl_totals()[0] == 10.0
    assert panel.pnl_timeframe_label() == "Noon"

    panel._control_bar.setFilterKey("morning")         # 09:30 cutoff
    assert panel.pnl_totals()[0] == 67.0
    assert panel.pnl_timeframe_label() == "Morning"


def test_footer_reconciles_r_plus_u():
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "SLD", 6, 110.0, realized_pnl=57.0, con_id=1))
    m.add_fill(_fill("e2", "BOT", 10, 100.0, con_id=2, symbol="BBB"))
    m.set_last_price("BBB", 103.0)
    from src.ui.widgets.trades_summary_model import (
        S_COL_NET_TOTAL, S_COL_PAIRED_PNL, S_COL_UNPAIRED_PNL,
    )
    r = m._total_value(S_COL_PAIRED_PNL)
    u = m._total_value(S_COL_UNPAIRED_PNL)
    total = m._total_value(S_COL_NET_TOTAL)
    assert r == 57.0
    assert u == (103.0 - 100.0) * 10
    assert total == r + u
