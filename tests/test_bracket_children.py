"""Tests for the bracket child-order builder in ib_worker.IBWorkerThread.

The helper `_build_bracket_children` is exercised in isolation by stubbing
out the IB client's `getReqId()` (monotonic counter) and the parent order
(a plain namespace with the fields the helper reads).  No TWS connection
or PySide dependency is required.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.core.ib_worker import IBWorkerThread, _round_to_tick


# ═══════════════════════════════════════════════════════════════════════════════
#  _round_to_tick
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoundToTick:
    def test_quarter_tick(self):
        assert _round_to_tick(17502.13, 0.25) == 17502.25
        assert _round_to_tick(17502.12, 0.25) == 17502.00

    def test_penny_tick(self):
        assert _round_to_tick(150.006, 0.01) == 150.01

    def test_zero_tick_returns_rounded_price(self):
        assert _round_to_tick(100.12345, 0) == 100.1235


# ═══════════════════════════════════════════════════════════════════════════════
#  _build_bracket_children — fixtures
# ═══════════════════════════════════════════════════════════════════════════════

class _FakeClient:
    """Stub for `self._ib.client` — monotonic `getReqId`."""

    def __init__(self, start: int = 1000) -> None:
        self._next = start

    def getReqId(self) -> int:
        self._next += 1
        return self._next


def _make_worker() -> IBWorkerThread:
    """Build a bare IBWorkerThread that bypasses the QThread `__init__`.

    `_build_bracket_children` only touches `self._ib.client.getReqId` and
    `self._label`, so stubbing those fields is enough.
    """
    wt = IBWorkerThread.__new__(IBWorkerThread)
    wt._ib = SimpleNamespace(client=_FakeClient())
    wt._label = "test"
    return wt


def _parent(action="BUY", outside_rth=False, allow_pre_open=False):
    """Build a parent-order stand-in.  The helper reads:
    action, totalQuantity, account, tif, orderRef, outsideRth, allowPreOpen,
    orderId.
    """
    return SimpleNamespace(
        action=action,
        totalQuantity=2,
        account="DU1234",
        tif="DAY",
        orderRef="nano_0419_120000_abcd",
        outsideRth=outside_rth,
        allowPreOpen=allow_pre_open,
        orderId=500,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Core assertions
# ═══════════════════════════════════════════════════════════════════════════════

class TestBracketChildren:
    def test_no_tiers_returns_empty(self):
        wt = _make_worker()
        out = wt._build_bracket_children(_parent(), params={"sec_type": "FUT"},
                                         ref_price=17500.0, tick=0.25)
        assert out == []

    def test_buy_parent_tp_offset_is_above_ref(self):
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "profit_take": {"value": 20, "unit": "ticks"},  # 20*0.25 = 5 pts
        }
        out = wt._build_bracket_children(_parent("BUY"), params,
                                         ref_price=17500.0, tick=0.25)
        assert len(out) == 1
        tp = out[0]
        assert tp.action == "SELL"
        assert tp.orderType == "LMT"
        assert tp.lmtPrice == 17505.00
        assert tp.parentId == 500
        # Sole child transmits the bundle.
        assert tp.transmit is True

    def test_sell_parent_tp_offset_is_below_ref(self):
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "profit_take": {"value": 8, "unit": "price"},  # 8 points
        }
        out = wt._build_bracket_children(_parent("SELL"), params,
                                         ref_price=17500.0, tick=0.25)
        assert out[0].action == "BUY"
        assert out[0].lmtPrice == 17492.00

    def test_sell_parent_sl_offset_is_above_ref(self):
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "stop_loss": {"value": 10, "unit": "price"},
        }
        out = wt._build_bracket_children(_parent("SELL"), params,
                                         ref_price=17500.0, tick=0.25)
        sl = out[0]
        assert sl.action == "BUY"
        assert sl.orderType == "STP"
        assert sl.auxPrice == 17510.00  # StopOrder puts stop price in auxPrice

    def test_sl_with_cap_becomes_stop_limit_buy_parent(self):
        """BUY parent + cap_offset → SELL STP LMT with aux > lmt.

        Chip offset maps to the stop TRIGGER (auxPrice) so what TWS
        displays as the stop matches what the user configured.  The
        limit sits cap_offset further below the stop to bound the
        execution slippage after the stop fires.
        """
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "stop_loss": {"value": 20, "unit": "ticks"},  # 5 pts
            "limit_cap_offset": 1.0,                       # 1 pt cap
        }
        out = wt._build_bracket_children(_parent("BUY"), params,
                                         ref_price=17500.0, tick=0.25)
        sl = out[0]
        assert sl.orderType == "STP LMT"
        assert sl.action == "SELL"
        assert sl.auxPrice == 17495.00    # ref - 5 (user's stop = chip offset)
        assert sl.lmtPrice == 17494.00    # stop - cap (limit below stop)

    def test_sl_with_cap_becomes_stop_limit_sell_parent(self):
        """SELL parent + cap_offset → BUY STP LMT with aux < lmt."""
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "stop_loss": {"value": 20, "unit": "ticks"},
            "limit_cap_offset": 1.0,
        }
        out = wt._build_bracket_children(_parent("SELL"), params,
                                         ref_price=17500.0, tick=0.25)
        sl = out[0]
        assert sl.orderType == "STP LMT"
        assert sl.action == "BUY"
        assert sl.auxPrice == 17505.00    # ref + 5 (user's stop = chip offset)
        assert sl.lmtPrice == 17506.00    # stop + cap (limit above stop)

    def test_sl_cap_zero_falls_back_to_plain_stp(self):
        """Explicit cap=0 behaves the same as no cap — plain STP."""
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "stop_loss": {"value": 20, "unit": "ticks"},
            "limit_cap_offset": 0.0,
        }
        out = wt._build_bracket_children(_parent("BUY"), params,
                                         ref_price=17500.0, tick=0.25)
        assert out[0].orderType == "STP"

    def test_sl_cap_snaps_to_tick_grid(self):
        """aux/lmt spread must stay tick-aligned even if cap is off-grid."""
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "stop_loss": {"value": 20, "unit": "ticks"},
            "limit_cap_offset": 0.30,  # 1.2 ticks → snaps to 1 tick
        }
        out = wt._build_bracket_children(_parent("BUY"), params,
                                         ref_price=17500.0, tick=0.25)
        sl = out[0]
        assert sl.auxPrice == 17495.00  # ref - 5 (stop trigger)
        assert sl.lmtPrice == 17494.75  # stop - 0.30 rounded to 0.25 grid

    def test_trailing_uses_unsigned_offset(self):
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "trailing": {"value": 12, "unit": "ticks"},  # 12*0.25 = 3 pts
        }
        out = wt._build_bracket_children(_parent("BUY"), params,
                                         ref_price=17500.0, tick=0.25)
        tr = out[0]
        assert tr.orderType == "TRAIL"
        assert tr.action == "SELL"
        assert tr.auxPrice == 3.00

    def test_all_three_transmit_flags_only_last_true(self):
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "profit_take": {"value": 20, "unit": "ticks"},
            "stop_loss":   {"value": 10, "unit": "ticks"},
            "trailing":    {"value": 5,  "unit": "ticks"},
        }
        out = wt._build_bracket_children(_parent("BUY"), params,
                                         ref_price=17500.0, tick=0.25)
        assert len(out) == 3
        assert [c.transmit for c in out] == [False, False, True]

    def test_parentid_linkage_and_unique_orderids(self):
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "profit_take": {"value": 20, "unit": "ticks"},
            "stop_loss":   {"value": 10, "unit": "ticks"},
        }
        out = wt._build_bracket_children(_parent("BUY"), params,
                                         ref_price=17500.0, tick=0.25)
        assert all(c.parentId == 500 for c in out)
        order_ids = [c.orderId for c in out]
        assert len(set(order_ids)) == len(order_ids)  # unique

    def test_no_explicit_oca_group(self):
        """Attached children (shared parentId) are implicitly OCA'd by IB
        at the bracket level.  Setting an explicit ocaGroup on top of that
        is redundant and is rejected by IBKR as 'OCA handling method
        mismatch' (error 201) — confirmed in paper-account testing.  The
        helper must therefore leave ocaGroup unset on every child."""
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "profit_take": {"value": 20, "unit": "ticks"},
            "stop_loss":   {"value": 10, "unit": "ticks"},
            "trailing":    {"value": 5,  "unit": "ticks"},
        }
        out = wt._build_bracket_children(_parent(), params,
                                         ref_price=17500.0, tick=0.25)
        for ch in out:
            assert getattr(ch, "ocaGroup", "") == ""
            assert getattr(ch, "ocaType", 0) == 0

    def test_stk_inherits_parent_rth_flags(self):
        wt = _make_worker()
        params = {
            "sec_type": "STK",
            "profit_take": {"value": 5, "unit": "ticks"},
        }
        out = wt._build_bracket_children(
            _parent(outside_rth=True, allow_pre_open=True),
            params, ref_price=150.00, tick=0.01)
        assert out[0].outsideRth is True
        assert out[0].allowPreOpen is True

    def test_fut_does_not_set_rth_flags(self):
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "profit_take": {"value": 20, "unit": "ticks"},
        }
        out = wt._build_bracket_children(
            _parent(outside_rth=True, allow_pre_open=True),
            params, ref_price=17500.0, tick=0.25)
        # Futures shouldn't have STK-only RTH flags set by the helper.
        assert not hasattr(out[0], "outsideRth") or out[0].outsideRth is False

    def test_negative_tp_price_is_skipped_not_raised(self):
        wt = _make_worker()
        # SELL + huge TP offset → price goes negative → skip that child.
        params = {
            "sec_type": "FUT",
            "profit_take": {"value": 20000, "unit": "price"},
            "stop_loss":   {"value": 10,    "unit": "ticks"},
        }
        out = wt._build_bracket_children(_parent("SELL"), params,
                                         ref_price=100.0, tick=0.25)
        # TP was skipped; SL survives as the sole child and must transmit.
        assert len(out) == 1
        assert out[0].orderType == "STP"
        assert out[0].transmit is True

    def test_custom_price_unit_is_treated_as_points(self):
        """'price' unit means points — _TierSection uses dist*multiplier to
        compute dollars, which only makes sense if the value is already in
        points.  This test pins that contract."""
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "profit_take": {"value": 7.25, "unit": "price"},  # 7.25 pts
        }
        out = wt._build_bracket_children(_parent("BUY"), params,
                                         ref_price=17500.0, tick=0.25)
        assert out[0].lmtPrice == 17507.25

    def test_rounding_snaps_to_tick_grid(self):
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "profit_take": {"value": 5.13, "unit": "price"},  # off-grid
        }
        out = wt._build_bracket_children(_parent("BUY"), params,
                                         ref_price=17500.0, tick=0.25)
        # 17500 + 5.13 = 17505.13 → snaps to 17505.25
        assert out[0].lmtPrice == 17505.25

    def test_chip_label_is_direct_points_offset(self):
        """Tier chips (loaded via ReferenceData._extract_tiers or
        _enrich_pref_tiers) carry unit='price' because the stored offset
        is already in price points — the StrategyEditor computes
        ``val = offset × multiplier`` which only makes sense if offset
        is in points.  Chip label N therefore means "N points" for TP,
        SL and Trail alike — no tick_size multiplication anywhere."""
        wt = _make_worker()
        tier = {"value": 5, "unit": "price", "label": "5"}
        params = {
            "sec_type": "FUT",
            "profit_take": tier,
            "stop_loss":   tier,
            "trailing":    tier,
        }

        # BUY parent at 26695.
        out = wt._build_bracket_children(_parent("BUY"), params,
                                         ref_price=26695.0, tick=0.25)
        tp = next(o for o in out if o.orderType == "LMT")
        sl = next(o for o in out if o.orderType == "STP")
        tr = next(o for o in out if o.orderType == "TRAIL")
        assert tp.lmtPrice == 26700.00  # 26695 + 5
        assert sl.auxPrice == 26690.00  # 26695 - 5
        assert tr.auxPrice == 5.00      # trailing distance = 5 pts

        # SELL parent mirrors TP/SL; trail distance is directionless.
        out = wt._build_bracket_children(_parent("SELL"), params,
                                         ref_price=26695.0, tick=0.25)
        tp = next(o for o in out if o.orderType == "LMT")
        sl = next(o for o in out if o.orderType == "STP")
        tr = next(o for o in out if o.orderType == "TRAIL")
        assert tp.lmtPrice == 26690.00  # 26695 - 5
        assert sl.auxPrice == 26700.00  # 26695 + 5
        assert tr.auxPrice == 5.00
