"""Per-account percent resolution in FlashTradeView's order fan-out.

One ``qty_pct`` payload → each checked account gets a share count sized
to its own NetLiquidation (clamped to its own buying power); accounts
without NLV data are skipped; missing price aborts; non-percent payloads
are untouched (regression).

Model accessors (`get_net_liquidation` / `get_excess_liquidity`) are
covered here too, seeded through the public ``update_batch`` API.
"""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QObject, Signal

from src.core import config_manager
from src.ui.widgets.accounts_model import AccountsTableModel


class FakeWorker(QObject):
    order_status_updated = Signal(dict)

    def __init__(self):
        super().__init__()
        self.orders: list[dict] = []

    def place_order(self, params: dict) -> None:
        self.orders.append(dict(params))

    def place_option_order(self, params: dict) -> None:
        self.orders.append(dict(params))


@pytest.fixture
def model():
    am = AccountsTableModel()
    am.update_batch([
        ("L", "A1", "NetLiquidation", "1420000", "USD"),
        ("L", "A2", "NetLiquidation", "1000000", "USD"),
        ("L", "A2", "BuyingPower", "100000", "USD"),
        ("L", "A3", "AccountType", "INDIVIDUAL", ""),   # no NLV for A3
    ])
    return am


def test_model_accessors(model):
    assert model.get_net_liquidation("A1") == 1_420_000.0
    assert model.get_net_liquidation("A3") == 0.0
    assert model.get_net_liquidation("nope") == 0.0
    assert model.get_buying_power("A2") == 100_000.0
    assert model.get_excess_liquidity("A1") == 0.0


@pytest.fixture
def view(qtbot, tmp_path, monkeypatch, model):
    monkeypatch.setattr(config_manager, "_active_path", tmp_path / "cfg.json")
    (tmp_path / "cfg.json").write_text(json.dumps({}))
    from src.ui.widgets.flash_trade_view import FlashTradeView
    v = FlashTradeView(active_threads={}, accounts_model=model)
    qtbot.addWidget(v)
    workers = {a: FakeWorker() for a in ("A1", "A2", "A3")}
    monkeypatch.setattr(v, "_find_worker_for_account",
                        lambda a: workers.get(a))
    monkeypatch.setattr(v, "_partition_accounts_by_connection",
                        lambda accts: (list(accts), []))
    monkeypatch.setattr(v, "_ensure_worker_connected", lambda w: None)
    return v, workers


def _payload(**over) -> dict:
    p = {
        "action": "BUY", "type": "BID_LMT", "symbol": "NVDA",
        "qty": 10, "sec_type": "STK",
        "last_price": 182.40, "limit_price": 182.40,
        "profit_take": None, "stop_loss": None, "trailing": None,
        "_target_accounts": ["A1", "A2", "A3"],
        "_resolved_contract": {
            "conid": 4815747, "symbol": "NVDA", "sec_type": "STK",
            "exchange": "SMART", "currency": "USD",
            "local_symbol": "", "expiry": "", "multiplier": "",
        },
    }
    p.update(over)
    return p


def _seed_positions(v):
    """A1 holds 600 NVDA, A2 holds 3, A3 is flat."""
    def _pos(account, qty, con_id):
        return {"label": "L", "account": account, "con_id": con_id,
                "symbol": "NVDA", "local_symbol": "NVDA",
                "sec_type": "STK", "exchange": "SMART", "currency": "USD",
                "multiplier": "", "position": qty, "avg_cost": 100.0,
                "market_price": 182.40, "market_value": qty * 182.40,
                "unrealized_pnl": 0.0, "realized_pnl": 0.0}
    v._account_position._model.update_positions(
        [_pos("A1", 600.0, 11), _pos("A2", 3.0, 12)])


def test_percent_resolves_per_account(view):
    v, workers = view
    v._on_order_requested(_payload(qty_pct=25))
    # A1: 25% × $1.42M / 182.40 → 1,946 (BP unknown → NLV base).
    assert workers["A1"].orders[0]["qty"] == 1946
    # A2: deployable base min($1.0M, $100k BP) → 25% × $100k / 182.40 → 137.
    assert workers["A2"].orders[0]["qty"] == 137
    # A3 has no NLV → skipped entirely, never a guessed size.
    assert workers["A3"].orders == []


def test_percent_without_price_aborts_all(view):
    v, workers = view
    v._on_order_requested(_payload(qty_pct=25, last_price=0.0,
                                   limit_price=0.0))
    assert all(w.orders == [] for w in workers.values())


def test_percent_falls_back_to_limit_price(view):
    v, workers = view
    v._on_order_requested(_payload(qty_pct=25, last_price=0.0,
                                   limit_price=182.40))
    assert workers["A1"].orders[0]["qty"] == 1946


def test_percent_sell_resolves_per_account_position(view):
    v, workers = view
    _seed_positions(v)
    v._on_order_requested(_payload(qty_pct=25, action="SELL"))
    # A1: 25% of 600 held → 150.  A2: 25% of 3 → ceil(0.75) = 1.
    assert workers["A1"].orders[0]["qty"] == 150
    assert workers["A2"].orders[0]["qty"] == 1
    # A3 is flat → skipped, never shorted.
    assert workers["A3"].orders == []


def test_percent_sell_works_without_price(view):
    # Position-based sizing needs no live price (buy would abort).
    v, workers = view
    _seed_positions(v)
    v._on_order_requested(_payload(qty_pct=25, action="SELL",
                                   last_price=0.0))
    assert workers["A1"].orders[0]["qty"] == 150


def test_plain_payload_unchanged_regression(view):
    v, workers = view
    v._on_order_requested(_payload())               # no qty_pct
    assert workers["A1"].orders[0]["qty"] == 10
    assert workers["A2"].orders[0]["qty"] == 10
    assert workers["A3"].orders[0]["qty"] == 10     # A3 not skipped


def test_fut_payload_ignores_percent(view):
    v, workers = view
    p = _payload(qty_pct=25, symbol="MNQ", sec_type="FUT")
    p["_resolved_contract"]["sec_type"] = "FUT"
    p["_resolved_contract"]["symbol"] = "MNQ"
    v._on_order_requested(p)
    # Defensive: percent never applies to futures — plain qty fan-out.
    assert workers["A1"].orders[0]["qty"] == 10
    assert workers["A3"].orders[0]["qty"] == 10
