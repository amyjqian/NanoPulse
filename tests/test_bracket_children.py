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

    def test_fut_forces_outside_rth_on_children(self):
        """Futures: STP / STP LMT / TRAIL children sit dormant outside
        RTH unless outsideRth=True is set explicitly.  Pin that the
        helper forces the flag on for FUT bracket children regardless
        of what the parent carries.  STK path is unaffected (see the
        STK-specific test above)."""
        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "profit_take": {"value": 20, "unit": "ticks"},
            "stop_loss":   {"value": 10, "unit": "ticks"},
            "trailing":    {"value": 5,  "unit": "ticks"},
        }
        out = wt._build_bracket_children(
            _parent(outside_rth=False, allow_pre_open=False),
            params, ref_price=17500.0, tick=0.25)
        for ch in out:
            if ch.orderType == "LMT":
                # Plain LMT children are exempt: CME ignores the flag
                # for them and emits Warning 2109, which the blotter
                # renders as a scary ValidationError.
                assert not getattr(ch, "outsideRth", False)
            else:
                assert ch.outsideRth is True
            # allowPreOpen is STK-only; FUT path shouldn't touch it.
            assert not getattr(ch, "allowPreOpen", False)

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

    def test_blotter_params_overlay_child_identity(self):
        """Regression for blotter showing parent's BUY action on every child.

        The worker emits order-status dicts keyed on a `params` dict.  When
        reused for a child, that dict must be overlaid with the child's own
        action / qty / prices — otherwise the blotter renders all three
        rows as BUY (or SELL) of the parent's type."""
        from src.core.ib_worker import _IB_DBL_MAX

        wt = _make_worker()
        params = {
            "sec_type": "FUT",
            "action": "BUY", "qty": 1, "order_type": "LMT",
            "limit_price": 26688.75,
            "profit_take": {"value": 5,  "unit": "price"},
            "stop_loss":   {"value": 15, "unit": "price"},
        }
        children = wt._build_bracket_children(_parent("BUY"), params,
                                              ref_price=26688.75, tick=0.25)

        def _overlay(ch):
            cp = dict(params)
            cp["order_type"] = ch.orderType
            cp["action"]     = ch.action
            cp["qty"]        = int(ch.totalQuantity)
            lmt = getattr(ch, "lmtPrice", None)
            aux = getattr(ch, "auxPrice", None)
            if ch.orderType == "LMT" and lmt and lmt < _IB_DBL_MAX:
                cp["limit_price"] = lmt
            else:
                cp.pop("limit_price", None)
            if ch.orderType in ("STP", "STP LMT") and aux and aux < _IB_DBL_MAX:
                cp["stop_price"] = aux
                if ch.orderType == "STP LMT" and lmt and lmt < _IB_DBL_MAX:
                    cp["limit_price"] = lmt
            else:
                cp.pop("stop_price", None)
            return cp

        tp_params = _overlay(next(c for c in children
                                  if c.orderType == "LMT"))
        sl_params = _overlay(next(c for c in children
                                  if c.orderType == "STP"))
        assert tp_params["action"] == "SELL"
        assert sl_params["action"] == "SELL"
        assert tp_params["limit_price"] == 26693.75
        assert "stop_price" not in tp_params
        assert sl_params["stop_price"] == 26673.75
        assert "limit_price" not in sl_params

    def test_chip_label_is_direct_points_offset(self):
        """Chip label N means N points for TP, SL, and Trail alike —
        no tick_size multiplication."""
        wt = _make_worker()
        tier = {"value": 5, "unit": "price", "label": "5"}
        params = {
            "sec_type": "FUT",
            "profit_take": tier,
            "stop_loss":   tier,
            "trailing":    tier,
        }
        out = wt._build_bracket_children(_parent("BUY"), params,
                                         ref_price=26695.0, tick=0.25)
        tp = next(o for o in out if o.orderType == "LMT")
        sl = next(o for o in out if o.orderType == "STP")
        tr = next(o for o in out if o.orderType == "TRAIL")
        assert tp.lmtPrice == 26700.00  # 26695 + 5
        assert sl.auxPrice == 26690.00  # 26695 - 5
        assert tr.auxPrice == 5.00      # trailing distance = 5 pts

        out = wt._build_bracket_children(_parent("SELL"), params,
                                         ref_price=26695.0, tick=0.25)
        tp = next(o for o in out if o.orderType == "LMT")
        sl = next(o for o in out if o.orderType == "STP")
        tr = next(o for o in out if o.orderType == "TRAIL")
        assert tp.lmtPrice == 26690.00  # 26695 - 5
        assert sl.auxPrice == 26700.00  # 26695 + 5
        assert tr.auxPrice == 5.00


# ═══════════════════════════════════════════════════════════════════════════════
#  close_tier — Take Profit / Stop Loss header-button path
# ═══════════════════════════════════════════════════════════════════════════════

import asyncio as _asyncio


def _make_position(symbol: str, secType: str, conId: int,
                   net_pos: float, account: str,
                   *, minTick: float = 0.25):
    contract = SimpleNamespace(
        symbol=symbol, localSymbol=f"{symbol}M6",
        secType=secType, exchange="CME", currency="USD",
        conId=conId, minTick=minTick,
    )
    return SimpleNamespace(
        contract=contract, position=net_pos, account=account,
    )


class _FakeIBForCloseTier:
    def __init__(self, positions):
        self._positions = positions
        self.placed: list = []

    def isConnected(self): return True
    def positions(self): return self._positions

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        trade = SimpleNamespace(
            contract=contract, order=order,
            orderStatus=SimpleNamespace(
                status="PendingSubmit", filled=0.0,
                avgFillPrice=0.0, remaining=order.totalQuantity),
            fills=[], log=[],
        )

        class _Event:
            def __iadd__(self, h): return self
        trade.statusEvent = _Event()
        return trade


def _make_close_tier_worker(positions):
    wt = IBWorkerThread.__new__(IBWorkerThread)
    wt._ib = _FakeIBForCloseTier(positions)
    wt._label = "close-tier-test"
    wt._trades = {}
    wt._has_status_handler = set()
    wt._order_times = {}
    wt._order_error_messages = {}
    wt._portfolio_cache = {}
    wt._emit_order_status = lambda *a, **kw: None
    return wt


def _run(coro):
    return _asyncio.get_event_loop().run_until_complete(coro) \
        if False else _asyncio.run(coro)


class TestCloseTier:
    def test_tp_long_position_submits_sell_limit_above_ref(self):
        pos = _make_position("MNQ", "FUT", 111, net_pos=3,
                             account="DU1", minTick=0.25)
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier(
            symbol="MNQ", account="DU1", side="tp",
            offset_pts=10.0, requested_qty=5,
            current_price=26697.0, cap_offset=0.0,
        ))
        assert len(wt._ib.placed) == 1
        _, order = wt._ib.placed[0]
        assert order.orderType == "LMT"
        assert order.action == "SELL"
        assert order.totalQuantity == 3     # min(|pos|=3, req=5)
        assert order.lmtPrice == 26707.00   # 26697 + 10

    def test_tp_long_caps_qty_at_position_size(self):
        """If position is smaller than requested_qty, close only the pos."""
        pos = _make_position("MNQ", "FUT", 111, net_pos=2, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "tp", 10.0, 5, 26697.0, 0.0))
        assert wt._ib.placed[0][1].totalQuantity == 2

    def test_tp_long_uses_requested_qty_when_position_is_larger(self):
        """Scale-out: if pos is bigger than req, close only req, leave rest."""
        pos = _make_position("MNQ", "FUT", 111, net_pos=10, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "tp", 10.0, 3, 26697.0, 0.0))
        assert wt._ib.placed[0][1].totalQuantity == 3

    def test_tp_short_position_submits_buy_limit_below_ref(self):
        pos = _make_position("MNQ", "FUT", 111, net_pos=-4, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "tp", 10.0, 10, 26697.0, 0.0))
        _, order = wt._ib.placed[0]
        assert order.action == "BUY"
        assert order.totalQuantity == 4
        assert order.lmtPrice == 26687.00   # 26697 - 10 (profit on short)

    def test_sl_long_plain_stop_when_no_cap(self):
        pos = _make_position("MNQ", "FUT", 111, net_pos=3, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "sl", 10.0, 5, 26697.0, 0.0))
        _, order = wt._ib.placed[0]
        assert order.orderType == "STP"
        assert order.action == "SELL"
        assert order.auxPrice == 26687.00   # 26697 - 10

    def test_sl_long_stop_limit_with_cap(self):
        pos = _make_position("MNQ", "FUT", 111, net_pos=3, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "sl", 10.0, 5, 26697.0, 1.0))
        _, order = wt._ib.placed[0]
        assert order.orderType == "STP LMT"
        assert order.auxPrice == 26687.00       # stop = ref - offset
        assert order.lmtPrice == 26686.00       # lmt = stop - cap

    def test_sl_short_stop_above_ref(self):
        pos = _make_position("MNQ", "FUT", 111, net_pos=-3, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "sl", 10.0, 5, 26697.0, 0.0))
        _, order = wt._ib.placed[0]
        assert order.action == "BUY"
        assert order.auxPrice == 26707.00   # short SL is above ref

    def test_zero_position_submits_no_order(self):
        """Critical: we must never send an empty/phantom order to TWS."""
        pos = _make_position("MNQ", "FUT", 111, net_pos=0, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "tp", 10.0, 5, 26697.0, 0.0))
        assert wt._ib.placed == []

    def test_other_account_positions_are_ignored(self):
        """Only the targeted account's position counts."""
        mine = _make_position("MNQ", "FUT", 111, net_pos=2, account="DU1")
        other = _make_position("MNQ", "FUT", 111, net_pos=5, account="DU2")
        wt = _make_close_tier_worker([mine, other])
        _run(wt._do_close_tier("MNQ", "DU1", "tp", 10.0, 9, 26697.0, 0.0))
        assert wt._ib.placed[0][1].totalQuantity == 2  # not 5

    def test_symbol_mismatch_is_ignored(self):
        pos = _make_position("NQ", "FUT", 111, net_pos=3, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "tp", 10.0, 5, 26697.0, 0.0))
        assert wt._ib.placed == []

    def test_non_positive_offset_skipped(self):
        pos = _make_position("MNQ", "FUT", 111, net_pos=3, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "tp", 0.0, 5, 26697.0, 0.0))
        assert wt._ib.placed == []

    def test_prices_snap_to_min_tick(self):
        """Contract.minTick governs the tick grid, even if off-grid offset."""
        pos = _make_position("MNQ", "FUT", 111, net_pos=1,
                             account="DU1", minTick=0.25)
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "tp", 5.13, 1, 26697.0, 0.0))
        # 26697 + 5.13 = 26702.13 → snaps to 26702.25
        assert wt._ib.placed[0][1].lmtPrice == 26702.25

    def test_fut_close_tier_orders_force_outside_rth(self):
        """FUT close-tier exits (TP LMT and SL STP/STP LMT) must carry
        outsideRth=True so CME holds the stop active overnight.  Stocks
        keep their time-of-day logic."""
        pos = _make_position("MNQ", "FUT", 111, net_pos=2, account="DU1")
        for side in ("tp", "sl"):
            wt = _make_close_tier_worker([pos])
            _run(wt._do_close_tier("MNQ", "DU1", side, 10.0, 5, 26697.0, 0.0))
            order = wt._ib.placed[0][1]
            assert order.outsideRth is True, \
                f"{side}: expected outsideRth=True on FUT exit, got False"


# ═══════════════════════════════════════════════════════════════════════════════
#  close_tier — Trailing (TS) header-button path
# ═══════════════════════════════════════════════════════════════════════════════

class TestCloseTierTrailing:
    def test_tr_long_submits_sell_trail_with_distance(self):
        pos = _make_position("MNQ", "FUT", 111, net_pos=3,
                             account="DU1", minTick=0.25)
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier(
            symbol="MNQ", account="DU1", side="tr",
            offset_pts=0.5, requested_qty=5,
            current_price=26697.0, cap_offset=0.0,
        ))
        assert len(wt._ib.placed) == 1
        _, order = wt._ib.placed[0]
        assert order.orderType == "TRAIL"
        assert order.action == "SELL"
        assert order.totalQuantity == 3       # min(|pos|=3, req=5)
        assert order.auxPrice == 0.5          # trail DISTANCE, not a level

    def test_tr_short_submits_buy_trail_same_unsigned_distance(self):
        pos = _make_position("MNQ", "FUT", 111, net_pos=-4, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "tr", 0.5, 10, 26697.0, 0.0))
        _, order = wt._ib.placed[0]
        assert order.action == "BUY"
        assert order.totalQuantity == 4
        assert order.auxPrice == 0.5          # unsigned either direction

    def test_tr_distance_snaps_to_tick(self):
        pos = _make_position("MNQ", "FUT", 111, net_pos=1,
                             account="DU1", minTick=0.25)
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "tr", 0.63, 1, 26697.0, 0.0))
        assert wt._ib.placed[0][1].auxPrice == 0.75

    def test_tr_flat_account_places_nothing(self):
        """Parity with tp/sl: a zero-position account is silently
        skipped — no phantom trailing stop."""
        pos = _make_position("MNQ", "FUT", 111, net_pos=0, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "tr", 0.5, 5, 26697.0, 0.0))
        assert wt._ib.placed == []

    def test_tr_cap_offset_ignored(self):
        """cap_offset shapes SL's STP LMT only — a trail must stay a
        plain TRAIL regardless."""
        pos = _make_position("MNQ", "FUT", 111, net_pos=2, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "tr", 0.5, 2, 26697.0, 1.0))
        assert wt._ib.placed[0][1].orderType == "TRAIL"

    def test_tr_fut_forces_outside_rth(self):
        pos = _make_position("MNQ", "FUT", 111, net_pos=2, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "tr", 0.5, 5, 26697.0, 0.0))
        assert wt._ib.placed[0][1].outsideRth is True

    def test_tr_status_params_carry_distance_and_type(self):
        pos = _make_position("MNQ", "FUT", 111, net_pos=2, account="DU1")
        wt = _make_close_tier_worker([pos])
        seen = []
        wt._emit_order_status = (
            lambda trade, oid, params, status: seen.append(params))
        _run(wt._do_close_tier("MNQ", "DU1", "tr", 0.5, 5, 26697.0, 0.0))
        params = seen[0]
        assert params["order_type"] == "TRAIL"
        assert params["stop_price"] == 0.5    # distance in the STP cell
        assert "limit_price" not in params

    def test_invalid_side_still_rejected(self):
        pos = _make_position("MNQ", "FUT", 111, net_pos=2, account="DU1")
        wt = _make_close_tier_worker([pos])
        _run(wt._do_close_tier("MNQ", "DU1", "zz", 0.5, 5, 26697.0, 0.0))
        assert wt._ib.placed == []


# ═══════════════════════════════════════════════════════════════════════════════
#  _child_price_params — blotter price cells per child order type
# ═══════════════════════════════════════════════════════════════════════════════

class TestChildPriceParams:
    _f = staticmethod(IBWorkerThread._child_price_params)

    def test_lmt_limit_only(self):
        assert self._f("LMT", 101.5, None) == {"limit_price": 101.5}

    def test_stp_stop_only(self):
        assert self._f("STP", None, 99.0) == {"stop_price": 99.0}

    def test_stp_lmt_both(self):
        assert self._f("STP LMT", 98.5, 99.0) == {
            "limit_price": 98.5, "stop_price": 99.0}

    def test_trail_distance_in_stop_cell(self):
        assert self._f("TRAIL", None, 0.5) == {"stop_price": 0.5}

    def test_dbl_max_sentinels_dropped(self):
        big = 1.8e308
        assert self._f("LMT", big, None) == {}
        assert self._f("TRAIL", None, big) == {}
        assert self._f("STP", None, 0.0) == {}


# ═══════════════════════════════════════════════════════════════════════════════
#  _convert_mkt_parent_for_trail — IB error 328 guard
# ═══════════════════════════════════════════════════════════════════════════════

def _mkt_parent(action="BUY"):
    return SimpleNamespace(
        action=action,
        orderType="MKT",
        orderRef="nano_0419_120000_abcd",
    )


class TestConvertMktParentForTrail:
    def test_buy_mkt_becomes_lmt_capped_above_ref(self):
        wt = _make_worker()
        params = {"tick_size": 0.25}
        order = _mkt_parent("BUY")
        assert wt._convert_mkt_parent_for_trail(order, params, 17500.0)
        # No cap preference → 10 ticks through the market.
        assert order.orderType == "LMT"
        assert order.lmtPrice == 17502.50
        # Blotter params reflect the converted parent.
        assert params["order_type"] == "LMT"
        assert params["limit_price"] == 17502.50

    def test_sell_mkt_caps_below_ref(self):
        wt = _make_worker()
        order = _mkt_parent("SELL")
        assert wt._convert_mkt_parent_for_trail(order, {"tick_size": 0.25},
                                                17500.0)
        assert order.lmtPrice == 17497.50

    def test_limit_cap_offset_preference_wins(self):
        wt = _make_worker()
        params = {"tick_size": 0.25, "limit_cap_offset": 1.0}
        order = _mkt_parent("BUY")
        assert wt._convert_mkt_parent_for_trail(order, params, 17500.0)
        assert order.lmtPrice == 17501.00

    def test_cap_snaps_to_tick_grid(self):
        wt = _make_worker()
        params = {"tick_size": 0.25, "limit_cap_offset": 0.30}
        order = _mkt_parent("BUY")
        assert wt._convert_mkt_parent_for_trail(order, params, 17500.0)
        assert order.lmtPrice == 17500.25  # 17500.30 snapped to 0.25 grid

    def test_non_mkt_parent_untouched(self):
        wt = _make_worker()
        params = {"tick_size": 0.25}
        order = SimpleNamespace(action="BUY", orderType="LMT",
                                lmtPrice=17499.0,
                                orderRef="nano_0419_120000_abcd")
        assert not wt._convert_mkt_parent_for_trail(order, params, 17500.0)
        assert order.orderType == "LMT"
        assert order.lmtPrice == 17499.0
        assert "order_type" not in params

    def test_zero_ref_price_leaves_mkt(self):
        """A SELL against a tiny ref can only produce lmt<=0 — no convert."""
        wt = _make_worker()
        order = _mkt_parent("SELL")
        assert not wt._convert_mkt_parent_for_trail(
            order, {"tick_size": 0.25}, 1.0)
        assert order.orderType == "MKT"

    def test_default_penny_tick_for_stocks(self):
        wt = _make_worker()
        order = _mkt_parent("BUY")
        assert wt._convert_mkt_parent_for_trail(order, {}, 150.00)
        assert order.lmtPrice == 150.10  # 10 * 0.01 default tick
