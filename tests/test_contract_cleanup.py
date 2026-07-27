"""Tests for config_manager.remove_stock_contract_if_unused — the
chip-removal → contracts-DB cleanup path."""
from types import SimpleNamespace

import pytest

from src.core import config_manager as cm


@pytest.fixture
def env(monkeypatch):
    """Fake persistence: chip lists, hot buttons, contracts DB."""
    state = {
        "order_entry": [],
        "book_trader": [],
        "hot_buttons": [],
        "contracts": [
            {"conid": "1", "symbol": "MSFT", "sec_type": "STK"},
            {"conid": "2", "symbol": "NQ", "sec_type": "FUT"},
        ],
        "saved": None,
        "reloaded": False,
    }
    monkeypatch.setattr(cm, "load_order_entry_symbols",
                        lambda path=None: list(state["order_entry"]))
    monkeypatch.setattr(cm, "load_book_trader_symbols",
                        lambda path=None: list(state["book_trader"]))
    monkeypatch.setattr(cm, "load_hot_buttons",
                        lambda path=None: list(state["hot_buttons"]))
    monkeypatch.setattr(cm, "load_contracts",
                        lambda path=None: list(state["contracts"]))

    def _save(contracts, path=None):
        state["saved"] = contracts
    monkeypatch.setattr(cm, "save_contracts", _save)

    import src.core.reference_data as rd

    def _reload():
        state["reloaded"] = True
    monkeypatch.setattr(
        rd, "get_instance",
        lambda: SimpleNamespace(reload_contracts=_reload))
    return state


def test_unreferenced_stock_is_deleted(env):
    assert cm.remove_stock_contract_if_unused("msft") is True
    assert [c["symbol"] for c in env["saved"]] == ["NQ"]
    assert env["reloaded"]


def test_kept_while_other_view_still_uses_it(env):
    env["book_trader"] = ["MSFT"]
    assert cm.remove_stock_contract_if_unused("MSFT") is False
    assert env["saved"] is None


def test_kept_while_order_entry_still_uses_it(env):
    env["order_entry"] = ["msft"]
    assert cm.remove_stock_contract_if_unused("MSFT") is False
    assert env["saved"] is None


def test_kept_while_enabled_hot_button_references_it(env):
    env["hot_buttons"] = [{"symbol": "MSFT", "disabled": False}]
    assert cm.remove_stock_contract_if_unused("MSFT") is False
    assert env["saved"] is None


def test_disabled_hot_button_does_not_block_deletion(env):
    env["hot_buttons"] = [{"symbol": "MSFT", "disabled": True}]
    assert cm.remove_stock_contract_if_unused("MSFT") is True
    assert [c["symbol"] for c in env["saved"]] == ["NQ"]


def test_futures_are_never_deleted(env):
    assert cm.remove_stock_contract_if_unused("NQ") is False
    assert env["saved"] is None


def test_unknown_or_empty_symbol_is_noop(env):
    assert cm.remove_stock_contract_if_unused("ZZZZ") is False
    assert cm.remove_stock_contract_if_unused("") is False
    assert env["saved"] is None
