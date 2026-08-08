"""Tests for the account cash-sweep menu computation + rounding."""
from __future__ import annotations

import json

import pytest

from src.ui.widgets.account_list_panel import (
    AccountListPanel, _floor_to_5, _ceil_to_5,
)
from src.ui.widgets.accounts_model import AccountsTableModel
from src.core import config_manager


# ── rounding rules (spec) ─────────────────────────────────────────────────────

def test_floor_to_5():
    # Sweep in: 911→910, 919→915 (never buy beyond the cash)
    assert _floor_to_5(911) == 910
    assert _floor_to_5(919) == 915
    assert _floor_to_5(910) == 910
    assert _floor_to_5(911.7) == 910


def test_ceil_to_5():
    # Sweep out: 911→915, 919→920 (cover the whole deficit)
    assert _ceil_to_5(911) == 915
    assert _ceil_to_5(919) == 920
    assert _ceil_to_5(910) == 910
    assert _ceil_to_5(911.7) == 915


# ── menu computation ──────────────────────────────────────────────────────────

@pytest.fixture
def panel(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(config_manager, "_active_path", tmp_path / "cfg.json")
    (tmp_path / "cfg.json").write_text(json.dumps(
        {"preferences": {"cash_management": {"sweep_symbol": "SGOV"}}}))
    from src.core.cash_management import cash_prefs
    cash_prefs.reload()

    am = AccountsTableModel()
    am.update_batch([("L", "pos", "NetLiquidation", "1", "USD"),
                     ("L", "pos", "CashBalance", "91170", "USD")])
    am.update_batch([("L", "neg", "NetLiquidation", "1", "USD"),
                     ("L", "neg", "CashBalance", "-91170", "USD")])
    am.update_batch([("L", "flat", "NetLiquidation", "1", "USD"),
                     ("L", "flat", "CashBalance", "0", "USD")])
    p = AccountListPanel(am)
    p.set_sweep_quote_provider(
    lambda acct, sym: (100.0, 100.0, 2000.0))  # last, ask, held
    return p


def test_sweep_in_positive_balance(panel):
    info = panel.compute_sweep("pos")           # 91170 / 100 = 911.7 → floor5 910
    assert info["enabled"] is True
    assert info["direction"] == "in"
    assert info["qty"] == 910
    assert info["label"] == "Sweep in 910 SGOV"


def test_sweep_out_negative_balance(panel):
    info = panel.compute_sweep("neg")           # 91170 / 100 = 911.7 → ceil5 915
    assert info["enabled"] is True
    assert info["direction"] == "out"
    assert info["qty"] == 915
    assert info["label"] == "Sweep out 915 SGOV"


def test_sweep_out_sells_all_when_insufficient(panel):
    # Need 5000 shares but only hold 2000 → sell the entire position (2000).
    panel._model.update_batch([("L", "negbig", "NetLiquidation", "1", "USD"),
                               ("L", "negbig", "CashBalance", "-500000", "USD")])
    info = panel.compute_sweep("negbig")
    assert info["qty"] == 2000
    assert info["label"] == "Sweep out 2,000 SGOV"


def test_sweep_out_sells_all_non_multiple_of_5(panel):
    # Insufficient holding that isn't a multiple of 5 → sell all of it (1998),
    # not rounded.
    panel.set_sweep_quote_provider(lambda acct, sym: (100.0, 100.0, 1998.0))
    panel._model.update_batch([("L", "negbig", "NetLiquidation", "1", "USD"),
                               ("L", "negbig", "CashBalance", "-500000", "USD")])
    info = panel.compute_sweep("negbig")
    assert info["qty"] == 1998


def test_sweep_out_hidden_when_no_holdings(panel):
    # Negative USD but the account holds none of the sweep symbol → disabled.
    panel.set_sweep_quote_provider(lambda acct, sym: (100.0, 100.0, 0.0))
    info = panel.compute_sweep("neg")
    assert info["enabled"] is False
    assert "No SGOV shares" in info["label"]


def test_sweep_in_still_shown_without_holdings(panel):
    # Sweep IN doesn't require holding the symbol.
    panel.set_sweep_quote_provider(lambda acct, sym: (100.0, 100.0, 0.0))
    info = panel.compute_sweep("pos")
    assert info["enabled"] is True
    assert info["qty"] == 910


def test_flat_balance_disabled(panel):
    info = panel.compute_sweep("flat")
    assert info["enabled"] is False
    assert "flat" in info["label"].lower()


def test_no_price_disabled(panel):
    panel.set_sweep_quote_provider(lambda acct, sym: (0.0, 0.0))
    info = panel.compute_sweep("pos")
    assert info["enabled"] is False
    assert "no live price" in info["label"]


def test_no_symbol_disabled(panel, tmp_path):
    (tmp_path / "cfg.json").write_text(json.dumps(
        {"preferences": {"cash_management": {"sweep_symbol": ""}}}))
    from src.core.cash_management import cash_prefs
    cash_prefs.reload()
    info = panel.compute_sweep("pos")
    assert info["enabled"] is False
    assert "Settings" in info["label"]


# ── sweep-in limit pricing ────────────────────────────────────────────────────
# The buy goes out as a marketable LIMIT (no MKT ~5% cash cushion — that
# cushion rejects full-balance sweeps in no-margin TFSA/RSP accounts).

def test_sweep_in_limit_is_the_ask(panel):
    panel.set_sweep_quote_provider(lambda acct, sym: (100.43, 100.45, 0.0))
    info = panel.compute_sweep("pos")
    assert info["limit_price"] == 100.45
    # Sized by the limit price, not last: floor5(91170 / 100.45) = 905.
    assert info["qty"] == 905


def test_sweep_in_no_ask_uses_last_plus_two_cents(panel):
    panel.set_sweep_quote_provider(lambda acct, sym: (100.43, 0.0, 0.0))
    info = panel.compute_sweep("pos")
    assert info["limit_price"] == 100.45
    assert info["qty"] == 905


def test_sweep_in_no_price_at_all_disabled(panel):
    panel.set_sweep_quote_provider(lambda acct, sym: (0.0, 0.0, 0.0))
    info = panel.compute_sweep("pos")
    assert info["enabled"] is False
    assert "no live price" in info["label"]


def test_sweep_out_stays_market(panel):
    # Sells have no cash check to trip — limit_price stays 0 (MKT), and
    # sizing uses last so the deficit estimate matches the tape.
    panel.set_sweep_quote_provider(lambda acct, sym: (100.0, 100.45, 2000.0))
    info = panel.compute_sweep("neg")
    assert info["enabled"] is True
    assert info["limit_price"] == 0.0
    assert info["qty"] == 915  # ceil5(91170 / 100.0)


def test_legacy_two_tuple_provider_disables_gracefully(panel):
    # A provider still returning (price, held) must not crash the menu.
    panel.set_sweep_quote_provider(lambda acct, sym: (100.0, 2000.0))
    info = panel.compute_sweep("pos")
    assert info["enabled"] is False


# ── multiple sweep symbols (max 4) ────────────────────────────────────────────

@pytest.fixture
def multi_panel(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(config_manager, "_active_path", tmp_path / "cfg.json")
    (tmp_path / "cfg.json").write_text(json.dumps(
        {"preferences": {"cash_management": {
            "sweep_symbols": ["SGOV", "BIL"]}}}))
    from src.core.cash_management import cash_prefs
    cash_prefs.reload()

    am = AccountsTableModel()
    am.update_batch([("L", "pos", "NetLiquidation", "1", "USD"),
                     ("L", "pos", "CashBalance", "91170", "USD")])
    am.update_batch([("L", "neg", "NetLiquidation", "1", "USD"),
                     ("L", "neg", "CashBalance", "-91170", "USD")])
    p = AccountListPanel(am)
    # SGOV $100, 2000 held; BIL $50, nothing held.
    p.set_sweep_quote_provider(
        lambda acct, sym: (100.0, 100.0, 2000.0) if sym == "SGOV"
        else (50.0, 50.0, 0.0))
    return p


def test_multi_symbol_sweep_in_entry_per_symbol(multi_panel):
    infos = multi_panel.compute_sweeps("pos")
    assert [(i["symbol"], i["enabled"], i["qty"]) for i in infos] == [
        ("SGOV", True, 910),      # 91170 / 100 → floor5
        ("BIL", True, 1820),      # 91170 / 50 → floor5
    ]
    assert infos[0]["label"] == "Sweep in 910 SGOV"
    assert infos[1]["label"] == "Sweep in 1,820 BIL"


def test_multi_symbol_sweep_out_only_where_held(multi_panel):
    infos = multi_panel.compute_sweeps("neg")
    sgov, bil = infos
    assert sgov["enabled"] is True and sgov["direction"] == "out"
    assert bil["enabled"] is False
    assert "No BIL shares" in bil["label"]


def test_legacy_single_symbol_config_still_works(panel):
    # The original fixture writes only the legacy "sweep_symbol" key —
    # compute_sweeps must surface it as the one configured symbol.
    infos = panel.compute_sweeps("pos")
    assert len(infos) == 1
    assert infos[0]["symbol"] == "SGOV" and infos[0]["enabled"] is True


def test_sweep_symbols_capped_at_four(tmp_path, monkeypatch):
    monkeypatch.setattr(config_manager, "_active_path", tmp_path / "cfg.json")
    (tmp_path / "cfg.json").write_text(json.dumps(
        {"preferences": {"cash_management": {
            "sweep_symbols": ["A", "B", "C", "D", "E", "A"]}}}))
    from src.core.cash_management import cash_prefs
    cash_prefs.reload()
    assert cash_prefs.sweep_symbols == ["A", "B", "C", "D"]
    assert cash_prefs.sweep_symbol == "A"      # legacy view = first


def test_no_symbols_configured_single_settings_pointer(tmp_path, monkeypatch,
                                                       qtbot):
    monkeypatch.setattr(config_manager, "_active_path", tmp_path / "cfg.json")
    (tmp_path / "cfg.json").write_text(json.dumps(
        {"preferences": {"cash_management": {"sweep_symbols": []}}}))
    from src.core.cash_management import cash_prefs
    cash_prefs.reload()
    am = AccountsTableModel()
    p = AccountListPanel(am)
    infos = p.compute_sweeps("pos")
    assert len(infos) == 1
    assert infos[0]["enabled"] is False
    assert "Settings" in infos[0]["label"]
