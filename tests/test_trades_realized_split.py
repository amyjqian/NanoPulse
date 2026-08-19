"""Trades summary R/U P&L split — window-relative by design.

Fills walk their own account's book in the order they printed (see
``TradesSummaryModel._walk_account``), seeing NOTHING outside the
selected time window.  A fill that closes lots opened earlier in the
window is a round trip → R.P&L, valued gross from both legs' own
prices.  Anything unmatched — including a fill that really closed a
position opened *before* the window — is an open window leg and marks
to the current price → U.P&L.  So NET 0 always means U.P&L 0, and
P&L = R.P&L + U.P&L always.

IB's ``realized_pnl`` (computed against the account's average cost, a
basis that reaches outside the window) is deliberately NOT folded into
R/U — it stays visible in the separate Rlz P&L column.

Every P&L number the model reports is GROSS; the Comm column deducts
commission exactly once (Net P&L = P&L − Comm).
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


def test_net_zero_window_has_zero_upnl():
    """The headline rule: buys == sells in the window → U.P&L is exactly
    0 and stops moving with the price, even when the sell actually
    closed an overnight position (its IB realized number is ignored by
    R/U — it lives in the Rlz P&L column)."""
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "SLD", 6, 110.0, realized_pnl=57.0))
    m.add_fill(_fill("e2", "BOT", 6, 111.0, t="2026-05-29T11:00:00"))
    m.set_last_price("AAA", 95.0)       # must NOT affect either number
    assert _r(m) == (110.0 - 111.0) * 6
    assert _u(m) == 0.0


def test_close_of_overnight_position_is_a_window_short():
    """Sell 6 bought yesterday: the window can't see the buy, so the
    sell is an open window-short marked against the current price."""
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "SLD", 6, 110.0, realized_pnl=57.0))
    m.set_last_price("AAA", 95.0)
    assert _r(m) == 0.0
    assert _u(m) == (110.0 - 95.0) * 6


def test_naked_sell_marks_to_last():
    """Opening short: R.P&L stays 0 and the leg marks to last —
    profit as the price falls."""
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


def test_partial_pair_remainder_marks_to_current_price():
    """Buy 4 today, sell 10: 4 pair in-window; the unmatched 6 stay an
    open window-short marked to the current price — IB's realized on
    the sell is ignored."""
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "BOT", 4, 100.0))
    m.add_fill(_fill("e2", "SLD", 10, 110.0, realized_pnl=600.0,
                     t="2026-05-29T11:00:00"))
    m.set_last_price("AAA", 105.0)
    assert _r(m) == (110.0 - 100.0) * 4
    assert _u(m) == (110.0 - 105.0) * 6


def test_opening_buys_mark_to_last():
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "BOT", 10, 100.0))
    m.set_last_price("AAA", 105.0)
    assert _r(m) == 0.0
    assert _u(m) == (105.0 - 100.0) * 10


def test_realized_pnl_value_never_changes_window_math():
    """``realized_pnl`` — absent, zero, or huge — must not change R/U:
    the walk is window-relative and ignores it entirely (it still feeds
    the separate Rlz P&L column)."""
    with_rpnl = TradesSummaryModel()
    without = TradesSummaryModel()
    with_rpnl.add_fill(_fill("e1", "SLD", 6, 110.0, realized_pnl=57.0))
    without.add_fill(_fill("e1", "SLD", 6, 110.0))
    with_rpnl.set_last_price("AAA", 108.0)
    without.set_last_price("AAA", 108.0)
    assert _r(with_rpnl) == _r(without) == 0.0
    assert _u(with_rpnl) == _u(without) == (110.0 - 108.0) * 6


class TestPnlTotalsToday:
    """Status-bar aggregate: (R, U, P&L, commission) for today's fills
    by account set, independent of every view filter."""

    def _model(self):
        m = TradesSummaryModel()
        # U111: sold 6 @110, bought 4 back @108 → R +8; the open short 2
        # marks @104 → U +12.
        m.add_fill(_fill("e1", "SLD", 6, 110.0,
                         con_id=1) | {"account_id": "U111"})
        m.add_fill(_fill("e1b", "BOT", 4, 108.0, con_id=1,
                         t="2026-05-29T11:00:00") | {"account_id": "U111"})
        # U222: opened a long, marked to 103 → U +30.
        m.add_fill(_fill("e2", "BOT", 10, 100.0, con_id=2,
                         symbol="BBB") | {"account_id": "U222"})
        m.set_last_price("AAA", 104.0)
        m.set_last_price("BBB", 103.0)
        return m

    def test_all_accounts(self):
        r, u, total, comm = self._model().pnl_totals_today()
        assert r == 8.0
        assert u == 42.0
        assert total == 50.0
        assert comm == 0.0            # no commission reports in-frame

    def test_account_subset(self):
        m = self._model()
        assert m.pnl_totals_today({"U111"}) == (8.0, 12.0, 20.0, 0.0)
        assert m.pnl_totals_today({"U222"}) == (0.0, 30.0, 30.0, 0.0)

    def test_prior_day_fills_excluded(self):
        m = self._model()
        m.add_fill(_fill("e3", "SLD", 99, 500.0, con_id=3, symbol="OLD",
                         t="2026-05-28T10:00:00") | {"account_id": "U111"})
        m.set_last_price("OLD", 480.0)
        assert m.pnl_totals_today() == (8.0, 42.0, 50.0, 0.0)

    def test_independent_of_view_filters(self):
        m = self._model()
        m.set_selected_accounts({"U222"})   # view filtered to one account
        m.set_search_text("zzz-no-match")   # view shows nothing
        assert m.pnl_totals_today() == (8.0, 42.0, 50.0, 0.0)

    def test_hidden_fills_excluded_until_restored(self):
        # A hide is session-level "stop counting this transaction" — it
        # drops out of the status-bar totals too, until restored.  With
        # e1 hidden, U111 is left holding the 4-lot buy-back as an open
        # long: (104 − 108) × 4 = −16.
        m = self._model()
        m.set_hidden_fills({"e1"})
        assert m.pnl_totals_today() == (0.0, 14.0, 14.0, 0.0)
        m.set_hidden_fills(set())           # restore
        assert m.pnl_totals_today() == (8.0, 42.0, 50.0, 0.0)

    def test_empty_model(self):
        assert TradesSummaryModel().pnl_totals_today() == (0.0, 0.0, 0.0, 0.0)

    def test_days_back_widens_the_window(self):
        m = self._model()
        # Yesterday: an open short 1 @500 marked @480 → U +20.
        m.add_fill(_fill("e3", "SLD", 1, 500.0, con_id=3, symbol="OLD",
                         t="2026-05-28T10:00:00") | {"account_id": "U111"})
        m.set_last_price("OLD", 480.0)
        assert m.pnl_totals(days_back=1)[1] == 42.0    # today only
        assert m.pnl_totals(days_back=2)[1] == 62.0    # + yesterday
        assert m.pnl_totals(days_back=0)[1] == 62.0    # 0 = everything

    def test_commission_sums_over_same_fill_set(self):
        m = TradesSummaryModel()
        m.add_fill(_fill("e1", "SLD", 6, 110.0)
                   | {"account_id": "U111", "commission": 1.25})
        m.add_fill(_fill("e2", "BOT", 10, 100.0, con_id=2, symbol="BBB")
                   | {"account_id": "U222", "commission": 0.35})
        # Out-of-window fill's commission must not leak in.
        m.add_fill(_fill("e3", "SLD", 1, 500.0, con_id=3, symbol="OLD",
                         t="2026-05-28T10:00:00")
                   | {"account_id": "U111", "commission": 9.99})
        m.set_last_price("AAA", 104.0)
        m.set_last_price("BBB", 103.0)
        assert m.pnl_totals_today()[3] == pytest.approx(1.60)
        # Account filter applies to the commission element too.
        assert m.pnl_totals_today({"U111"})[3] == pytest.approx(1.25)
        # Gross P&L stays gross — commission is reported, not deducted.
        r, u, total, comm = m.pnl_totals_today()
        assert r == 0.0
        assert u == pytest.approx(66.0)     # (110−104)×6 + (103−100)×10
        assert total == pytest.approx(66.0)
        assert total - comm == pytest.approx(64.40)

    def test_threshold_keeps_fills_at_or_after(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        m = self._model()   # fills at 10:00 / 11:00 ET
        noon = datetime(2026, 5, 29, 12, 0,
                        tzinfo=ZoneInfo("America/New_York"))
        morning = datetime(2026, 5, 29, 9, 30,
                           tzinfo=ZoneInfo("America/New_York"))
        assert m.pnl_totals(threshold=noon) == (0.0, 0.0, 0.0, 0.0)
        assert m.pnl_totals(threshold=morning) == (8.0, 42.0, 50.0, 0.0)


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
    # Morning round trip: +50.
    panel.add_fill(_fill("e1", "BOT", 5, 100.0)
                   | {"account_id": "U111"})               # 10:00 ET
    panel.add_fill(_fill("e2", "SLD", 5, 110.0,
                         t="2026-05-29T10:30:00") | {"account_id": "U111"})
    # Afternoon round trip: +10.
    panel.add_fill(_fill("e3", "BOT", 2, 200.0, con_id=2, symbol="BBB",
                         t="2026-05-29T13:00:00") | {"account_id": "U111"})
    panel.add_fill(_fill("e4", "SLD", 2, 205.0, con_id=2, symbol="BBB",
                         t="2026-05-29T13:30:00") | {"account_id": "U111"})
    assert panel.pnl_totals()[0] == 60.0               # Today (default)
    assert panel.pnl_timeframe_label() == "Today"

    panel._control_bar.setFilterKey("afternoon")       # Noon → 12:00 cutoff
    assert panel.pnl_totals()[0] == 10.0
    assert panel.pnl_timeframe_label() == "Noon"

    panel._control_bar.setFilterKey("morning")         # 09:30 cutoff
    assert panel.pnl_totals()[0] == 60.0
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
    hiding both accounts' real window legs from U.P&L.
    """

    def test_a_sell_in_one_account_never_pairs_a_buy_in_another(self):
        # A sold 1 (a window-short, marked), B opened a long (marked).
        # Pooled books would pair the two into a round trip nobody
        # traded: R −100 / U 0 instead of R 0 / U −120 + 20.
        m = TradesSummaryModel()
        m.add_fill(_fut("a1", "SLD", 30100.0, "10:00:00", "A"))
        m.add_fill(_fut("b1", "BOT", 30150.0, "10:30:00", "B"))
        m.set_last_price("MNQ", 30160.0)

        r, u, _total, _comm = m.pnl_totals_today()
        assert r == 0.0
        assert u == pytest.approx(-100.0)   # A: −120 marked, B: +20 marked

    def test_close_then_reopen_pairs_as_a_window_round_trip(self):
        # Same account: the 10:00 sale really closed an overnight long,
        # but the window can't see that basis — window-relative by
        # design, the sell and the 10:30 buy-back pair into a window
        # round trip.  IB's own realized number (against average cost)
        # stays in the Rlz P&L column.
        m = TradesSummaryModel()
        m.add_fill(_fut("c1", "SLD", 30100.0, "10:00:00", "A",
                        realized_pnl=198.78))
        m.add_fill(_fut("c2", "BOT", 30150.0, "10:30:00", "A"))
        m.set_last_price("MNQ", 30160.0)

        r, u, _total, _comm = m.pnl_totals_today()
        assert r == pytest.approx(-100.0)   # (30100 − 30150) × 2
        assert u == 0.0                     # NET 0 → U.P&L 0

    def test_in_window_round_trip_pairs_gross_from_both_prices(self):
        # Buy then sell: paired gross from both fill prices, IB's
        # realized number ignored; net of the two 0.61 commissions the
        # total lands on IB's 198.78.
        m = TradesSummaryModel()
        m.add_fill(_fut("d1", "BOT", 30000.0, "09:40:00", "A"))
        m.add_fill(_fut("d2", "SLD", 30100.0, "10:00:00", "A",
                        realized_pnl=198.78))
        r, u, total, comm = m.pnl_totals_today()
        assert r == pytest.approx(200.0)       # (30100 − 30000) × 2
        assert u == 0.0
        assert total - comm == pytest.approx(198.78)

    def test_summary_row_splits_per_account_too(self):
        # The row aggregates both accounts, but its R/U come from the
        # per-account walk — not just the status-bar aggregate.
        m = TradesSummaryModel()
        m.add_fill(_fut("a1", "SLD", 30100.0, "10:00:00", "A"))
        m.add_fill(_fut("b1", "BOT", 30150.0, "10:30:00", "B"))
        m.set_last_price("MNQ", 30160.0)
        assert _r(m) == 0.0
        assert _u(m) == pytest.approx(-100.0)

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


class TestCommissionReportedNotDeducted:
    """Every P&L the model reports is GROSS; commission is summed in its
    own element/column and subtracted exactly once by the callers
    (Net P&L = P&L − Comm)."""

    def _net(self, m):
        _r, _u, total, comm = m.pnl_totals_today()
        return total - comm

    def test_round_trip_nets_both_legs_commissions(self):
        m = TradesSummaryModel()
        m.add_fill(_fut("d1", "BOT", 30000.0, "09:40:00", "A"))
        m.add_fill(_fut("d2", "SLD", 30100.0, "10:00:00", "A"))
        assert self._net(m) == pytest.approx(200.0 - 2 * 0.61)

    def test_unmatched_leg_marks_gross_and_nets_its_own_commission(self):
        m = TradesSummaryModel()
        m.add_fill(_fut("d2", "SLD", 30100.0, "10:00:00", "A"))
        m.set_last_price("MNQ", 30160.0)
        assert self._net(m) == pytest.approx(-120.0 - 0.61)


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
    m.add_fill(_fill("e1", "SLD", 6, 110.0, con_id=1))
    m.add_fill(_fill("e2", "BOT", 10, 100.0, con_id=2, symbol="BBB"))
    m.set_last_price("AAA", 104.0)
    m.set_last_price("BBB", 103.0)
    from src.ui.widgets.trades_summary_model import (
        S_COL_NET_TOTAL, S_COL_PAIRED_PNL, S_COL_UNPAIRED_PNL,
    )
    r = m._total_value(S_COL_PAIRED_PNL)
    u = m._total_value(S_COL_UNPAIRED_PNL)
    total = m._total_value(S_COL_NET_TOTAL)
    assert r == 0.0
    assert u == (110.0 - 104.0) * 6 + (103.0 - 100.0) * 10
    assert total == r + u
