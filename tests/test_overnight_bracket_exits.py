"""Bracket exits on an overnight stock order (20:00–03:59 ET).

Until the parent is done its exits are also recorded on disk
(``src.core.pending_exits``), so a parent that fills while NanoPulse is
closed, disconnected or TWS is restarting still gets them: every connect
reconciles the record against TWS's orders and fills.

The order routes to IBEOS, which takes limit orders only — an attached
STP / STP LMT / TRAIL child would be refused (387).  So overnight the
parent goes out alone and, once it fills, its TP / SL / trail exits are
placed as standalone orders: one OCA group (the first to fill cancels
the rest), sized to the filled quantity, priced off the actual fill and
routed SMART with outsideRth — IB holds the stops until the regular
session opens.  Outside the overnight session brackets are unchanged.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import src.core.ib_worker as ib_worker
from src.core import pending_exits
from src.core.ib_worker import IBWorkerThread


class _Client:
    def __init__(self):
        self._next = 1000

    def getReqId(self) -> int:
        self._next += 1
        return self._next


class _Trade:
    def __init__(self, order):
        self.order = order
        self.orderStatus = SimpleNamespace(status="PendingSubmit",
                                           filled=0.0, avgFillPrice=0.0)
        self._handlers: list = []
        self.statusEvent = self

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def fire(self, status, filled=0.0, avg=0.0):
        self.orderStatus.status = status
        self.orderStatus.filled = filled
        self.orderStatus.avgFillPrice = avg
        for h in list(self._handlers):
            h(self)


class _IB:
    def __init__(self):
        self.client = _Client()
        self.placed: list[tuple[str, object, _Trade]] = []
        self.open_trades: list = []       # what TWS replays on connect
        self.exec_fills: list = []
        self.accounts = ["U1"]

    def isConnected(self):
        return True

    def managedAccounts(self):
        return list(self.accounts)

    def trades(self):
        return list(self.open_trades) + [t for _, _, t in self.placed]

    def fills(self):
        return list(self.exec_fills)

    def placeOrder(self, contract, order):
        trade = _Trade(order)
        self.placed.append((contract.exchange, order, trade))
        return trade


def _worker() -> IBWorkerThread:
    wt = IBWorkerThread.__new__(IBWorkerThread)
    wt._ib = _IB()
    wt._label = "test"
    wt._trades = {}
    wt._has_status_handler = set()
    wt._conid_contract_meta = {}
    wt._bg_last_bid_ask = {}
    wt._emit_order_status = lambda *a, **k: None
    wt._apply_fill_to_position = lambda *a, **k: None
    wt.notices = []
    wt.exits_notice = SimpleNamespace(emit=wt.notices.append)
    return wt


def _params(**over) -> dict:
    p = {"conid": 4815747, "symbol": "NVDA", "sec_type": "STK",
         "exchange": "SMART", "currency": "USD", "local_symbol": "NVDA",
         "account": "U1", "action": "BUY", "qty": 10, "order_type": "LMT",
         "limit_price": 180.10, "tif": "DAY", "tick_size": 0.01,
         "last_price": 180.05, "bracket_ref_price": 180.10,
         "profit_take": {"value": 2, "unit": "price"},
         "stop_loss": {"value": 3, "unit": "price"}}
    p.update(over)
    return p


@pytest.fixture
def overnight(monkeypatch):
    monkeypatch.setattr(ib_worker, "stk_routes_overnight",
                        lambda currency="USD", now=None: True)


def _place(wt, params):
    asyncio.run(wt._do_place_order(params))
    return wt._ib.placed


class TestOvernightBracket:
    def test_parent_goes_alone_to_ibeos(self, overnight):
        wt = _worker()
        placed = _place(wt, _params())
        assert len(placed) == 1
        exch, parent, _ = placed[0]
        assert exch == "IBEOS" and parent.orderType == "LMT"
        assert parent.transmit is True

    def test_fill_places_oca_exits_off_the_fill(self, overnight):
        wt = _worker()
        _place(wt, _params())
        parent_trade = wt._ib.placed[0][2]
        parent_trade.fire("Filled", filled=10, avg=180.00)
        exits = wt._ib.placed[1:]
        # The take profit works tonight on IBEOS; its SMART twin and the
        # stop wait for the pre-market / regular session.
        assert [(e, o.orderType, o.action, o.totalQuantity)
                for e, o, _ in exits] == [
            ("IBEOS", "LMT", "SELL", 10),
            ("SMART", "LMT", "SELL", 10), ("SMART", "STP", "SELL", 10)]
        night_tp, tp, sl = (o for _, o, _ in exits)
        assert night_tp.lmtPrice == tp.lmtPrice == pytest.approx(182.00)
        assert sl.auxPrice == pytest.approx(177.00)
        assert night_tp.advancedErrorOverride == "10329"
        assert night_tp.outsideRth is False
        assert night_tp.parentId == 0 and night_tp.transmit is True
        assert night_tp.ocaGroup == tp.ocaGroup and night_tp.ocaType == 1
        assert len({night_tp.orderId, tp.orderId, sl.orderId}) == 3
        assert len({night_tp.orderRef, tp.orderRef, sl.orderRef}) == 3
        assert f"U1_{night_tp.orderRef}" in wt._trades
        for o in (tp, sl):
            assert o.parentId == 0 and o.transmit is True
            assert o.ocaGroup == f"oca_{parent_trade.order.orderRef}"
            assert o.ocaType == 1 and o.outsideRth is True
            assert o.account == "U1" and o.orderRef.startswith("nano_")
        assert tp.orderRef != sl.orderRef
        # Both exits are tracked like any order.
        assert {f"U1_{tp.orderRef}", f"U1_{sl.orderRef}"} <= set(wt._trades)

    def test_exits_placed_once(self, overnight):
        wt = _worker()
        _place(wt, _params())
        parent_trade = wt._ib.placed[0][2]
        parent_trade.fire("Filled", filled=10, avg=180.00)
        parent_trade.fire("Completed", filled=10, avg=180.00)
        assert len(wt._ib.placed) == 4

    def test_partial_fill_then_cancel_protects_what_filled(self, overnight):
        wt = _worker()
        _place(wt, _params())
        parent_trade = wt._ib.placed[0][2]
        parent_trade.fire("Cancelled", filled=4, avg=180.02)
        assert [o.totalQuantity for _, o, _ in wt._ib.placed[1:]] == [4, 4, 4]

    def test_cancel_without_fill_places_nothing(self, overnight):
        wt = _worker()
        _place(wt, _params())
        wt._ib.placed[0][2].fire("Cancelled")
        assert len(wt._ib.placed) == 1

    def test_stop_only_has_no_overnight_twin(self, overnight):
        wt = _worker()
        _place(wt, _params(profit_take=None))
        wt._ib.placed[0][2].fire("Filled", filled=10, avg=180.00)
        assert [(e, o.orderType) for e, o, _ in wt._ib.placed[1:]] == [
            ("SMART", "STP")]

    def test_trailing_exit_is_standalone_trail(self, overnight):
        wt = _worker()
        _place(wt, _params(profit_take=None, stop_loss=None,
                           trailing={"value": 1.5, "unit": "price"}))
        wt._ib.placed[0][2].fire("Filled", filled=10, avg=180.00)
        (_, trail, _), = wt._ib.placed[1:]
        assert trail.orderType == "TRAIL" and trail.auxPrice == 1.5
        assert trail.parentId == 0


class TestRegularHoursBracketUnchanged:
    def test_children_attach_to_the_parent(self):
        wt = _worker()
        placed = _place(wt, _params())
        assert [e for e, _, _ in placed] == ["SMART"] * 3
        parent = placed[0][1]
        assert parent.transmit is False
        assert [o.parentId for _, o, _ in placed[1:]] == [parent.orderId] * 2
        assert all(not getattr(o, "ocaGroup", "") for _, o, _ in placed[1:])


# ── on disk until the parent is done ──────────────────────────────────────────

def _key(wt) -> str:
    return f"U1_{wt._ib.placed[0][1].orderRef}"


class TestPendingRecord:
    def test_recorded_until_the_fill_places_the_exits(self, overnight):
        wt = _worker()
        _place(wt, _params())
        rec = pending_exits.records()[_key(wt)]
        assert rec["account"] == "U1" and rec["params"]["stop_loss"]
        wt._ib.placed[0][2].fire("Filled", filled=10, avg=180.00)
        assert pending_exits.records() == {}

    def test_unfilled_cancel_clears_the_record(self, overnight):
        wt = _worker()
        _place(wt, _params())
        wt._ib.placed[0][2].fire("Cancelled")
        assert pending_exits.records() == {}

    def test_regular_hours_record_nothing(self):
        wt = _worker()
        _place(wt, _params())
        assert pending_exits.records() == {}


def _record(ref="nano_261016_013000_abcd", account="U1", age_s=0.0,
            **params_over):
    import time
    pending_exits.add(f"{account}_{ref}", label="J-ib", account=account,
                      order_ref=ref, params=_params(**params_over),
                      ref_price=180.10)
    if age_s:
        data = pending_exits._read()
        data[f"{account}_{ref}"]["created"] = time.time() - age_s
        pending_exits._write(data)
    return ref


def _exec(ref, shares, price, account="U1"):
    return SimpleNamespace(execution=SimpleNamespace(
        orderRef=ref, acctNumber=account, shares=shares, price=price))


def _replayed(ref, status, filled=0.0, avg=0.0, account="U1", oca=""):
    return SimpleNamespace(
        order=SimpleNamespace(orderRef=ref, account=account, ocaGroup=oca),
        orderStatus=SimpleNamespace(status=status, filled=filled,
                                    avgFillPrice=avg))


def _reconcile(wt):
    asyncio.run(wt._reconcile_pending_exits())
    return [o for _, o, _ in wt._ib.placed]


class TestReconnect:
    """A fresh worker (NanoPulse restarted, or reconnected) catching up."""

    def test_filled_while_offline_places_exits_off_the_fills(self):
        ref = _record()
        wt = _worker()
        wt._ib.exec_fills = [_exec(ref, 6, 180.00), _exec(ref, 4, 180.05)]
        tp, sl = _reconcile(wt)
        assert (tp.orderType, tp.totalQuantity) == ("LMT", 10)
        assert tp.lmtPrice == pytest.approx(182.02)     # vwap 180.02 + 2
        assert sl.auxPrice == pytest.approx(177.02)
        assert tp.ocaGroup == sl.ocaGroup == f"oca_{ref}"
        assert [e for e, _, _ in wt._ib.placed] == ["SMART", "SMART"]
        assert pending_exits.records() == {}
        assert "exits are now placed" in wt.notices[-1]

    def test_caught_up_overnight_also_twins_the_take_profit(
            self, overnight):
        ref = _record()
        wt = _worker()
        wt._ib.exec_fills = [_exec(ref, 10, 180.00)]
        _reconcile(wt)
        assert [e for e, _, _ in wt._ib.placed] == [
            "IBEOS", "SMART", "SMART"]

    def test_replayed_done_parent_uses_its_status(self):
        ref = _record()
        wt = _worker()
        wt._ib.open_trades = [_replayed(ref, "Filled", 10, 179.90)]
        tp, _ = _reconcile(wt)
        assert tp.totalQuantity == 10
        assert tp.lmtPrice == pytest.approx(181.90)

    def test_still_working_parent_waits_for_its_status(self):
        ref = _record()
        wt = _worker()
        parent = _replayed(ref, "Submitted")
        wt._ib.open_trades = [parent]
        assert _reconcile(wt) == []
        assert pending_exits.has(f"U1_{ref}")
        # …then fills after the reconnect: the global hook settles it.
        parent.orderStatus.status = "Filled"
        parent.orderStatus.filled = 10
        parent.orderStatus.avgFillPrice = 180.00
        wt._maybe_settle_pending_from_trade(parent)
        assert len(wt._ib.placed) == 2
        assert pending_exits.records() == {}
        wt._maybe_settle_pending_from_trade(parent)     # replayed again
        assert len(wt._ib.placed) == 2

    def test_exits_already_out_just_clears_the_record(self):
        ref = _record()
        wt = _worker()
        wt._ib.open_trades = [_replayed("nano_261016_013500_wxyz",
                                        "PreSubmitted", oca=f"oca_{ref}")]
        wt._ib.exec_fills = [_exec(ref, 10, 180.0)]
        assert _reconcile(wt) == []
        assert pending_exits.records() == {}

    def test_cancelled_unfilled_parent_is_dropped(self):
        ref = _record()
        wt = _worker()
        wt._ib.open_trades = [_replayed(ref, "Cancelled")]
        assert _reconcile(wt) == []
        assert pending_exits.records() == {}

    def test_untraceable_record_kept_for_a_day_then_dropped(self):
        young = _record("nano_261016_013000_aaaa")
        old = _record("nano_261015_013000_bbbb", age_s=2 * 86400)
        wt = _worker()
        assert _reconcile(wt) == []
        assert set(pending_exits.records()) == {f"U1_{young}"}
        assert "NOT placed" in wt.notices[-1]

    def test_other_connections_accounts_are_left_alone(self):
        ref = _record(account="U9")
        wt = _worker()
        wt._ib.exec_fills = [_exec(ref, 10, 180.0, account="U9")]
        assert _reconcile(wt) == []
        assert pending_exits.has(f"U9_{ref}")
