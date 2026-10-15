"""Covering a cash-account stock buy by selling sweep holdings first.

A cash account (no margin, every registered plan) can only buy with the
cash it has.  When exactly one such account is selected and a plain Buy
needs more than its AvailableFunds, FlashTradeView offers to sell the
account's configured sweep symbols (SGOV, BIL, …) first — sized like the
Accounts panel's Sweep out — and sends the buy automatically once every
sell has filled.  Holdings that cannot cover it are sold in full and the
buy shrinks to what the cash plus proceeds affords.  Margin accounts,
multi-account fan-outs, flatten legs, hot buttons, sweeps and the
options ticket never see the dialog.
"""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QObject, Signal

from src.core import config_manager
from src.core.cash_cover import buy_unit_price, plan_cash_cover
from src.core.cash_management import CashManagementPreferences
from src.ui.widgets.accounts_model import AccountsTableModel
import src.ui.widgets.flash_trade_view as ftv


# ── planner ───────────────────────────────────────────────────────────────────

class TestPlanner:
    def test_no_shortfall_means_no_sells(self):
        p = plan_cash_cover("A", "NVDA", 10, 220.0, 5000.0, [("SGOV", 100, 100.5)])
        assert not p.needed and p.sells == [] and p.buy_qty == 10

    def test_sells_in_configured_order_only_as_needed(self):
        p = plan_cash_cover("A", "NVDA", 100, 220.0, 5000.0,
                            [("SGOV", 150, 100.5), ("BIL", 200, 91.6)])
        assert p.shortfall == pytest.approx(17000.0)
        assert [(s.symbol, s.qty) for s in p.sells] == [("SGOV", 150), ("BIL", 25)]
        assert p.proceeds >= p.shortfall
        assert p.buy_qty == 100 and not p.shrunk

    def test_sell_rounds_up_to_lots_of_five(self):
        p = plan_cash_cover("A", "X", 1, 1001.0, 0.0, [("SGOV", 100, 100.0)])
        assert p.sells[0].qty == 15          # 10.01 shares needed → 15

    def test_insufficient_holdings_sell_all_and_shrink_buy(self):
        p = plan_cash_cover("A", "NVDA", 100, 220.0, 5000.0, [("SGOV", 50, 100.5)])
        assert [(s.symbol, s.qty) for s in p.sells] == [("SGOV", 50)]
        assert p.shrunk and p.buy_qty == 45   # (5000 + 5025) // 220

    def test_nothing_fits_even_after_selling(self):
        p = plan_cash_cover("A", "BRK", 1, 700000.0, 100.0, [("SGOV", 5, 100.0)])
        assert p.sells and p.buy_qty == 0

    def test_unpriced_or_empty_holdings_are_skipped(self):
        p = plan_cash_cover("A", "NVDA", 100, 220.0, 5000.0,
                            [("SGOV", 0, 100.5), ("BIL", 200, 0.0)])
        assert p.needed and p.sells == []

    def test_unit_price_limit_vs_market(self):
        assert buy_unit_price("LMT", 182.4, 190.0, 180.0) == 182.4
        assert buy_unit_price("MKT", None, 190.0, 180.0) == pytest.approx(199.5)
        assert buy_unit_price("PEG_PRIM", 0, 0.0, 180.0) == pytest.approx(189.0)
        assert buy_unit_price("MKT", None, 0.0, 0.0) == 0.0


# ── flow ──────────────────────────────────────────────────────────────────────

class FakeWorker(QObject):
    order_status_updated = Signal(dict)

    def __init__(self):
        super().__init__()
        self.orders: list[dict] = []

    def place_order(self, params: dict) -> None:
        self.orders.append(dict(params))

    def place_option_order(self, params: dict) -> None:
        self.orders.append(dict(params))


_CONTRACTS = {
    "NVDA": {"conid": 4815747, "symbol": "NVDA", "sec_type": "STK",
             "exchange": "SMART", "currency": "USD", "local_symbol": "NVDA"},
    "SGOV": {"conid": 424099317, "symbol": "SGOV", "sec_type": "STK",
             "exchange": "SMART", "currency": "USD", "local_symbol": "SGOV"},
    "BIL": {"conid": 296457239, "symbol": "BIL", "sec_type": "STK",
            "exchange": "SMART", "currency": "USD", "local_symbol": "BIL"},
}


@pytest.fixture
def rig(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(config_manager, "_active_path", tmp_path / "cfg.json")
    (tmp_path / "cfg.json").write_text(json.dumps({}))
    import src.core.instruments as instruments
    monkeypatch.setattr(instruments, "get_contract",
                        lambda sym: _CONTRACTS.get((sym or "").upper()))
    monkeypatch.setattr(CashManagementPreferences, "sweep_symbols",
                        property(lambda self: ["SGOV", "BIL"]))
    am = AccountsTableModel()
    am.update_batch([("L", "A1", "NetLiquidation", "60000", "USD")])
    v = ftv.FlashTradeView(active_threads={}, accounts_model=am)
    qtbot.addWidget(v)
    v._model_pin = am
    worker = FakeWorker()
    monkeypatch.setattr(v, "_find_worker_for_account", lambda a: worker)
    monkeypatch.setattr(v, "_partition_accounts_by_connection",
                        lambda accts: (list(accts), []))
    monkeypatch.setattr(v, "_ensure_worker_connected", lambda w: None)
    v._order_entry._price_cache.update({
        "NVDA": (220.0, 219.9, 220.1),
        "SGOV": (100.5, 100.49, 100.51),
        "BIL": (91.6, 91.59, 91.61),
    })
    held = {"SGOV": 150.0, "BIL": 200.0}
    monkeypatch.setattr(v._account_position, "held_shares",
                        lambda acct, sym: held.get(sym.upper(), 0.0))
    answers = {"reply": True, "plans": []}
    def _confirm(parent, plan, alias=""):
        answers["plans"].append(plan)
        return answers["reply"]
    monkeypatch.setattr(ftv, "confirm_cash_cover", _confirm)
    v._COVER_BUY_DELAY_MS = 0
    msgs: list[str] = []
    v.transient_status_message.connect(lambda m, _ms=0: msgs.append(m))
    v._account_list._select_default_group("All")
    return v, am, worker, held, answers, msgs


def _cash_account(am, available: float) -> None:
    am.update_batch([("L", "A1", "AvailableFunds", str(available), "USD"),
                     ("L", "A1", "BuyingPower", str(available), "USD")])


def _margin_account(am, available: float) -> None:
    am.update_batch([("L", "A1", "AvailableFunds", str(available), "USD"),
                     ("L", "A1", "BuyingPower", str(available * 4), "USD")])


def _buy(qty=100, **over) -> dict:
    p = {"action": "BUY", "type": "LMT", "symbol": "NVDA", "qty": qty,
         "last_price": 220.0, "limit_price": 220.0,
         "profit_take": None, "stop_loss": None, "trailing": None}
    p.update(over)
    return p


def _fill(v, ref, status="Filled"):
    v._on_order_status_updated({"order_id": f"A1_{ref}", "order_ref": ref,
                                "account": "A1", "status": status,
                                "symbol": "SGOV"})


class TestWhenTheDialogIsSkipped:
    def test_margin_account_buys_directly(self, rig):
        v, am, worker, _, answers, _ = rig
        _margin_account(am, 5000)
        v._on_order_requested(_buy())
        assert [o["action"] for o in worker.orders] == ["BUY"]
        assert answers["plans"] == []

    def test_enough_cash_buys_directly(self, rig):
        v, am, worker, _, answers, _ = rig
        _cash_account(am, 30000)
        v._on_order_requested(_buy())
        assert [o["action"] for o in worker.orders] == ["BUY"]
        assert answers["plans"] == []

    def test_no_sweep_holdings_lets_ib_decide(self, rig):
        v, am, worker, held, answers, _ = rig
        _cash_account(am, 5000)
        held.clear()
        v._on_order_requested(_buy())
        assert [o["action"] for o in worker.orders] == ["BUY"]
        assert answers["plans"] == []

    def test_two_accounts_selected_never_prompts(self, rig, monkeypatch):
        v, am, worker, _, answers, _ = rig
        _cash_account(am, 5000)
        monkeypatch.setattr(v._account_list, "get_selected_accounts",
                            lambda: ["A1", "A2"])
        v._on_order_requested(_buy())
        assert answers["plans"] == []
        assert len(worker.orders) == 2

    def test_sell_orders_never_prompt(self, rig):
        v, am, worker, _, answers, _ = rig
        _cash_account(am, 5000)
        v._on_order_requested(_buy(action="SELL"))
        assert answers["plans"] == [] and len(worker.orders) == 1


class TestCoverFlow:
    def test_decline_sends_nothing(self, rig):
        v, am, worker, _, answers, msgs = rig
        _cash_account(am, 5000)
        answers["reply"] = False
        v._on_order_requested(_buy())
        assert worker.orders == []
        assert len(answers["plans"]) == 1
        assert msgs and "cancelled" in msgs[-1]
        assert v._cash_cover is None

    def test_accept_sells_first_then_buys_on_fill(self, rig, qtbot):
        v, am, worker, _, answers, msgs = rig
        _cash_account(am, 5000)
        v._on_order_requested(_buy())
        sells = [o for o in worker.orders if o["action"] == "SELL"]
        assert [(o["symbol"], o["qty"], o["account"]) for o in sells] == [
            ("SGOV", 150, "A1"), ("BIL", 25, "A1")]
        assert all(o.get("order_ref") for o in sells)
        assert [o for o in worker.orders if o["action"] == "BUY"] == []
        assert v._cash_cover is not None

        _fill(v, sells[0]["order_ref"])
        assert [o for o in worker.orders if o["action"] == "BUY"] == []
        _fill(v, sells[1]["order_ref"])
        qtbot.waitUntil(
            lambda: any(o["action"] == "BUY" for o in worker.orders),
            timeout=2000)
        buy = [o for o in worker.orders if o["action"] == "BUY"][0]
        assert (buy["symbol"], buy["qty"], buy["account"]) == ("NVDA", 100, "A1")
        assert v._cash_cover is None

    def test_cancelled_sell_abandons_the_buy(self, rig, qtbot):
        v, am, worker, _, _, msgs = rig
        _cash_account(am, 5000)
        v._on_order_requested(_buy())
        sells = [o for o in worker.orders if o["action"] == "SELL"]
        _fill(v, sells[0]["order_ref"])
        _fill(v, sells[1]["order_ref"], status="Cancelled")
        qtbot.wait(50)
        assert [o for o in worker.orders if o["action"] == "BUY"] == []
        assert v._cash_cover is None
        assert "NOT bought" in msgs[-1]

    def test_insufficient_holdings_shrink_the_buy(self, rig, qtbot):
        v, am, worker, held, answers, _ = rig
        _cash_account(am, 5000)
        held.clear(); held["SGOV"] = 50.0
        v._on_order_requested(_buy())
        plan = answers["plans"][0]
        assert plan.shrunk and plan.buy_qty == 45
        sells = [o for o in worker.orders if o["action"] == "SELL"]
        assert [(o["symbol"], o["qty"]) for o in sells] == [("SGOV", 50)]
        _fill(v, sells[0]["order_ref"])
        qtbot.waitUntil(
            lambda: any(o["action"] == "BUY" for o in worker.orders),
            timeout=2000)
        assert [o["qty"] for o in worker.orders if o["action"] == "BUY"] == [45]

    def test_nothing_fits_sends_nothing(self, rig):
        v, am, worker, held, answers, msgs = rig
        _cash_account(am, 100)
        held.clear(); held["SGOV"] = 5.0
        v._on_order_requested(_buy(qty=1, limit_price=700000.0,
                                   last_price=700000.0))
        assert worker.orders == [] and answers["plans"] == []
        assert "not enough cash" in msgs[-1]

    def test_market_buy_is_checked_at_ask_plus_cushion(self, rig):
        v, am, worker, _, answers, _ = rig
        # 100 × 220.1 × 1.05 = 23,110 > 23,000 available → prompts.
        _cash_account(am, 23000)
        v._on_order_requested(_buy(type="MKT", limit_price=0.0))
        assert len(answers["plans"]) == 1
        assert answers["plans"][0].unit_price == pytest.approx(220.1 * 1.05)

    def test_timeout_abandons_the_buy(self, rig, qtbot):
        v, am, worker, _, _, msgs = rig
        _cash_account(am, 5000)
        v._COVER_TIMEOUT_MS = 50
        v._on_order_requested(_buy())
        qtbot.waitUntil(lambda: v._cash_cover is None, timeout=2000)
        assert [o for o in worker.orders if o["action"] == "BUY"] == []
        assert "did not fill in time" in msgs[-1]
