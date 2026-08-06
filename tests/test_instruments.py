"""Instrument resolver — chip/hot-button entries as the contract source.

Covers entry normalisation, resolution precedence (exact local_symbol >
order-entry > book-trader > hot buttons, conid-carrying entries beating
bare ones), the derived getters, md-param building, the merge-aware
legacy *_symbols shims, upsert_instrument fan-out, and cache
invalidation on config writes.
"""
from __future__ import annotations

import json

import pytest

import src.core.config_manager as cm
import src.core.instruments as instruments


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point the active config at a temp file; resolver cache reset."""
    p = tmp_path / "app_config-test.json"
    monkeypatch.setattr(cm, "_active_path", p)
    instruments.invalidate_cache()
    yield p
    instruments.invalidate_cache()


def _write(p, data):
    p.write_text(json.dumps(data), encoding="utf-8")
    instruments.invalidate_cache()


_MNQ = {"symbol": "MNQ", "conid": "793356225", "sec_type": "FUT",
        "exchange": "CME", "currency": "USD", "expiry": "20260320",
        "local_symbol": "MNQH6", "multiplier": "2", "min_tick": 0.25}
_AAPL = {"symbol": "AAPL", "conid": "265598", "sec_type": "STK",
         "exchange": "SMART", "currency": "USD", "local_symbol": "AAPL",
         "primary_exchange": "ISLAND"}


class TestNormalisers:
    def test_meta_whitelists_and_coerces(self):
        meta = instruments.normalise_contract_meta(
            {"conid": 42, "sec_type": "STK", "junk": "x", "min_tick": "0.25"})
        assert meta == {"conid": "42", "sec_type": "STK", "min_tick": 0.25}

    def test_meta_rejects_non_dict_and_empty(self):
        assert instruments.normalise_contract_meta("MNQ") is None
        assert instruments.normalise_contract_meta({}) is None
        assert instruments.normalise_contract_meta({"conid": ""}) is None

    def test_symbol_entry_from_string_and_dict(self):
        assert instruments.normalise_symbol_entry(" MNQ ") == {"symbol": "MNQ"}
        e = instruments.normalise_symbol_entry(_MNQ)
        assert e["symbol"] == "MNQ" and e["conid"] == "793356225"
        assert instruments.normalise_symbol_entry("") is None
        assert instruments.normalise_symbol_entry(42) is None


class TestResolution:
    def test_exact_local_symbol_wins(self, cfg):
        _write(cfg, {"order_entry_symbols": [_MNQ]})
        assert instruments.get_contract("MNQH6")["conid"] == "793356225"

    def test_conid_entry_beats_bare_entry(self, cfg):
        # OE has a bare string; BT has the full dict — resolver must
        # return the one that can actually subscribe/order.
        _write(cfg, {"order_entry_symbols": ["MNQ"],
                     "book_trader_symbols": [_MNQ]})
        assert instruments.get_contract("MNQ")["conid"] == "793356225"

    def test_hot_button_contract_resolves(self, cfg):
        _write(cfg, {"hot_buttons": [
            {"side": "BUY", "symbol": "AAPL", "contract": _AAPL}]})
        assert instruments.get_contract("AAPL")["conid"] == "265598"

    def test_unknown_symbol_none(self, cfg):
        _write(cfg, {"order_entry_symbols": [_MNQ]})
        assert instruments.get_contract("SPCX") is None
        assert instruments.get_contract("") is None

    def test_all_contracts_dedupes_and_sorts(self, cfg):
        _write(cfg, {"order_entry_symbols": ["MNQ", _AAPL],
                     "book_trader_symbols": [_MNQ]})
        rows = instruments.all_contracts()
        # STK before FUT; the bare OE "MNQ" merged with BT's full entry.
        assert [r["symbol"] for r in rows] == ["AAPL", "MNQ"]
        assert rows[1]["conid"] == "793356225"


class TestGetters:
    def test_sec_type_with_prefix_fallback(self, cfg):
        _write(cfg, {"order_entry_symbols": [_MNQ, _AAPL]})
        assert instruments.get_sec_type("MNQ") == "FUT"
        assert instruments.get_sec_type("MNQM6") == "FUT"   # prefix match
        assert instruments.get_sec_type("SPCX") == "?"

    def test_multiplier_primary_exchange_min_tick(self, cfg):
        _write(cfg, {"order_entry_symbols": [_MNQ, _AAPL]})
        assert instruments.get_multiplier("MNQ") == 2.0
        assert instruments.get_multiplier("AAPL") == 1.0
        assert instruments.get_primary_exchange("AAPL") == "ISLAND"
        assert instruments.get_min_tick("MNQ") == 0.25
        assert instruments.get_min_tick("AAPL") is None

    def test_find_by_conid(self, cfg):
        _write(cfg, {"order_entry_symbols": [_MNQ]})
        assert instruments.find_by_conid(793356225)["symbol"] == "MNQ"
        assert instruments.find_by_conid("nope") is None

    def test_build_md_params(self, cfg):
        _write(cfg, {"order_entry_symbols": [_MNQ, "BARE"]})
        p = instruments.build_md_params("MNQ")
        assert p == {"conid": 793356225, "symbol": "MNQ", "sec_type": "FUT",
                     "exchange": "CME", "currency": "USD",
                     "local_symbol": "MNQH6", "expiry": "20260320",
                     "multiplier": "2"}
        assert instruments.build_md_params("BARE") is None

    def test_meta_from_stk_popup_result(self):
        meta = instruments.contract_meta_from_result(
            {"symbol": "NVDA", "sec_type": "STK", "conid": "4815747",
             "currency": "USD", "primary_exchange": "", "name": "NVIDIA"})
        assert meta["exchange"] == "SMART"
        assert meta["local_symbol"] == "NVDA"
        assert meta["conid"] == "4815747"


class TestLegacyShims:
    def test_symbols_roundtrip_preserves_metadata(self, cfg):
        _write(cfg, {"order_entry_symbols": [_MNQ, _AAPL]})
        # Panel-style write: reorder + drop AAPL + append new bare TSLA.
        cm.save_order_entry_symbols(["TSLA", "MNQ"])
        entries = cm.load_order_entry_entries()
        assert [e["symbol"] for e in entries] == ["TSLA", "MNQ"]
        assert entries[1]["conid"] == "793356225"   # metadata survived
        assert "conid" not in entries[0]            # new symbol is bare
        assert cm.load_order_entry_symbols() == ["TSLA", "MNQ"]

    def test_book_trader_first_save_keeps_seeded_metadata(self, cfg):
        # No book_trader_symbols key yet — the BT list seeds from OE.
        _write(cfg, {"order_entry_symbols": [_MNQ]})
        assert cm.load_book_trader_symbols() == ["MNQ"]
        cm.save_book_trader_symbols(["MNQ"])        # first BT save
        entries = cm.load_book_trader_entries()
        assert entries[0]["conid"] == "793356225"   # seed metadata kept

    def test_hot_button_contract_survives_renormalisation(self, cfg):
        btn = {"side": "BUY", "symbol": "AAPL", "contract": _AAPL}
        cm.save_hot_buttons([btn])
        loaded = cm.load_hot_buttons()
        assert loaded[0]["contract"]["conid"] == "265598"


class TestUpsert:
    def test_upsert_fans_out_to_all_references(self, cfg):
        _write(cfg, {
            "order_entry_symbols": ["MNQ"],
            "book_trader_symbols": [dict(_MNQ)],
            "hot_buttons": [{"side": "BUY", "symbol": "MNQ"}],
        })
        rolled = dict(_MNQ, expiry="20260619", local_symbol="MNQM6",
                      conid="800000001")
        assert cm.upsert_instrument("MNQ", rolled) is True
        oe = cm.load_order_entry_entries()[0]
        bt = cm.load_book_trader_entries()[0]
        hb = cm.load_hot_buttons()[0]["contract"]
        assert oe["conid"] == bt["conid"] == hb["conid"] == "800000001"
        assert bt["local_symbol"] == "MNQM6"

    def test_upsert_no_match_is_noop(self, cfg):
        _write(cfg, {"order_entry_symbols": ["MNQ"]})
        assert cm.upsert_instrument("SPCX", _AAPL) is False


def test_cache_invalidates_on_config_write(cfg):
    _write(cfg, {"order_entry_symbols": ["MNQ"]})
    assert instruments.get_contract("MNQ") == {"symbol": "MNQ"}
    # A write through config_manager must be visible immediately.
    cm.save_order_entry_entries([dict(_MNQ)])
    assert instruments.get_contract("MNQ")["conid"] == "793356225"


class TestHotButtonTrailingLeg:
    """The TS bracket leg persists like TP/SL and reaches the order
    payload; legacy entries without it gain a default-off leg."""

    def test_tr_leg_round_trips(self, cfg):
        cm.save_hot_buttons([{
            "side": "BUY", "symbol": "MNQ",
            "tr": {"on": True, "v": 25.0, "u": "abs"},
        }])
        (btn,) = cm.load_hot_buttons()
        assert btn["tr"] == {"on": True, "v": 25.0, "u": "abs"}

    def test_legacy_entry_gains_default_off_leg(self, cfg):
        cm.save_hot_buttons([{"side": "SELL", "symbol": "ES"}])
        (btn,) = cm.load_hot_buttons()
        assert btn["tr"] == {"on": False, "v": 80.0, "u": "abs"}

    def test_bracket_payload_maps_tr_leg(self):
        from src.ui.widgets.order_entry_panel import OrderEntryPanel
        f = OrderEntryPanel._bracket_payload
        assert f({"on": True, "v": 25, "u": "abs"}) == {
            "value": 25.0, "unit": "price", "label": "HotButton"}
        assert f({"on": False, "v": 25, "u": "abs"}) is None
        assert f(None) is None

    def test_auto_label_third_value_only_when_armed(self):
        from src.ui.widgets.order_entry_panel import _auto_hot_button_label
        base = dict(side="BUY", qty="2", sym="NQ", ord_type="MKT",
                    anchor="MID", offset=0)
        on = {"on": True, "v": 80, "u": "abs"}
        sl = {"on": True, "v": 50, "u": "abs"}
        off = {"on": False, "v": 80, "u": "abs"}
        # Backwards compatible: tr off → identical two-value label.
        assert _auto_hot_button_label(**base, tp=on, sl=sl, tr=off) == \
            _auto_hot_button_label(**base, tp=on, sl=sl)
        # Armed tr appends the third value.
        assert _auto_hot_button_label(
            **base, tp=on, sl=sl,
            tr={"on": True, "v": 25, "u": "abs"}).endswith("80 - 50 - 25")
        # tr-only still renders the full triple so the armed side shows.
        assert _auto_hot_button_label(
            **base, tp=off, sl=off,
            tr={"on": True, "v": 25, "u": "abs"}).endswith("0 - 0 - 25")


class TestPinnedFutures:
    """Expiry-keyed futures chips (symbol == local_symbol, e.g. NQZ6)
    are frozen to one contract: root fan-out misses them by key and
    they resolve/subscribe through their own metadata."""

    _PINNED = {"symbol": "NQZ6", "conid": "555", "sec_type": "FUT",
               "exchange": "CME", "currency": "USD", "expiry": "20261218",
               "local_symbol": "NQZ6", "multiplier": "20"}
    _ROOT = {"symbol": "NQ", "conid": "444", "sec_type": "FUT",
             "exchange": "CME", "currency": "USD", "expiry": "20260918",
             "local_symbol": "NQU6", "multiplier": "20"}

    def test_is_pinned_fut(self):
        assert instruments.is_pinned_fut(self._PINNED) is True
        assert instruments.is_pinned_fut(self._ROOT) is False
        assert instruments.is_pinned_fut({"symbol": "AAPL",
                                          "sec_type": "STK",
                                          "local_symbol": "AAPL"}) is False

    def test_pinned_chip_resolves_and_subscribes(self, cfg):
        _write(cfg, {"book_trader_symbols": [self._ROOT, self._PINNED]})
        assert instruments.get_contract("NQZ6")["conid"] == "555"
        assert instruments.get_contract("NQ")["conid"] == "444"
        assert instruments.build_md_params("NQZ6")["conid"] == 555

    def test_root_upsert_does_not_touch_pinned(self, cfg):
        _write(cfg, {"book_trader_symbols": [self._ROOT, self._PINNED]})
        rolled = dict(self._ROOT, conid="999", expiry="20261218",
                      local_symbol="NQZ6")
        cm.upsert_instrument("NQ", rolled)
        assert instruments.get_contract("NQZ6")["conid"] == "555"  # frozen
        assert instruments.get_contract("NQ")["conid"] == "999"    # rolled

    def test_is_expired_fut(self, cfg):
        expired = dict(self._PINNED, expiry="20200619")
        _write(cfg, {"book_trader_symbols": [expired, self._ROOT]})
        assert instruments.is_expired_fut("NQZ6") is True
        assert instruments.is_expired_fut("NQ") is False    # future expiry
        assert instruments.is_expired_fut("ZZZ") is False   # unknown
