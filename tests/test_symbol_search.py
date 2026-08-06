"""Tests for the live IBKR symbol-search feature (non-GUI parts).

Covers ``_matching_symbols_to_dicts`` (ib_worker) and
``ensure_contract_saved`` (symbol_search_popup) — the popup widget itself
is exercised manually against TWS.
"""
from datetime import date
from types import SimpleNamespace

import pytest

from src.core.ib_worker import _matching_symbols_to_dicts
from src.ui.widgets import symbol_search_popup as ssp
from src.ui.widgets.symbol_search_popup import display_exchange, merge_rows


def _desc(symbol="GOOGL", con_id=208813720, sec_type="STK",
          primary="NASDAQ", currency="USD", name="Alphabet Inc (Class A)"):
    return SimpleNamespace(
        contract=SimpleNamespace(
            conId=con_id, symbol=symbol, secType=sec_type,
            primaryExchange=primary, currency=currency, description=name),
        derivativeSecTypes=["OPT"],
    )


class TestMatchingSymbolsToDicts:
    def test_basic_conversion(self):
        out = _matching_symbols_to_dicts([_desc()])
        assert out == [{
            "conid": 208813720,
            "symbol": "GOOGL",
            "sec_type": "STK",
            "primary_exchange": "NASDAQ",
            "currency": "USD",
            "name": "Alphabet Inc (Class A)",
            "description": "Alphabet Inc (Class A)",
            "has_options": True,
            "derivative_sec_types": ["OPT"],
        }]

    def test_skips_missing_contract_and_empty_symbol(self):
        broken = SimpleNamespace(contract=None, derivativeSecTypes=[])
        empty = _desc(symbol="")
        out = _matching_symbols_to_dicts([broken, empty, _desc(symbol="GOOW")])
        assert [r["symbol"] for r in out] == ["GOOW"]

    def test_none_and_empty_input(self):
        assert _matching_symbols_to_dicts(None) == []
        assert _matching_symbols_to_dicts([]) == []

    def test_missing_description_defaults_to_empty(self):
        d = _desc()
        del d.contract.description
        assert _matching_symbols_to_dicts([d])[0]["name"] == ""


def _local_row(symbol="AMZN", sec_type="STK", conid="3691937", name=""):
    return {"symbol": symbol, "sec_type": sec_type, "conid": conid,
            "name": name, "badge": sec_type, "source": "local"}


def _remote_row(symbol="AMZN", sec_type="STK", conid=3691937,
                primary="NASDAQ"):
    return {"conid": conid, "symbol": symbol, "sec_type": sec_type,
            "primary_exchange": primary, "currency": "USD", "name": ""}


class TestMergeRows:
    def test_other_exchange_listings_survive_local_dedup(self):
        """A saved NASDAQ AMZN must not hide AMZN's TSE/MEXI listings."""
        local = [_local_row(conid="3691937")]
        remote = [
            _remote_row(conid=3691937, primary="NASDAQ"),
            _remote_row(conid=80000001, primary="TSE"),
            _remote_row(conid=80000002, primary="MEXI"),
        ]
        locals_shown, remote_shown, already = merge_rows(
            local, remote, exclude=set(), query="amzn")
        assert [e["symbol"] for e in locals_shown] == ["AMZN"]
        assert [r["primary_exchange"] for r in remote_shown] == [
            "TSE", "MEXI"]
        assert already == []

    def test_legacy_local_without_conid_dedups_by_symbol(self):
        local = [_local_row(conid="")]
        remote = [_remote_row(conid=3691937, primary="NASDAQ")]
        _, remote_shown, _ = merge_rows(local, remote, set(), "amzn")
        assert remote_shown == []

    def test_already_added_symbol_reported_not_silently_hidden(self):
        remote = [_remote_row(conid=3691937, primary="NASDAQ"),
                  _remote_row(conid=80000001, primary="TSE")]
        locals_shown, remote_shown, already = merge_rows(
            [], remote, exclude={"AMZN"}, query="amz")
        assert locals_shown == [] and remote_shown == []
        assert already == ["AMZN"]

    def test_no_query_hides_remote_and_already_notes(self):
        local = [_local_row()]
        _, remote_shown, already = merge_rows(
            local, [_remote_row()], exclude={"NQ"}, query="")
        assert already == []
        assert [e["symbol"] for e in local] == ["AMZN"]

    def test_query_filters_locals(self):
        local = [_local_row(symbol="AMZN"), _local_row(symbol="NQ",
                                                       sec_type="FUT")]
        locals_shown, _, _ = merge_rows(local, [], set(), "amz")
        assert [e["symbol"] for e in locals_shown] == ["AMZN"]

    def test_live_fut_root_supersedes_saved_chain_rows(self):
        """A live FUT root always shows (it expands into pickable
        expirations) — the saved per-expiry rows step aside."""
        local = [_local_row(symbol="NQ", sec_type="FUT", conid="111")]
        remote = [{**_remote_row(symbol="NQ", conid=11004958,
                                 primary="CME"), "sec_type": "FUT"}]
        locals_shown, remote_shown, _ = merge_rows(
            local, remote, set(), "nq")
        assert locals_shown == []
        assert [r["symbol"] for r in remote_shown] == ["NQ"]

    def test_fut_root_shown_even_when_chip_already_added(self):
        """Chip-bar futures still expand so the user can switch
        expiration; no 'already added' note for them."""
        remote = [{**_remote_row(symbol="NQ", conid=11004958,
                                 primary="CME"), "sec_type": "FUT"}]
        locals_shown, remote_shown, already = merge_rows(
            [], remote, exclude={"NQ"}, query="nq")
        assert [r["symbol"] for r in remote_shown] == ["NQ"]
        assert already == []

    def test_deduped_remote_backfills_local_name_and_exchange(self):
        """A saved MSFT with no stored name/exchange borrows them from
        the live result it suppressed — so both display identically."""
        local = [{**_local_row(symbol="MSFT", conid="272093", name=""),
                  "detail": "SMART", "primary_exchange": ""}]
        remote = [{**_remote_row(symbol="MSFT", conid=272093,
                                 primary="ISLAND"),
                   "name": "MICROSOFT CORP"}]
        locals_shown, remote_shown, _ = merge_rows(
            local, remote, set(), "msft")
        assert remote_shown == []
        (e,) = locals_shown
        assert e["name"] == "MICROSOFT CORP"
        assert e["primary_exchange"] == "ISLAND"
        # the popup's cached originals must stay untouched
        assert local[0]["name"] == ""

    def test_stored_local_fields_not_overwritten_by_backfill(self):
        local = [{**_local_row(symbol="MSFT", conid="272093",
                               name="Microsoft"),
                  "primary_exchange": "NASDAQ"}]
        remote = [{**_remote_row(symbol="MSFT", conid=272093,
                                 primary="ISLAND"), "name": "MSFT CORP"}]
        (e,), _, _ = merge_rows(local, remote, set(), "msft")
        assert e["name"] == "Microsoft"
        assert e["primary_exchange"] == "NASDAQ"


class TestFilterStockResults:
    def test_keeps_only_usa_and_canada_stocks(self):
        results = [
            _remote_row(symbol="AMZN", conid=1, primary="NASDAQ"),
            {**_remote_row(symbol="AMZN", conid=2, primary="TSE"),
             "currency": "CAD"},
            {**_remote_row(symbol="AMZN", conid=3, primary="MEXI"),
             "currency": "MXN"},
            {**_remote_row(symbol="AMZN", conid=4, primary="IBIS"),
             "currency": "EUR"},
            _remote_row(symbol="AMZN25", conid=5, sec_type="FUT"),
        ]
        kept = ssp.filter_stock_results(results)
        assert [r["conid"] for r in kept] == [1, 2]

    def test_missing_currency_is_dropped(self):
        row = _remote_row()
        row["currency"] = ""
        assert ssp.filter_stock_results([row]) == []


class TestExtractFutureResults:
    def test_index_underlying_with_fut_derivative_becomes_fut_row(self):
        results = [
            {"conid": 11004968, "symbol": "ES", "sec_type": "IND",
             "primary_exchange": "CME", "currency": "USD",
             "name": "E-mini S&P 500",
             "derivative_sec_types": ["FUT", "OPT"]},
            {"conid": 256177, "symbol": "CL", "sec_type": "CMDTY",
             "primary_exchange": "NYMEX", "currency": "USD",
             "name": "Crude Oil", "derivative_sec_types": ["FUT"]},
        ]
        out = ssp.extract_future_results(results)
        assert [(r["symbol"], r["sec_type"]) for r in out] == [
            ("ES", "FUT"), ("CL", "FUT")]

    def test_stock_rows_never_become_fut_rows(self):
        results = [{"conid": 1, "symbol": "AAPL", "sec_type": "STK",
                    "primary_exchange": "NASDAQ", "currency": "USD",
                    "name": "", "derivative_sec_types": ["FUT", "OPT"]}]
        assert ssp.extract_future_results(results) == []

    def test_non_usd_futures_dropped(self):
        results = [{"conid": 1, "symbol": "DAX", "sec_type": "IND",
                    "primary_exchange": "EUREX", "currency": "EUR",
                    "name": "", "derivative_sec_types": ["FUT"]}]
        assert ssp.extract_future_results(results) == []

    def test_underlying_without_fut_derivative_dropped(self):
        results = [{"conid": 1, "symbol": "TICK", "sec_type": "IND",
                    "primary_exchange": "NYSE", "currency": "USD",
                    "name": "", "derivative_sec_types": []}]
        assert ssp.extract_future_results(results) == []


def _chain_row(conid, expiry, symbol="NQ", currency="USD"):
    return {"conid": conid, "symbol": symbol, "sec_type": "FUT",
            "exchange": "CME", "currency": currency, "expiry": expiry,
            "strike": 0.0, "right": "", "local_symbol": f"{symbol}{expiry}",
            "multiplier": "20", "primary_exchange": "CME"}


class TestPickFrontExpiries:
    _TODAY = date(2026, 7, 26)

    def test_nearest_two_unexpired_sorted(self):
        chain = [_chain_row(3, "20261218"), _chain_row(1, "20250620"),
                 _chain_row(2, "20260918"), _chain_row(4, "20270319")]
        out = ssp.pick_front_expiries(chain, today=self._TODAY)
        assert [r["conid"] for r in out] == [2, 3]

    def test_month_only_expiry_selectable_through_its_month(self):
        chain = [_chain_row(1, "202607")]
        out = ssp.pick_front_expiries(chain, today=self._TODAY)
        assert [r["conid"] for r in out] == [1]

    def test_non_usd_bad_expiry_and_non_fut_dropped(self):
        chain = [_chain_row(1, "20261218", currency="EUR"),
                 _chain_row(2, "garbage"),
                 {"conid": 3, "symbol": "NQ", "sec_type": "STK",
                  "currency": "USD", "expiry": "20261218"}]
        assert ssp.pick_front_expiries(chain, today=self._TODAY) == []

    def test_count_respected(self):
        chain = [_chain_row(i, f"2026{m:02d}18") for i, m in
                 enumerate((9, 10, 11, 12), start=1)]
        out = ssp.pick_front_expiries(chain, count=2, today=self._TODAY)
        assert len(out) == 2


class TestFormatExpiry:
    def test_full_date(self):
        assert ssp.format_expiry("20261218") == "Dec 18, 2026"

    def test_contract_month(self):
        assert ssp.format_expiry("202612") == "Dec 2026"

    def test_garbage_passes_through(self):
        assert ssp.format_expiry("soon") == "soon"
        assert ssp.format_expiry("") == ""


class TestSaveFutureChain:
    @pytest.fixture
    def store(self, monkeypatch):
        state = {"contracts": [], "saved": None, "reloaded": False}
        monkeypatch.setattr(ssp.config_manager, "load_contracts",
                            lambda: list(state["contracts"]))

        def _save(contracts):
            state["saved"] = contracts
        monkeypatch.setattr(ssp.config_manager, "save_contracts", _save)

        import src.core.reference_data as rd

        def _reload():
            state["reloaded"] = True
        monkeypatch.setattr(
            rd, "get_instance",
            lambda: SimpleNamespace(reload_contracts=_reload))
        return state

    def test_saves_whole_usd_chain(self, store):
        sym = ssp.save_future_chain(
            [_chain_row(1, "20260918"), _chain_row(2, "20261218")],
            root_name="E-mini Nasdaq 100")
        assert sym == "NQ"
        assert store["reloaded"]
        assert [c["conid"] for c in store["saved"]] == [1, 2]
        assert all(c["name"] == "E-mini Nasdaq 100"
                   for c in store["saved"])

    def test_dedupes_against_existing_conids(self, store):
        store["contracts"] = [_chain_row(1, "20260918")]
        ssp.save_future_chain(
            [_chain_row(1, "20260918"), _chain_row(2, "20261218")])
        assert [c["conid"] for c in store["saved"]] == [1, 2]

    def test_fully_saved_chain_writes_nothing(self, store):
        store["contracts"] = [_chain_row(1, "20260918")]
        assert ssp.save_future_chain([_chain_row(1, "20260918")]) == "NQ"
        assert store["saved"] is None

    def test_non_usd_and_non_fut_rows_ignored(self, store):
        assert ssp.save_future_chain(
            [_chain_row(1, "20260918", currency="EUR"),
             {"conid": 9, "symbol": "NQ", "sec_type": "STK",
              "currency": "USD"}]) == ""
        assert store["saved"] is None


class TestApplyFuturePick:
    @pytest.fixture
    def store(self, monkeypatch):
        state = {"contracts": [], "saved": None, "reloaded": False}
        monkeypatch.setattr(ssp.config_manager, "load_contracts",
                            lambda: [dict(c) for c in state["contracts"]])

        def _save(contracts):
            state["saved"] = contracts
        monkeypatch.setattr(ssp.config_manager, "save_contracts", _save)

        import src.core.reference_data as rd

        def _reload():
            state["reloaded"] = True
        monkeypatch.setattr(
            rd, "get_instance",
            lambda: SimpleNamespace(reload_contracts=_reload))
        return state

    @staticmethod
    def _answer(monkeypatch, button):
        from PySide6.QtWidgets import QMessageBox
        calls = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: calls.append(a) or button)
        return calls

    def _entry(self, conid=2, expiry="20261218"):
        return {**_chain_row(conid, expiry), "source": "ibkr",
                "name": "E-mini Nasdaq 100"}

    def test_new_root_saves_without_prompt(self, store, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        calls = self._answer(monkeypatch, QMessageBox.StandardButton.No)
        sym, replaced = ssp.apply_future_pick(None, self._entry())
        assert (sym, replaced) == ("NQ", False)
        assert calls == []  # no confirmation needed
        assert [c["conid"] for c in store["saved"]] == [2]

    def test_same_contract_is_noop(self, store, monkeypatch):
        store["contracts"] = [_chain_row(2, "20261218")]
        calls = self._answer(monkeypatch, None)
        sym, replaced = ssp.apply_future_pick(None, self._entry(conid=2))
        assert (sym, replaced) == ("NQ", False)
        assert calls == [] and store["saved"] is None

    def test_different_expiry_yes_replaces_everywhere(self, store,
                                                      monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        store["contracts"] = [
            _chain_row(1, "20260918"),
            {"conid": "9", "symbol": "MSFT", "sec_type": "STK"},
        ]
        self._answer(monkeypatch, QMessageBox.StandardButton.Yes)
        sym, replaced = ssp.apply_future_pick(
            None, self._entry(conid=2, expiry="20261218"))
        assert (sym, replaced) == ("NQ", True)
        assert store["reloaded"]
        saved = store["saved"]
        # old NQ expiry gone, new one in, unrelated contracts untouched
        assert [c["conid"] for c in saved
                if c.get("sec_type") == "FUT"] == [2]
        assert any(c.get("symbol") == "MSFT" for c in saved)

    def test_different_expiry_no_cancels(self, store, monkeypatch):
        from PySide6.QtWidgets import QMessageBox
        store["contracts"] = [_chain_row(1, "20260918")]
        self._answer(monkeypatch, QMessageBox.StandardButton.No)
        sym, replaced = ssp.apply_future_pick(
            None, self._entry(conid=2, expiry="20261218"))
        assert (sym, replaced) == ("", False)
        assert store["saved"] is None


class TestDisplayExchange:
    def test_island_maps_to_nasdaq(self):
        assert display_exchange("ISLAND") == "NASDAQ"
        assert display_exchange("island") == "NASDAQ"

    def test_other_codes_pass_through(self):
        assert display_exchange("TSE") == "TSE"
        assert display_exchange("NYSE") == "NYSE"
        assert display_exchange("") == ""


class TestEnsureContractSaved:
    @pytest.fixture
    def store(self, monkeypatch):
        """Patch config_manager + ReferenceData; returns the fake DB state."""
        state = {"contracts": [], "saved": False, "reloaded": False}
        monkeypatch.setattr(ssp.config_manager, "load_contracts",
                            lambda: list(state["contracts"]))

        def _save(contracts):
            state["contracts"] = contracts
            state["saved"] = True
        monkeypatch.setattr(ssp.config_manager, "save_contracts", _save)

        import src.core.reference_data as rd

        def _reload():
            state["reloaded"] = True
        monkeypatch.setattr(
            rd, "get_instance",
            lambda: SimpleNamespace(reload_contracts=_reload))
        return state

    def test_local_source_is_noop(self, store):
        sym, _meta = ssp.ensure_contract_saved(
            {"symbol": "nq", "sec_type": "FUT", "source": "local"})
        assert sym == "NQ"
        assert not store["saved"]

    def test_ibkr_future_is_noop(self, store):
        """FUT picks are chain-saved by the popup — never as a single
        SMART pseudo-contract here."""
        sym, meta = ssp.ensure_contract_saved(
            {"symbol": "es", "sec_type": "FUT", "conid": 11004968,
             "currency": "USD", "source": "ibkr"})
        assert sym == "ES"
        assert not store["saved"]
        assert meta["conid"] == "11004968"   # caller still gets chip meta

    def test_ibkr_pick_saves_contract(self, store):
        sym, meta = ssp.ensure_contract_saved({
            "symbol": "googl", "sec_type": "STK", "conid": 208813720,
            "currency": "USD", "primary_exchange": "NASDAQ",
            "name": "Alphabet Inc (Class A)",
            "source": "ibkr",
        })
        assert sym == "GOOGL"
        assert store["saved"] and store["reloaded"]
        assert meta["conid"] == "208813720"
        assert meta["exchange"] == "SMART"       # chip metadata for the host
        assert meta["local_symbol"] == "GOOGL"
        (c,) = store["contracts"]
        assert c["symbol"] == "GOOGL"
        assert c["sec_type"] == "STK"
        assert c["exchange"] == "SMART"
        assert c["currency"] == "USD"
        assert c["local_symbol"] == "GOOGL"
        assert c["primary_exchange"] == "NASDAQ"
        assert c["name"] == "Alphabet Inc (Class A)"

    def test_duplicate_conid_not_resaved(self, store):
        store["contracts"] = [
            {"conid": "208813720", "symbol": "GOOGL", "sec_type": "STK"}]
        sym, _meta = ssp.ensure_contract_saved({
            "symbol": "GOOGL", "sec_type": "STK", "conid": 208813720,
            "source": "ibkr",
        })
        assert sym == "GOOGL"
        assert not store["saved"]

    def test_duplicate_symbol_sectype_not_resaved(self, store):
        store["contracts"] = [
            {"conid": "", "symbol": "GOOGL", "sec_type": "STK"}]
        ssp.ensure_contract_saved({
            "symbol": "googl", "sec_type": "STK", "conid": 1,
            "source": "ibkr",
        })
        assert not store["saved"]

    def test_empty_symbol_returns_empty(self, store):
        assert ssp.ensure_contract_saved({"source": "ibkr"}) == ("", None)
        assert not store["saved"]
