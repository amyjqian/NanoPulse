"""FlashTradeView._on_order_requested — order-type translation for the
Options Income ticket's payloads (sec_type OPT).

The handler is driven unbound on a minimal stub: the OPT paths never
touch the account list (payloads carry ``_target_accounts``), and the
worker is a recorder.  This pins the translation layer:

* ``"MID"``  → ADAPTIVE with priority "Normal" (never the futures prefs
  that ``trading_prefs.get("OPT")`` falls back to);
* ``"BID"``/``"ASK"`` → PEG_PRIM honouring the payload's cap and
  peg_offset (never the futures ADAPTIVE branch);
* ``"MKT"``  → stays MKT (no limit-cap wrap for options);
* payload ``min_tick`` overrides the generic 0.01 OPT tick.
"""
from __future__ import annotations

from src.ui.widgets.flash_trade_view import FlashTradeView


class _Recorder:
    def __init__(self):
        self.option_orders: list[dict] = []
        self.orders: list[dict] = []

    def place_option_order(self, params):
        self.option_orders.append(dict(params))

    def place_order(self, params):
        self.orders.append(dict(params))


class _Signal:
    def emit(self, *_a, **_k):
        pass


class _Stub:
    """Just the attributes the OPT translation path touches."""

    def __init__(self, worker):
        self._trading_allowed = True
        self._worker = worker
        self.transient_status_message = _Signal()

    def _partition_accounts_by_connection(self, accounts):
        return list(accounts), []

    def _notify_skipped_accounts(self, _what, _skipped):
        pass

    def _find_worker_for_account(self, _account):
        return self._worker

    def _ensure_worker_connected(self, _worker):
        pass


_CONTRACT = {
    "conid": 9007,
    "symbol": "NVDA",
    "sec_type": "OPT",
    "exchange": "SMART",
    "currency": "USD",
    "local_symbol": "NVDA 187.5C",
    "expiry": "20260918",
    "multiplier": "100",
    "strike": 187.5,
    "right": "C",
}


def _payload(**over):
    base = {
        "action": "SELL",
        "type": "MID",
        "symbol": "NVDA",
        "qty": 1,
        "limit_price": 3.10,
        "last_price": 3.10,
        "min_tick": 0.05,
        "sec_type": "OPT",
        "profit_take": None,
        "stop_loss": None,
        "trailing": None,
        "_resolved_contract": dict(_CONTRACT),
        "_target_accounts": ["U111"],
        "_what_if_check": True,
    }
    base.update(over)
    return base


def _run(payload):
    worker = _Recorder()
    FlashTradeView._on_order_requested(_Stub(worker), payload)
    assert len(worker.option_orders) == 1
    return worker.option_orders[0]


def test_mid_translates_to_adaptive_normal_priority():
    params = _run(_payload(type="MID"))
    assert params["order_type"] == "ADAPTIVE"
    assert params["adaptive_priority"] == "Normal"
    assert params["limit_price"] == 3.10
    assert "peg_offset" not in params


def test_passive_ask_translates_to_peg_prim():
    params = _run(_payload(type="ASK", peg_offset=0.0))
    assert params["order_type"] == "PEG_PRIM"
    assert params["limit_price"] == 3.10       # cap/floor from payload
    assert params["peg_offset"] == 0.0
    assert params["account"] == "U111"


def test_passive_bid_translates_to_peg_prim():
    params = _run(_payload(action="BUY", type="BID", peg_offset=0.0))
    assert params["order_type"] == "PEG_PRIM"
    assert params["peg_offset"] == 0.0


def test_opt_market_order_is_not_cap_wrapped():
    params = _run(_payload(type="MKT", limit_price=0.0))
    assert params["order_type"] == "MKT"
    assert "limit_price" not in params


def test_payload_min_tick_overrides_generic_opt_tick():
    params = _run(_payload(min_tick=0.05))
    assert params["tick_size"] == 0.05
    params = _run(_payload(min_tick=None))
    assert params["tick_size"] == 0.01         # generic OPT fallback


def test_plain_limit_passthrough():
    params = _run(_payload(type="LMT", limit_price=2.95))
    assert params["order_type"] == "LMT"
    assert params["limit_price"] == 2.95
