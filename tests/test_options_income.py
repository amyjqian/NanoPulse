"""Options-income domain logic — pricing (Decimal), eligibility, expiry
organisation and strike selection.

The pricing cases mirror the reference mockup: NVDA spot 182.40, basis
164.20, Aug-14 (17 DTE) 187.50 CALL @ 3.10.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from src.core import options_income as oi


# ── Pricing ──────────────────────────────────────────────────────────────────

def test_credit():
    assert oi.credit("3.10", 1) == Decimal("310.00")
    assert oi.credit("3.10", 2) == Decimal("620.00")


def test_break_even_cc_matches_mockup():
    assert oi.break_even_cc("164.20", "3.10") == Decimal("161.10")


def test_break_even_csp():
    assert oi.break_even_csp("187.50", "3.10") == Decimal("184.40")


def test_if_called_away_matches_mockup():
    # (187.50 − 164.20) × 100 + 310 = 2,640
    assert oi.if_called_away("187.50", "164.20", "3.10", 1) == \
        Decimal("2640.00")


def test_if_assigned_cost():
    assert oi.if_assigned_cost("187.50", "3.10", 2) == Decimal("36880.00")


def test_yield_and_annualized_match_mockup():
    y = oi.yield_pct("3.10", "182.40")
    assert y == Decimal("1.70")
    assert oi.annualized_pct(y, 17) == Decimal("36.5")


def test_annualized_zero_dte_is_zero():
    assert oi.annualized_pct(Decimal("1.70"), 0) == Decimal(0)


def test_snap_to_tick():
    assert oi.snap_to_tick("3.117", "0.05") == Decimal("3.10")
    assert oi.snap_to_tick("3.13", "0.05") == Decimal("3.15")
    assert oi.snap_to_tick("3.1", "0.01") == Decimal("3.10")


# ── Eligibility / capacity ───────────────────────────────────────────────────

def test_cc_capacity():
    assert oi.cc_capacity(300) == 3
    assert oi.cc_capacity(99) == 0
    assert oi.cc_capacity(None) == 0


def test_csp_capacity():
    assert oi.csp_capacity(2_523_356, 187.5) == 134
    assert oi.csp_capacity(25_000, 250) == 1
    assert oi.csp_capacity(0, 100) == 0
    assert oi.csp_capacity(10_000, 0) == 0


def test_cc_status_badges():
    ok = oi.cc_status(300)
    assert ok.eligible and ok.badge == "ELIGIBLE"
    bad = oi.cc_status(99)
    assert not bad.eligible
    assert bad.badge == "NEEDS 100 SH"
    assert bad.reason == "Not enough shares"


def test_csp_status_badges():
    assert oi.csp_status(2_523_356).eligible
    assert not oi.csp_status(0).eligible
    assert not oi.csp_status(1_000_000, is_margin=False).eligible


# ── Expiries ─────────────────────────────────────────────────────────────────

def _ymd(base: date, days: int) -> str:
    return (base + timedelta(days=days)).strftime("%Y%m%d")


def test_organize_expirations_chips_and_more():
    today = date(2026, 7, 28)
    listed = [_ymd(today, d) for d in (-7, 3, 10, 17, 24, 45, 80)]
    chips, more = oi.organize_expirations(listed, today)
    assert [e.dte for e in chips] == [3, 10, 17, 24]   # past one skipped
    assert [e.dte for e in more] == [45, 80]
    assert chips[2].chip_label == "Aug 14"
    assert "(45d)" in more[0].long_label


def test_organize_expirations_chips_are_week_ending():
    # Tue 2026-07-28; QQQ-style chain with Mon/Wed dailies mixed in.
    # Chips are one per week — the LAST expiration of each week, i.e.
    # the Fridays (Jul 31, Aug 7, 14, 21); dailies stay in More.
    today = date(2026, 7, 28)
    listed = [_ymd(today, d) for d in (1, 3, 6, 8, 10, 17, 24, 31, 45, 80)]
    chips, more = oi.organize_expirations(listed, today)
    assert [e.chip_label for e in chips] == \
        ["Jul 31", "Aug 7", "Aug 14", "Aug 21"]
    assert [e.dte for e in more] == [1, 6, 8, 31, 45, 80]


def test_organize_expirations_holiday_week_uses_thursday():
    # A week whose last listed expiration is Thursday (Friday holiday)
    # still yields exactly one chip for that week.
    today = date(2026, 7, 28)
    listed = ["20260730", "20260807", "20260814", "20260821"]   # Thu + 3 Fri
    chips, more = oi.organize_expirations(listed, today)
    assert [e.chip_label for e in chips] == \
        ["Jul 30", "Aug 7", "Aug 14", "Aug 21"]
    assert more == []


def test_organize_expirations_empty():
    chips, more = oi.organize_expirations([], date.today())
    assert chips == [] and more == []


# ── Strikes ──────────────────────────────────────────────────────────────────

_STRIKES = [170.0, 172.5, 175.0, 177.5, 180.0, 182.5, 185.0, 187.5,
            190.0, 192.5, 195.0, 197.5, 200.0]


def test_pick_strikes_calls_otm_and_itm_windows():
    # 4 nearest OTM calls at/above spot + 2 nearest ITM below, high → low.
    picked = oi.pick_strikes(_STRIKES, 182.40, "C", n=4, n_itm=2)
    assert picked == [190.0, 187.5, 185.0, 182.5, 180.0, 177.5]


def test_pick_strikes_puts_otm_and_itm_windows():
    # 4 nearest OTM puts at/below spot + 2 nearest ITM above, high → low.
    picked = oi.pick_strikes(_STRIKES, 182.40, "P", n=4, n_itm=2)
    assert picked == [185.0, 182.5, 180.0, 177.5, 175.0, 172.5]


def test_pick_strikes_defaults_cover_both_sides():
    # Default 50 OTM + 50 ITM windows swallow this 13-strike chain whole.
    picked = oi.pick_strikes(_STRIKES, 182.40, "C")
    assert picked == sorted(_STRIKES, reverse=True)


def test_pick_strikes_default_window_50_per_side():
    strikes = [2.5 * i for i in range(1, 161)]        # 2.5 … 400
    picked = oi.pick_strikes(strikes, 182.40, "C")
    assert len(picked) == 100
    assert sum(1 for s in picked if s >= 182.40) == 50
    assert sum(1 for s in picked if s < 182.40) == 50


def test_pick_strikes_pads_with_itm_when_otm_short():
    # Only 2 OTM calls above 196 → the ITM side fills the shortfall.
    picked = oi.pick_strikes(_STRIKES, 196.0, "C", n=4, n_itm=2)
    assert picked == [200.0, 197.5, 195.0, 192.5, 190.0, 187.5]


def test_pick_strikes_pads_with_otm_when_itm_short():
    # Only 2 ITM calls below 173 → the OTM side fills the shortfall.
    picked = oi.pick_strikes(_STRIKES, 173.0, "C", n=4, n_itm=4)
    assert picked == [187.5, 185.0, 182.5, 180.0, 177.5, 175.0,
                      172.5, 170.0]


def test_bs_delta_roundtrip_atm():
    # Invert a premium generated at a known vol; ATM 30d call delta ≈ 0.53.
    prem = oi.bs_price(100.0, 100.0, 30 / 365, 0.30, "C")
    d = oi.bs_delta(100.0, 100.0, 30, prem, "C")
    assert 0.50 < d < 0.56
    prem_p = oi.bs_price(100.0, 100.0, 30 / 365, 0.30, "P")
    dp = oi.bs_delta(100.0, 100.0, 30, prem_p, "P")
    assert -0.50 < dp < -0.42


def test_bs_delta_monotonic_across_strikes():
    lo = oi.bs_delta(100.0, 90.0, 30,
                     oi.bs_price(100.0, 90.0, 30 / 365, 0.30, "C"), "C")
    hi = oi.bs_delta(100.0, 110.0, 30,
                     oi.bs_price(100.0, 110.0, 30 / 365, 0.30, "C"), "C")
    assert lo > hi > 0


def test_bs_delta_degenerate_inputs():
    assert oi.bs_delta(0, 100, 30, 1.0, "C") == 0.0
    assert oi.bs_delta(100, 100, 0, 1.0, "C") == 0.0
    assert oi.bs_delta(100, 100, 30, 0.0, "C") == 0.0
    # Premium at parity → deep ITM.
    assert oi.bs_delta(150.0, 100.0, 30, 50.0, "C") == 1.0


def test_moneyness_tags():
    assert oi.moneyness(187.5, 182.4, "C") == "OTM"
    assert oi.moneyness(180.0, 182.4, "C") == "ITM"
    assert oi.moneyness(180.0, 182.4, "P") == "OTM"
    assert oi.moneyness(185.0, 182.4, "P") == "ITM"
