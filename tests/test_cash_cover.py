"""Covering a cash-account stock buy by selling sweep holdings first.

A cash account (no margin, every registered plan) can only buy with the
cash it has.  When a plain Buy fans out to the selected accounts and a
cash account among them needs more than its AvailableFunds,
FlashTradeView offers — in one dialog for the whole click — to sell that
account's configured sweep symbols (SGOV, BIL, …) first, sized like the
Accounts panel's Sweep out, and sends its buy automatically once its
sells have filled.  Margin accounts and funded cash accounts are bought
right away; each covered account runs on its own.  Holdings that cannot
cover it are sold in full and that buy shrinks to what the cash plus
proceeds affords.  Cancel sends nothing to anyone.  Flatten legs, hot
buttons, sweeps and the options ticket never see the dialog.
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

_ACCOUNTS = ("A1", "A2", "A3")


@pytest.fixture
def rig(qtbot, tmp_path, monkeypatch):
    """A1 and A3 are cash accounts, A2 is margin; selection defaults to
    A1 alone (``select(...)`` widens it).  A1 holds 150 SGOV + 200 BIL,
    A3 holds 100 SGOV."""
    monkeypatch.setattr(config_manager, "_active_path", tmp_path / "cfg.json")
    (tmp_path / "cfg.json").write_text(json.dumps({}))
    import src.core.instruments as instruments
    monkeypatch.setattr(instruments, "get_contract",
                        lambda sym: _CONTRACTS.get((sym or "").upper()))
    # Regular hours unless a test opts into the overnight session.
    import src.core.ib_worker as ib_worker
    monkeypatch.setattr(ib_worker, "stk_routes_overnight",
                        lambda currency="USD", now=None: False)
    monkeypatch.setattr(CashManagementPreferences, "sweep_symbols",
                        property(lambda self: ["SGOV", "BIL"]))
    am = AccountsTableModel()
    am.update_batch([("L", a, "NetLiquidation", "60000", "USD")
                     for a in _ACCOUNTS])
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
    held = {("A1", "SGOV"): 150.0, ("A1", "BIL"): 200.0, ("A3", "SGOV"): 100.0}
    monkeypatch.setattr(v._account_position, "held_shares",
                        lambda acct, sym: held.get((acct, sym.upper()), 0.0))
    answers = {"reply": True, "asks": []}
    def _confirm(parent, plans, aliases=None, immediate=None):
        answers["asks"].append((list(plans), list(immediate or [])))
        return answers["reply"]
    monkeypatch.setattr(ftv, "confirm_cash_cover", _confirm)
    v._COVER_BUY_DELAY_MS = 0
    msgs: list[str] = []
    v.transient_status_message.connect(lambda m, _ms=0: msgs.append(m))
    selection = ["A1"]
    monkeypatch.setattr(v._account_list, "get_selected_accounts",
                        lambda: list(selection))
    monkeypatch.setattr(v._account_list, "is_full_fanout_selection",
                        lambda: False)
    v._selection = selection
    return v, am, worker, held, answers, msgs


def _cash_account(am, account: str, available: float) -> None:
    am.update_batch([("L", account, "AvailableFunds", str(available), "USD"),
                     ("L", account, "BuyingPower", str(available), "USD")])


def _margin_account(am, account: str, available: float) -> None:
    am.update_batch([("L", account, "AvailableFunds", str(available), "USD"),
                     ("L", account, "BuyingPower", str(available * 4), "USD")])


def _buy(qty=100, **over) -> dict:
    p = {"action": "BUY", "type": "LMT", "symbol": "NVDA", "qty": qty,
         "last_price": 220.0, "limit_price": 220.0,
         "profit_take": None, "stop_loss": None, "trailing": None}
    p.update(over)
    return p


def _fill(v, order, status="Filled"):
    v._on_order_status_updated({
        "order_id": f"{order['account']}_{order['order_ref']}",
        "order_ref": order["order_ref"], "account": order["account"],
        "status": status, "symbol": order["symbol"]})


def _sells(worker, account=None):
    return [o for o in worker.orders if o["action"] == "SELL"
            and (account is None or o["account"] == account)]


def _buys(worker, account=None):
    return [o for o in worker.orders if o["action"] == "BUY"
            and (account is None or o["account"] == account)]


class TestWhenTheDialogIsSkipped:
    def test_margin_account_buys_directly(self, rig):
        v, am, worker, _, answers, _ = rig
        _margin_account(am, "A1", 5000)
        v._on_order_requested(_buy())
        assert [o["action"] for o in worker.orders] == ["BUY"]
        assert answers["asks"] == []

    def test_enough_cash_buys_directly(self, rig):
        v, am, worker, _, answers, _ = rig
        _cash_account(am, "A1", 30000)
        v._on_order_requested(_buy())
        assert [o["action"] for o in worker.orders] == ["BUY"]
        assert answers["asks"] == []

    def test_no_sweep_holdings_lets_ib_decide(self, rig):
        v, am, worker, held, answers, _ = rig
        _cash_account(am, "A1", 5000)
        held.clear()
        v._on_order_requested(_buy())
        assert [o["action"] for o in worker.orders] == ["BUY"]
        assert answers["asks"] == []

    def test_sell_orders_never_prompt(self, rig):
        v, am, worker, _, answers, _ = rig
        _cash_account(am, "A1", 5000)
        v._on_order_requested(_buy(action="SELL"))
        assert answers["asks"] == [] and len(worker.orders) == 1

    def test_target_accounts_payloads_never_prompt(self, rig):
        """Hot buttons / sweeps / options ticket name their accounts."""
        v, am, worker, _, answers, _ = rig
        _cash_account(am, "A1", 5000)
        v._on_order_requested(_buy(_target_accounts=["A1"]))
        assert answers["asks"] == [] and len(worker.orders) == 1


class TestSingleAccountCover:
    def test_decline_sends_nothing(self, rig):
        v, am, worker, _, answers, msgs = rig
        _cash_account(am, "A1", 5000)
        answers["reply"] = False
        v._on_order_requested(_buy())
        assert worker.orders == []
        assert len(answers["asks"]) == 1
        assert msgs and "cancelled" in msgs[-1]
        assert v._cash_cover == {}

    def test_accept_sells_first_then_buys_on_fill(self, rig, qtbot):
        v, am, worker, _, answers, msgs = rig
        _cash_account(am, "A1", 5000)
        v._on_order_requested(_buy())
        sells = _sells(worker)
        assert [(o["symbol"], o["qty"], o["account"]) for o in sells] == [
            ("SGOV", 150, "A1"), ("BIL", 25, "A1")]
        assert all(o.get("order_ref") for o in sells)
        assert _buys(worker) == []
        assert "A1" in v._cash_cover

        _fill(v, sells[0])
        assert _buys(worker) == []
        _fill(v, sells[1])
        qtbot.waitUntil(lambda: bool(_buys(worker)), timeout=2000)
        buy = _buys(worker)[0]
        assert (buy["symbol"], buy["qty"], buy["account"]) == ("NVDA", 100, "A1")
        assert v._cash_cover == {}

    def test_cancelled_sell_abandons_the_buy(self, rig, qtbot):
        v, am, worker, _, _, msgs = rig
        _cash_account(am, "A1", 5000)
        v._on_order_requested(_buy())
        sells = _sells(worker)
        _fill(v, sells[0])
        _fill(v, sells[1], status="Cancelled")
        qtbot.wait(50)
        assert _buys(worker) == []
        assert v._cash_cover == {}
        assert "NOT bought" in msgs[-1]

    def test_insufficient_holdings_shrink_the_buy(self, rig, qtbot):
        v, am, worker, held, answers, _ = rig
        _cash_account(am, "A1", 5000)
        held.clear(); held[("A1", "SGOV")] = 50.0
        v._on_order_requested(_buy())
        plan = answers["asks"][0][0][0]
        assert plan.shrunk and plan.buy_qty == 45
        assert [(o["symbol"], o["qty"]) for o in _sells(worker)] == [("SGOV", 50)]
        _fill(v, _sells(worker)[0])
        qtbot.waitUntil(lambda: bool(_buys(worker)), timeout=2000)
        assert [o["qty"] for o in _buys(worker)] == [45]

    def test_nothing_fits_sends_nothing(self, rig):
        v, am, worker, held, answers, msgs = rig
        _cash_account(am, "A1", 100)
        held.clear(); held[("A1", "SGOV")] = 5.0
        v._on_order_requested(_buy(qty=1, limit_price=700000.0,
                                   last_price=700000.0))
        assert worker.orders == [] and answers["asks"] == []
        assert "not enough cash" in msgs[-1]

    def test_market_buy_is_checked_at_ask_plus_cushion(self, rig):
        v, am, worker, _, answers, _ = rig
        # 100 × 220.1 × 1.05 = 23,110 > 23,000 available → prompts.
        _cash_account(am, "A1", 23000)
        v._on_order_requested(_buy(type="MKT", limit_price=0.0))
        assert len(answers["asks"]) == 1
        assert answers["asks"][0][0][0].unit_price == pytest.approx(220.1 * 1.05)

    def test_timeout_abandons_the_buy(self, rig, qtbot):
        v, am, worker, _, _, msgs = rig
        _cash_account(am, "A1", 5000)
        v._COVER_TIMEOUT_MS = 50
        v._on_order_requested(_buy())
        qtbot.waitUntil(lambda: v._cash_cover == {}, timeout=2000)
        assert _buys(worker) == []
        assert "did not fill in time" in msgs[-1]

    def test_second_click_while_cover_in_flight_is_refused(self, rig):
        v, am, worker, _, answers, msgs = rig
        _cash_account(am, "A1", 5000)
        v._on_order_requested(_buy())
        n = len(worker.orders)
        v._on_order_requested(_buy())
        assert len(worker.orders) == n and len(answers["asks"]) == 1
        assert "already in progress" in msgs[-1]


class TestOvernightSession:
    """IBEOS (20:00–03:59 ET) refuses market orders (error 387), which
    abandoned every overnight cover — the sweep sells go out as limits at
    the bid instead."""

    @pytest.fixture
    def overnight(self, monkeypatch):
        import src.core.ib_worker as ib_worker
        monkeypatch.setattr(ib_worker, "stk_routes_overnight",
                            lambda currency="USD", now=None: True)

    def test_sells_are_limits_at_the_bid(self, rig, overnight, qtbot):
        v, am, worker, _, answers, _ = rig
        _cash_account(am, "A1", 5000)
        v._on_order_requested(_buy())
        sells = _sells(worker)
        assert [(o["symbol"], o["order_type"], o["limit_price"])
                for o in sells] == [("SGOV", "LMT", 100.49),
                                    ("BIL", "LMT", 91.59)]
        assert all(o.get("order_ref") for o in sells)
        # Sized at the bid too, so the proceeds the dialog shows are what
        # the limits fetch.
        plan = answers["asks"][0][0][0]
        assert [s.price for s in plan.sells] == [100.49, 91.59]
        for o in sells:
            _fill(v, o)
        qtbot.waitUntil(lambda: bool(_buys(worker)), timeout=2000)
        assert _buys(worker)[0]["qty"] == 100

    def test_regular_hours_sells_stay_market(self, rig):
        v, am, worker, _, _, _ = rig
        _cash_account(am, "A1", 5000)
        v._on_order_requested(_buy())
        assert {o["order_type"] for o in _sells(worker)} == {"MKT"}


    def test_sweep_out_is_a_limit_at_the_bid(self, rig, overnight):
        v, _, worker, _, _, _ = rig
        v._on_sweep_requested("A1", "out", 150, "SGOV")
        (o,) = worker.orders
        assert (o["action"], o["order_type"], o["qty"], o["limit_price"]) \
            == ("SELL", "LMT", 150, 100.49)

    def test_sweep_out_without_a_price_sends_nothing(self, rig, overnight):
        v, _, worker, _, _, msgs = rig
        v._order_entry._price_cache.pop("SGOV")
        v._on_sweep_requested("A1", "out", 150, "SGOV")
        assert worker.orders == [] and "not sent" in msgs[-1]

    def test_regular_hours_sweep_out_stays_market(self, rig):
        v, _, worker, _, _, _ = rig
        v._on_sweep_requested("A1", "out", 150, "SGOV")
        assert [o["order_type"] for o in worker.orders] == ["MKT"]


class TestStkRoutesOvernight:
    @pytest.mark.parametrize("hh,mm,expected", [
        (19, 59, False), (20, 0, True), (0, 44, True), (3, 59, True),
        (4, 0, False), (12, 0, False)])
    def test_session_window(self, hh, mm, expected):
        from datetime import datetime
        from src.core.ib_worker import _ET, stk_routes_overnight
        now = datetime(2026, 10, 16, hh, mm, tzinfo=_ET)
        assert stk_routes_overnight("USD", now) is expected

    def test_non_usd_never_overnight(self):
        from datetime import datetime
        from src.core.ib_worker import _ET, stk_routes_overnight
        now = datetime(2026, 10, 16, 1, 0, tzinfo=_ET)
        assert stk_routes_overnight("CAD", now) is False


class TestMixedSelection:
    def _mixed(self, rig):
        v, am, *_ = rig
        v._selection[:] = ["A1", "A2", "A3"]
        _cash_account(am, "A1", 5000)       # short, holds SGOV + BIL
        _margin_account(am, "A2", 5000)     # margin → immediate
        _cash_account(am, "A3", 30000)      # funded cash → immediate

    def test_one_dialog_lists_covers_and_immediate_accounts(self, rig):
        v, am, worker, _, answers, _ = rig
        self._mixed(rig)
        v._on_order_requested(_buy())
        plans, immediate = answers["asks"][0]
        assert [p.account for p in plans] == ["A1"]
        assert immediate == ["A2", "A3"]

    def test_confirm_buys_immediate_accounts_now_and_covers_the_rest(
            self, rig, qtbot):
        v, am, worker, _, _, _ = rig
        self._mixed(rig)
        v._on_order_requested(_buy())
        assert sorted(o["account"] for o in _buys(worker)) == ["A2", "A3"]
        assert [o["account"] for o in _sells(worker)] == ["A1", "A1"]
        for o in _sells(worker):
            _fill(v, o)
        qtbot.waitUntil(lambda: bool(_buys(worker, "A1")), timeout=2000)
        assert _buys(worker, "A1")[0]["qty"] == 100
        assert v._cash_cover == {}

    def test_cancel_sends_nothing_to_anyone(self, rig):
        v, am, worker, _, answers, msgs = rig
        self._mixed(rig)
        answers["reply"] = False
        v._on_order_requested(_buy())
        assert worker.orders == []
        assert "cancelled" in msgs[-1]

    def test_two_cash_accounts_cover_independently(self, rig, qtbot):
        v, am, worker, _, answers, msgs = rig
        v._selection[:] = ["A1", "A3"]
        _cash_account(am, "A1", 5000)
        _cash_account(am, "A3", 15000)      # short 7,000, holds 100 SGOV
        v._on_order_requested(_buy())
        plans, immediate = answers["asks"][0]
        assert [p.account for p in plans] == ["A1", "A3"] and immediate == []
        assert [(o["account"], o["symbol"], o["qty"]) for o in _sells(worker)] == [
            ("A1", "SGOV", 150), ("A1", "BIL", 25), ("A3", "SGOV", 70)]
        assert set(v._cash_cover) == {"A1", "A3"}
        # A3's sell fills → only A3 buys; A1 still waits.
        _fill(v, _sells(worker, "A3")[0])
        qtbot.waitUntil(lambda: bool(_buys(worker, "A3")), timeout=2000)
        assert _buys(worker, "A1") == [] and set(v._cash_cover) == {"A1"}
        # A1's BIL sell is cancelled → A1 abandoned, A3's buy already out.
        _fill(v, _sells(worker, "A1")[0])
        _fill(v, _sells(worker, "A1")[1], status="Cancelled")
        qtbot.wait(50)
        assert _buys(worker, "A1") == [] and v._cash_cover == {}
        assert "NOT bought" in msgs[-1]

    def test_unaffordable_account_is_dropped_others_proceed(self, rig):
        v, am, worker, held, answers, msgs = rig
        v._selection[:] = ["A1", "A2"]
        _cash_account(am, "A1", 100)
        _margin_account(am, "A2", 5000)
        held.clear(); held[("A1", "SGOV")] = 5.0
        v._on_order_requested(_buy(qty=1, limit_price=700000.0,
                                   last_price=700000.0))
        assert answers["asks"] == []
        assert [o["account"] for o in _buys(worker)] == ["A2"]
        assert "not sent to A1" in msgs[-1]


class TestCashCoverDialog:
    def _texts(self, dlg):
        from PySide6.QtWidgets import QLabel
        return [lbl.text() for lbl in dlg.findChildren(QLabel)]

    def test_cards_steps_and_button(self, qtbot):
        from src.core.cash_cover import plan_cash_cover
        from src.ui.widgets.cash_cover_dialog import CashCoverDialog
        full = plan_cash_cover("U1", "NVDA", 100, 212.0, 10,
                               [("SGOV", 2615, 100.545)])
        part = plan_cash_cover("U2", "NVDA", 100, 212.0, 65,
                               [("SGOV", 95, 100.545)])
        dlg = CashCoverDialog([full, part], {"U1": "rsp-alex-01"},
                              ["mgn-zara-07"])
        qtbot.addWidget(dlg)
        texts = self._texts(dlg)
        assert "Not enough cash in 2 accounts" in texts
        assert "rsp-alex-01" in texts and "U2" in texts
        assert "Full buy" in texts
        assert f"Buy reduced to {part.buy_qty}" in texts
        assert "Sell 215 SGOV" in texts and "Buy 100 NVDA" in texts
        assert any("right away in mgn-zara-07" in t for t in texts)
        assert dlg._ok.text() == "Sell && Buy in 2 accounts"

    def test_ok_accepts_and_cancel_rejects(self, qtbot):
        from src.core.cash_cover import plan_cash_cover
        from src.ui.widgets.cash_cover_dialog import CashCoverDialog
        plan = plan_cash_cover("U1", "NVDA", 100, 212.0, 10,
                               [("SGOV", 2615, 100.545)])
        dlg = CashCoverDialog([plan])
        qtbot.addWidget(dlg)
        dlg._ok.click()
        assert dlg.result() == dlg.DialogCode.Accepted
