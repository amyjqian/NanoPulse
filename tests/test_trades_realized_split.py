"""Trades summary R/U P&L split with IB realized P&L on closing fills.

Fills walk their own account's book in the order they printed (see
``TradesSummaryModel._walk_account``).  A fill that closes lots opened
earlier in the window is a round trip, valued gross from both legs' own
prices.  A fill that closes a position opened *outside* the window is
recognised by its nonzero ``realized_pnl`` — stamped by the
commissionReport on every closing execution, computed against the
account's average cost — so it shows IB's true realized number instead
of a fake short marked to the last price.  Anything else is an *opening*
leg and marks to the last price.  P&L = R.P&L + U.P&L always.

Every P&L number the model reports is GROSS: IB's realized arrives net
of both legs' commissions, so the closing fill's own commission is added
back and the Comm column deducts it exactly once.
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
        assert m.pnl_totals_today() == (57.0, 30.0, 87.0, 0.0)

    def test_hidden_fills_excluded_until_restored(self):
        # A hide is session-level "stop counting this transaction" — it
        # drops out of the status-bar totals too, until restored.
        m = self._model()
        m.set_hidden_fills({"e1"})          # hide the realized +57 fill
        assert m.pnl_totals_today() == (0.0, 30.0, 30.0, 0.0)
        m.set_hidden_fills(set())           # restore
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
        # IB's 57.0 is already net of the 1.25 commission, so the gross
        # figure adds it back and NET = P&L − Comm lands on IB's number.
        r, _u, total, comm = m.pnl_totals_today()
        assert r == pytest.approx(58.25)
        assert total == pytest.approx(58.25)
        assert total - comm == pytest.approx(56.65)   # 57.0 − U222's 0.35

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
    # No account-scope caption — the bar always reports every account,
    # so a label would be noise.  Zero renders as a green unsigned 0.
    bar.set_pnl_summary((57.0, 0.0, 57.0))
    html = bar._pnl_lbl.text()
    assert "ALL" not in html and "SEL" not in html
    assert "+57" in html
    assert ">0</span>" in html and "#3FB950" in html
    # Frame label from the trades date combo leads.
    bar.set_pnl_summary((57.0, 30.0, 87.0), frame_label="Noon")
    html = bar._pnl_lbl.text()
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


def _fut(exec_id, side, price, t, account, *, realized_pnl=0.0,
         qty=1, commission=0.61):
    """One MNQ future fill ($2 multiplier) for *account*."""
    return _fill(exec_id, side, qty, price, realized_pnl=realized_pnl,
                 con_id=9, symbol="MNQ", t=f"2026-05-29T{t}") | {
        "sec_type": "FUT",
        "multiplier": 2.0,
        "account_id": account,
        "commission": commission,
    }


class TestPerAccountBooks:
    """Fills pair inside their own account, in the order they printed.

    A summary row aggregates one contract across every account, but P&L
    can't: pooling the books lets one account's close pair against
    another's opening fill, inventing a round trip nobody traded and
    hiding the second account's real position from U.P&L.  Chronology
    matters for the same reason — a fill can only close a lot that was
    already open when it printed.
    """

    def test_close_in_one_account_ignores_an_open_in_another(self):
        # A closes an overnight long: IB realized +198.78, already net of
        # both legs' 0.61 commissions.  B, flat, opens a new long.
        m = TradesSummaryModel()
        m.add_fill(_fut("a1", "SLD", 30100.0, "10:00:00", "A",
                        realized_pnl=198.78))
        m.add_fill(_fut("b1", "BOT", 30150.0, "10:30:00", "B"))
        m.set_last_price("MNQ", 30160.0)

        r, u, total, comm = m.pnl_totals_today()
        # Pooled books would pair the two into a fictitious short round
        # trip: R −100.00, U 0.00.
        assert r == pytest.approx(199.39)      # 198.78 + A's own 0.61
        assert u == pytest.approx(20.0)        # B's open long, marked
        assert total - comm == pytest.approx(218.17)

    def test_close_then_reopen_the_other_way_is_not_a_round_trip(self):
        # Same account, and the same trap: the 10:00 close of an
        # overnight long has nothing in-window to pair with, so the
        # 10:30 opening buy must not be conscripted as its other leg.
        m = TradesSummaryModel()
        m.add_fill(_fut("c1", "SLD", 30100.0, "10:00:00", "A",
                        realized_pnl=198.78))
        m.add_fill(_fut("c2", "BOT", 30150.0, "10:30:00", "A"))
        m.set_last_price("MNQ", 30160.0)

        r, u, total, comm = m.pnl_totals_today()
        assert r == pytest.approx(199.39)
        assert u == pytest.approx(20.0)
        assert total - comm == pytest.approx(218.17)

    def test_in_window_round_trip_still_pairs_in_the_right_order(self):
        # Buy then sell IS a round trip — the walk must still pair it,
        # gross from both fill prices, and ignore IB's realized number.
        m = TradesSummaryModel()
        m.add_fill(_fut("d1", "BOT", 30000.0, "09:40:00", "A"))
        m.add_fill(_fut("d2", "SLD", 30100.0, "10:00:00", "A",
                        realized_pnl=198.78))
        r, u, total, comm = m.pnl_totals_today()
        assert r == pytest.approx(200.0)       # (30100 − 30000) × 2
        assert u == 0.0
        assert total - comm == pytest.approx(198.78)   # == IB's number

    def test_summary_row_splits_per_account_too(self):
        # The row aggregates both accounts, but its R/U come from the
        # per-account walk — not just the status-bar aggregate.
        m = TradesSummaryModel()
        m.add_fill(_fut("a1", "SLD", 30100.0, "10:00:00", "A",
                        realized_pnl=198.78))
        m.add_fill(_fut("b1", "BOT", 30150.0, "10:30:00", "B"))
        m.set_last_price("MNQ", 30160.0)
        assert _r(m) == pytest.approx(199.39)
        assert _u(m) == pytest.approx(20.0)

    def test_arrival_order_does_not_change_the_result(self):
        # Snapshot replay after a reconnect emits fills in worker order,
        # not time order — the walk sorts before pairing.
        forward = TradesSummaryModel()
        backward = TradesSummaryModel()
        rows = [
            _fut("e1", "SLD", 30129.0, "09:50:25", "A"),
            _fut("e2", "BOT", 30123.0, "09:56:28", "A", realized_pnl=10.78),
            _fut("e3", "BOT", 30217.25, "10:53:15", "A"),
        ]
        for f in rows:
            forward.add_fill(f)
        for f in reversed(rows):
            backward.add_fill(f)
        forward.set_last_price("MNQ", 30207.75)
        backward.set_last_price("MNQ", 30207.75)
        assert forward.pnl_totals_today() == backward.pnl_totals_today()
        # One round trip (+12 gross) and one open long, marked.
        r, u, _t, _c = forward.pnl_totals_today()
        assert r == pytest.approx(12.0)
        assert u == pytest.approx(-19.0)


class TestCommissionNettedOnce:
    """IB's ``realized_pnl`` arrives NET of both legs' commissions, so a
    closing fill's own commission is added back to keep every reported
    P&L gross.  The status bar and the Net P&L column then subtract the
    Comm column exactly once."""

    def _net(self, m):
        _r, _u, total, comm = m.pnl_totals_today()
        return total - comm

    def test_net_is_the_same_whether_the_open_leg_is_in_frame(self):
        # Identical economics: bought 30000, sold 30100, $0.61 a side.
        both_legs = TradesSummaryModel()
        both_legs.add_fill(_fut("d1", "BOT", 30000.0, "09:40:00", "A"))
        both_legs.add_fill(_fut("d2", "SLD", 30100.0, "10:00:00", "A",
                                realized_pnl=198.78))
        # Same trade, but the buy printed before the time frame started.
        close_only = TradesSummaryModel()
        close_only.add_fill(_fut("d2", "SLD", 30100.0, "10:00:00", "A",
                                 realized_pnl=198.78))

        assert self._net(both_legs) == pytest.approx(198.78)
        assert self._net(close_only) == pytest.approx(198.78)

    def test_partial_close_prorates_the_commission_add_back(self):
        m = TradesSummaryModel()
        # 4 bought in-window, then 10 sold: 4 pair, 6 close an overnight
        # long.  6/10 of both IB's realized and the fill's commission.
        m.add_fill(_fut("f1", "BOT", 100.0, "10:00:00", "A", qty=4,
                        commission=0.4))
        m.add_fill(_fut("f2", "SLD", 110.0, "11:00:00", "A", qty=10,
                        realized_pnl=600.0, commission=1.0))
        r, _u, _t, _c = m.pnl_totals_today()
        # The paired leg is priced per point (× the $2 multiplier); IB's
        # realized already arrives in dollars, so only its 6/10 share and
        # the matching 6/10 of the sell's commission are added on top.
        assert r == pytest.approx((110.0 - 100.0) * 4 * 2.0
                                  + 600.0 * 0.6 + 1.0 * 0.6)


def test_multi_account_session_reconciles_with_per_account_totals():
    """Regression for the reported bottom-bar figures: four accounts
    trading one MNQ contract through the morning.  The whole-book total
    must equal the sum of the same numbers scoped one account at a
    time — pooled books used to report R −324.72 / U −72.00 because
    maya-02's close was counted twice and alex-04's open long vanished.
    """
    m = TradesSummaryModel()
    rows = [
        ("f11", "SLD", 30129.00, "09:50:25", "alex-01", 0.0),
        ("f12", "SLD", 30129.00, "09:50:25", "maya-02", 0.0),
        ("f09", "BOT", 30123.00, "09:56:28", "maya-02", 10.78),
        ("f10", "BOT", 30123.00, "09:56:28", "alex-01", 10.78),
        ("f07", "SLD", 30167.50, "10:01:59", "maya-02", 0.0),
        ("f08", "SLD", 30167.50, "10:01:59", "alex-01", 0.0),
        ("f04", "BOT", 30225.50, "10:52:48", "alex-01", -117.22),
        ("f05", "BOT", 30225.50, "10:52:48", "alex-04", 0.0),
        ("f06", "BOT", 30225.25, "10:52:48", "maya-02", -116.72),
        ("f03", "BOT", 30224.50, "10:52:49", "maya-03", 0.0),
        ("f01", "BOT", 30217.25, "10:53:15", "alex-01", 0.0),
        ("f02", "BOT", 30217.50, "10:53:15", "maya-02", 0.0),
    ]
    for exec_id, side, price, t, account, rpnl in rows:
        m.add_fill(_fut(exec_id, side, price, t, account, realized_pnl=rpnl))
    m.set_last_price("MNQ", 30207.75)

    accounts = ("alex-01", "maya-02", "maya-03", "alex-04")
    per_account = [m.pnl_totals_today({a}) for a in accounts]
    assert [(r, u) for r, u, _t, _c in per_account] == [
        pytest.approx((-104.0, -19.0)),     # two round trips, open long
        pytest.approx((-103.5, -19.5)),
        pytest.approx((0.0, -33.5)),        # opened only
        pytest.approx((0.0, -35.5)),
    ]

    r, u, total, comm = m.pnl_totals_today()
    assert r == pytest.approx(sum(p[0] for p in per_account))
    assert u == pytest.approx(sum(p[1] for p in per_account))
    assert (r, u) == (pytest.approx(-207.5), pytest.approx(-107.5))
    assert comm == pytest.approx(12 * 0.61)
    assert total - comm == pytest.approx(-322.32)


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
