"""The "Orders on All accounts" guard (preferences.trading_guards).

When ``allow_all_group_orders`` is off, a Buy/Sell whose account
fan-out covers every visible account — what the built-in All pill
selects — is refused with a status-bar message instead of being sent.

Explicitly NOT guarded: an active user-defined group (a deliberate
fan-out target), payloads carrying ``_target_accounts`` (group hot
buttons, sweeps, options tickets) and ``_flatten_account`` (close legs).
"""
from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QObject, Signal

from src.core import config_manager
from src.core.trading_prefs import trading_prefs
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


@pytest.fixture(autouse=True)
def _restore_guard():
    """The prefs singleton is process-wide — put it back afterwards."""
    yield
    trading_prefs.update(guards={"allow_all_group_orders": True})


def _block_orders() -> None:
    trading_prefs.update(guards={"allow_all_group_orders": False})


def _make_view(qtbot, tmp_path, monkeypatch, accounts=("A1", "A2", "A3")):
    monkeypatch.setattr(config_manager, "_active_path", tmp_path / "cfg.json")
    if not (tmp_path / "cfg.json").exists():
        (tmp_path / "cfg.json").write_text(json.dumps({}))
    am = AccountsTableModel()
    am.update_batch([("L", a, "NetLiquidation", "1000000", "USD")
                     for a in accounts])
    from src.ui.widgets.flash_trade_view import FlashTradeView
    v = FlashTradeView(active_threads={}, accounts_model=am)
    qtbot.addWidget(v)
    v._model_pin = am  # pin: pytest-qt holds widgets weakly
    workers = {a: FakeWorker() for a in accounts}
    monkeypatch.setattr(v, "_find_worker_for_account", lambda a: workers.get(a))
    monkeypatch.setattr(v, "_partition_accounts_by_connection",
                        lambda accts: (list(accts), []))
    monkeypatch.setattr(v, "_ensure_worker_connected", lambda w: None)
    msgs: list[str] = []
    v.transient_status_message.connect(lambda m, _ms=0: msgs.append(m))
    return v, workers, msgs


@pytest.fixture
def view(qtbot, tmp_path, monkeypatch):
    return _make_view(qtbot, tmp_path, monkeypatch)


def _payload(**over) -> dict:
    """A plain Buy from the order-entry panel — no account override, so
    it resolves through AccountListPanel (the guarded path)."""
    p = {
        "action": "BUY", "type": "LMT", "symbol": "NVDA",
        "qty": 10, "sec_type": "STK",
        "last_price": 182.40, "limit_price": 182.40,
        "profit_take": None, "stop_loss": None, "trailing": None,
        "_resolved_contract": {
            "conid": 4815747, "symbol": "NVDA", "sec_type": "STK",
            "exchange": "SMART", "currency": "USD",
            "local_symbol": "", "expiry": "", "multiplier": "",
        },
    }
    p.update(over)
    return p


def _orders(workers) -> list[dict]:
    return [o for w in workers.values() for o in w.orders]


class TestAllAccountsGuard:
    def test_all_checked_blocked_when_off(self, view):
        v, workers, msgs = view
        v._account_list._select_default_group("All")
        _block_orders()
        v._on_order_requested(_payload())
        assert _orders(workers) == []
        assert len(msgs) == 1 and "all accounts" in msgs[0]

    def test_all_checked_allowed_by_default(self, view):
        """Default is On — existing users see no behaviour change."""
        v, workers, msgs = view
        v._account_list._select_default_group("All")
        v._on_order_requested(_payload())
        assert len(_orders(workers)) == 3
        assert msgs == []

    def test_subset_allowed_when_off(self, view):
        v, workers, _ = view
        panel = v._account_list
        panel._select_default_group("All")
        panel._on_row_toggled("A3", False)
        panel._account_rows["A3"].set_checked(False)
        _block_orders()
        v._on_order_requested(_payload())
        assert sorted(o["account"] for o in _orders(workers)) == ["A1", "A2"]

    def test_single_account_never_blocked(self, qtbot, tmp_path, monkeypatch):
        v, workers, _ = _make_view(qtbot, tmp_path, monkeypatch,
                                   accounts=("A1",))
        v._account_list._select_default_group("All")
        _block_orders()
        v._on_order_requested(_payload())
        assert len(_orders(workers)) == 1

    def test_fully_checked_user_group_allowed(self, qtbot, tmp_path,
                                              monkeypatch):
        """A group IS the fan-out target — its rows are the only ones
        visible, so "all visible checked" is its normal state."""
        monkeypatch.setattr(config_manager, "_active_path",
                            tmp_path / "cfg.json")
        (tmp_path / "cfg.json").write_text(json.dumps({}))
        config_manager.save_account_groups(
            [{"name": "Beta", "accounts": ["A1", "A2"],
              "selected": ["A1", "A2"]}])
        v, workers, msgs = _make_view(qtbot, tmp_path, monkeypatch)
        v._account_list._select_group("Beta")
        _block_orders()
        v._on_order_requested(_payload())
        assert sorted(o["account"] for o in _orders(workers)) == ["A1", "A2"]
        assert msgs == []


class TestUnguardedPaths:
    def test_target_accounts_override_allowed(self, view):
        """Group hot buttons / sweeps / options tickets name their
        accounts explicitly and bypass the guard by design."""
        v, workers, _ = view
        v._account_list._select_default_group("All")
        _block_orders()
        v._on_order_requested(_payload(_target_accounts=["A1", "A2", "A3"]))
        assert len(_orders(workers)) == 3

    def test_flatten_account_override_allowed(self, view):
        v, workers, _ = view
        v._account_list._select_default_group("All")
        _block_orders()
        v._on_order_requested(_payload(action="SELL", _flatten_account="A1"))
        assert [o["account"] for o in _orders(workers)] == ["A1"]


class TestBookTraderSharesTheGuard:
    def test_ladder_click_is_guarded(self, view):
        """BookTraderView.order_requested is wired into the same slot,
        so ladder clicks inherit the guard."""
        v, workers, msgs = view
        v._account_list._select_default_group("All")
        _block_orders()
        v._book_trading.order_requested.emit(_payload())
        assert _orders(workers) == []
        assert len(msgs) == 1


class TestOrderGuardsSettings:
    """The Settings → Trading → Order Guards block round-trips the pref."""

    def test_toggle_reflects_stored_value(self, qtbot):
        from src.ui.widgets.preferences_dialog import _OrderGuardsGroup
        g = _OrderGuardsGroup(
            {"trading_guards": {"allow_all_group_orders": False}})
        qtbot.addWidget(g)
        assert g._all_toggle.value() == "Off"
        assert g.collect() == {"allow_all_group_orders": False}

    def test_defaults_to_on_when_section_absent(self, qtbot):
        from src.ui.widgets.preferences_dialog import _OrderGuardsGroup
        g = _OrderGuardsGroup({})
        qtbot.addWidget(g)
        assert g.collect() == {"allow_all_group_orders": True}

    def test_trading_page_collects_guards(self, qtbot):
        from src.ui.widgets.preferences_dialog import _TradingPage
        page = _TradingPage({})
        qtbot.addWidget(page)
        futures, stocks, guards = page.collect()
        assert futures["quantity_unit"] == "Contracts"
        assert stocks["quantity_unit"] == "Shares"
        assert guards == {"allow_all_group_orders": True}
        page._guards_group._all_toggle._on_click("Off")
        assert page.collect()[2] == {"allow_all_group_orders": False}


class TestSelectionHelper:
    def test_hand_checking_every_row_counts_as_full_fanout(self, view):
        """No pill active — the guard follows the actual selection, not
        the pill, because the All pill stays lit after unchecking rows."""
        panel = view[0]._account_list
        assert panel.is_full_fanout_selection() is False   # all unchecked
        for acct, row in panel._account_rows.items():
            panel._on_row_toggled(acct, True)
            row.set_checked(True)
        assert panel.is_full_fanout_selection() is True

    def test_none_pill_is_not_a_full_fanout(self, view):
        v, _, _ = view
        v._account_list._select_default_group("None")
        assert v._account_list.is_full_fanout_selection() is False
