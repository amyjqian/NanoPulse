"""Pure-math tests for src/core/percent_sizing.py (spec §4).

Covers the spec acceptance case (25% of $1.42M at 182.40 → 1,946, never
1,947), floor behaviour, degenerate inputs, the >100% clamp and the
buying-power clamp (including the deliberate no-clamp on unknown BP).
"""
from __future__ import annotations

from decimal import Decimal

from src.core.percent_sizing import (
    clamp_to_buying_power,
    resolve_percent_qty,
    sell_shares_from_position,
    shares_from_percent,
)


def test_spec_acceptance_case():
    # 25% × $1,420,000 = $355,000 / 182.40 = 1,946.27… → floor 1,946.
    assert shares_from_percent(25, 1_420_000, Decimal("182.40")) == 1946


def test_floats_and_decimals_agree():
    assert shares_from_percent(25, 1_420_000.0, 182.40) == 1946
    assert shares_from_percent(Decimal(25), Decimal(1_420_000),
                               Decimal("182.40")) == 1946


def test_floors_never_rounds_up():
    # 100% of $100 at $3 → 33.33… must floor to 33.
    assert shares_from_percent(100, 100, 3) == 33
    # Exactly divisible stays exact.
    assert shares_from_percent(100, 99, 3) == 33


def test_degenerate_inputs_resolve_to_zero():
    assert shares_from_percent(25, 1_000_000, 0) == 0       # no price
    assert shares_from_percent(25, 1_000_000, -1) == 0
    assert shares_from_percent(0, 1_000_000, 100) == 0      # pct <= 0
    assert shares_from_percent(-5, 1_000_000, 100) == 0
    assert shares_from_percent(25, 0, 100) == 0             # NLV <= 0
    assert shares_from_percent(25, "garbage", 100) == 0


def test_resolve_clamps_percent_to_100():
    # 250% must behave as 100%, not 2.5× the account.
    assert (resolve_percent_qty(250, 1_000_000, 100)
            == resolve_percent_qty(100, 1_000_000, 100)
            == 10_000)


def test_bp_clamp_boundary():
    # 100 shares @ $10 needs $1,000 BP — exactly enough is not clamped.
    assert clamp_to_buying_power(100, 10, 1_000) == 100
    assert clamp_to_buying_power(100, 10, 999) == 99
    assert clamp_to_buying_power(100, 10, 500) == 50


def test_bp_zero_means_unknown_not_broke():
    # BP tag not yet received must NOT zero the order.
    assert clamp_to_buying_power(100, 10, 0) == 100
    assert clamp_to_buying_power(100, 10, -1) == 100
    assert resolve_percent_qty(100, 10_000, 10, buying_power=0) == 1000


def test_sell_percent_of_position_ceils_then_floors_to_5():
    # SELL sizes against the position: ceil (mirrors close-pct), then a
    # partial sell floors to a multiple of 5.
    assert sell_shares_from_position(25, 600) == 150
    assert sell_shares_from_position(33, 100) == 30   # ceil 33 → floor5
    assert sell_shares_from_position(34, 100) == 30
    # 25% of 601 = 150.25 → ceil 151 → floor5 150
    assert sell_shares_from_position(25, 601) == 150
    # Sub-5 partial slices round to 0 (account skipped by the fan-out).
    assert sell_shares_from_position(10, 3) == 0
    assert sell_shares_from_position(10, 42) == 5     # ceil 4.2 → 5


def test_sell_percent_caps_at_holding():
    assert sell_shares_from_position(100, 600) == 600
    assert sell_shares_from_position(250, 600) == 600     # >100% clamps
    assert sell_shares_from_position(100, 1) == 1
    # Full close keeps the odd lot — 100% never strands 1–4 shares.
    assert sell_shares_from_position(100, 1998) == 1998
    assert sell_shares_from_position(100, 7) == 7


def test_sell_percent_never_shorts():
    assert sell_shares_from_position(25, 0) == 0          # flat
    assert sell_shares_from_position(25, -600) == 0       # short
    assert sell_shares_from_position(0, 600) == 0         # pct <= 0
    assert sell_shares_from_position(-5, 600) == 0
    assert sell_shares_from_position(25, "garbage") == 0


def test_sell_percent_truncates_fractional_position():
    # 10.7 held shares → treated as 10 (int truncate, like the worker).
    assert sell_shares_from_position(100, 10.7) == 10


def test_resolve_full_pipeline():
    # Deployable base = min(NLV, BP): 50% of min($200k, $40k) @ $50 = 400.
    assert resolve_percent_qty(50, 200_000, 50, buying_power=40_000) == 400
    # BP unknown → NLV alone: 50% of $200k @ $50 = 2,000.
    assert resolve_percent_qty(50, 200_000, 50) == 2000
    assert resolve_percent_qty(50, 200_000, 0) == 0


def test_resolve_scales_when_bp_below_nlv():
    # Capital mostly tied up (NLV $249k, BP $22.3k): each percentage
    # must resolve to a DIFFERENT count — under the old NLV base every
    # chip smashed into the BP clamp and read identically.
    kw = dict(net_liq=249_359, price=Decimal("825.82"), buying_power=22_299)
    # Raw counts 2 / 6 / 13 / 27, floored to multiples of 5.
    assert resolve_percent_qty(10, **kw) == 0
    assert resolve_percent_qty(25, **kw) == 5
    assert resolve_percent_qty(50, **kw) == 10
    assert resolve_percent_qty(100, **kw) == 25


def test_resolve_margin_account_keeps_nlv_base():
    # BP above NLV (margin): base stays NLV so 100% is 1× leverage,
    # never a levered position behind a single click.
    assert resolve_percent_qty(100, 100_000, 100,
                               buying_power=400_000) == 1000


def test_resolve_floors_to_multiple_of_5():
    # 25% of $1.42M @ 182.40 = 1,946.27 → floor 1,946 → floor5 1,945.
    assert resolve_percent_qty(25, 1_420_000, Decimal("182.40")) == 1945
    # Already a multiple of 5 → unchanged.
    assert resolve_percent_qty(50, 200_000, 50) == 2000
    # Sub-5 results round to 0 (fan-out skips the account).
    assert resolve_percent_qty(10, 4_000, 100) == 0
