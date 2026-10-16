"""Stock orders in the overnight session (20:00–03:59 ET).

A stock order then routes to IBEOS, which takes limit orders only — the
peg behind Buy @Bid +1t, the Adaptive algo behind @Mid and any market
order (flatten / close legs included) are refused with error 387.
``FlashTradeView`` turns each into a plain limit priced from the live
quote; an order that is already a limit is left alone.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal

import src.ui.widgets.flash_trade_view as ftv
from src.ui.widgets.accounts_model import AccountsTableModel
from src.ui.widgets.flash_trade_view import overnight_limit_price


# ── pricing ───────────────────────────────────────────────────────────────────

class TestOvernightLimitPrice:
    def test_bid_plus_one_tick_and_ask_minus_one_tick(self):
        assert overnight_limit_price("BID", "BUY", 100.00, 100.10, 0.01) \
            == pytest.approx(100.01)
        assert overnight_limit_price("ASK", "SELL", 100.00, 100.10, 0.01) \
            == pytest.approx(100.09)

    def test_one_tick_inside_never_crosses_a_one_tick_spread(self):
        assert overnight_limit_price("BID", "BUY", 100.57, 100.58, 0.01) \
            == pytest.approx(100.58)
        assert overnight_limit_price("ASK", "SELL", 100.57, 100.58, 0.01) \
            == pytest.approx(100.57)

    def test_mid_rounds_toward_the_far_side(self):
        assert overnight_limit_price("MID", "BUY", 100.00, 100.05, 0.01) \
            == pytest.approx(100.03)
        assert overnight_limit_price("MID", "SELL", 100.00, 100.05, 0.01) \
            == pytest.approx(100.02)
        assert overnight_limit_price("MID", "BUY", 100.00, 100.10, 0.01) \
            == pytest.approx(100.05)

    def test_market_takes_the_far_side(self):
        assert overnight_limit_price("MKT", "BUY", 99.0, 99.2, 0.01) == 99.2
        assert overnight_limit_price("MKT", "SELL", 99.0, 99.2, 0.01) == 99.0

    def test_no_two_sided_quote_means_no_price(self):
        assert overnight_limit_price("MKT", "BUY", 0.0, 99.2, 0.01) == 0.0
        assert overnight_limit_price("MID", "SELL", 99.0, 0.0, 0.01) == 0.0


# ── the order path ───────────────────────────────────────────────────────────

class _Worker(QObject):
    order_status_updated = Signal(dict)

    def __init__(self):
        super().__init__()
        self.orders: list[dict] = []

    def place_order(self, params: dict) -> None:
        self.orders.append(dict(params))


_NVDA = {"conid": 4815747, "symbol": "NVDA", "sec_type": "STK",
         "exchange": "SMART", "currency": "USD", "local_symbol": "NVDA"}
_MNQ = {"conid": 770561201, "symbol": "MNQ", "sec_type": "FUT",
        "exchange": "CME", "currency": "USD", "local_symbol": "MNQZ6",
        "expiry": "20261218", "multiplier": "2"}


@pytest.fixture
def rig(qtbot, monkeypatch):
    """One margin account selected, NVDA quoted 180.00 / 180.10."""
    import src.core.instruments as instruments
    contracts = {"NVDA": _NVDA, "MNQ": _MNQ}
    monkeypatch.setattr(instruments, "get_contract",
                        lambda sym: contracts.get((sym or "").upper()))
    am = AccountsTableModel()
    am.update_batch([("L", "A1", "AvailableFunds", "50000", "USD"),
                     ("L", "A1", "BuyingPower", "200000", "USD")])
    v = ftv.FlashTradeView(active_threads={}, accounts_model=am)
    qtbot.addWidget(v)
    v._model_pin = am
    worker = _Worker()
    monkeypatch.setattr(v, "_find_worker_for_account", lambda a: worker)
    monkeypatch.setattr(v, "_partition_accounts_by_connection",
                        lambda accts: (list(accts), []))
    monkeypatch.setattr(v, "_ensure_worker_connected", lambda w: None)
    monkeypatch.setattr(v._account_list, "get_selected_accounts",
                        lambda: ["A1"])
    monkeypatch.setattr(v._account_list, "is_full_fanout_selection",
                        lambda: False)
    v._order_entry._price_cache["NVDA"] = (180.05, 180.00, 180.10)
    msgs: list[str] = []
    v.transient_status_message.connect(lambda m, _ms=0: msgs.append(m))
    return v, worker, msgs


@pytest.fixture
def overnight(monkeypatch):
    import src.core.ib_worker as ib_worker
    monkeypatch.setattr(ib_worker, "stk_routes_overnight",
                        lambda currency="USD", now=None: True)


def _order(symbol="NVDA", **over) -> dict:
    p = {"action": "BUY", "type": "BID", "symbol": symbol, "qty": 10,
         "last_price": 180.05, "profit_take": None, "stop_loss": None,
         "trailing": None}
    p.update(over)
    return p


def _sent(worker):
    (o,) = worker.orders
    return o["order_type"], o.get("limit_price")


class TestOvernightOrders:
    @pytest.mark.parametrize("action,button,price", [
        ("BUY", "BID", 180.01), ("SELL", "ASK", 180.09),
        ("BUY", "MID", 180.05), ("SELL", "MID", 180.05),
        ("BUY", "MKT", 180.10), ("SELL", "MKT", 180.00)])
    def test_buttons_become_limits(self, rig, overnight, action, button,
                                   price):
        v, worker, _ = rig
        v._on_order_requested(_order(action=action, type=button))
        order_type, limit = _sent(worker)
        assert order_type == "LMT" and limit == pytest.approx(price)
        (o,) = worker.orders
        assert "peg_offset" not in o and "adaptive_priority" not in o
        assert o["bracket_ref_price"] == pytest.approx(price)

    def test_flatten_leg_becomes_a_limit(self, rig, overnight):
        v, worker, _ = rig
        v._on_order_requested(_order(
            action="SELL", type="MKT", _resolved_contract=dict(_NVDA),
            _target_accounts=["A1"]))
        assert _sent(worker) == ("LMT", 180.00)

    def test_explicit_limit_is_untouched(self, rig, overnight):
        v, worker, _ = rig
        v._on_order_requested(_order(type="BID_LMT", limit_price=179.5))
        assert _sent(worker) == ("LMT", 179.5)

    def test_no_quote_sends_nothing(self, rig, overnight):
        v, worker, msgs = rig
        v._order_entry._price_cache["NVDA"] = (180.05, 0.0, 0.0)
        v._on_order_requested(_order())
        assert worker.orders == [] and "not sent" in msgs[-1]

    def test_futures_are_not_touched(self, rig, overnight, monkeypatch):
        v, worker, _ = rig
        def _boom(*_a, **_k):
            raise AssertionError("futures never take the overnight limit")
        monkeypatch.setattr(ftv, "overnight_limit_price", _boom)
        v._on_order_requested(_order(symbol="MNQ", type="MKT",
                                     last_price=29800.0))
        assert len(worker.orders) == 1

    def test_regular_hours_keep_peg_and_adaptive(self, rig):
        v, worker, _ = rig
        v._on_order_requested(_order(type="BID"))
        v._on_order_requested(_order(type="MID"))
        assert [o["order_type"] for o in worker.orders] == [
            "PEG_PRIM", "ADAPTIVE"]
