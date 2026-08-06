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

    def test_short_query_does_not_match_names(self):
        """Searching "es" must not drag in every fund whose NAME contains
        the letters (iShar**ES**, QQQ Trust S**ERIES** 1)."""
        local = [
            _local_row(symbol="IWM", name="ISHARES RUSSELL 2000 ETF"),
            _local_row(symbol="QQQ", name="INVESCO QQQ TRUST SERIES 1"),
            _local_row(symbol="ES", sec_type="FUT", name="E-mini S&P 500"),
        ]
        locals_shown, _, _ = merge_rows(local, [], set(), "es")
        assert [e["symbol"] for e in locals_shown] == ["ES"]
        # 3+ character queries still search names.
        locals_shown, _, _ = merge_rows(local, [], set(), "russell")
        assert [e["symbol"] for e in locals_shown] == ["IWM"]

    def test_remote_ranked_exact_then_prefix_futures_first(self):
        """The "es" scenario: the ES future must lead — before the ES
        stock (Eversource), before prefix matches (ESRX/ESLT), before
        IB fuzzy matches with no visible relation."""
        remote = [
            _remote_row(symbol="VGSX", conid=1),                 # fuzzy noise
            _remote_row(symbol="ESRX", conid=2),                 # prefix
            _remote_row(symbol="ES", conid=3),                   # exact STK
            {**_remote_row(symbol="ES", conid=4), "sec_type": "FUT"},
            _remote_row(symbol="ESLT", conid=5),                 # prefix
        ]
        _, remote_shown, _ = merge_rows([], remote, set(), "es")
        assert [(r["symbol"], r["sec_type"]) for r in remote_shown] == [
            ("ES", "FUT"), ("ES", "STK"),
            ("ESRX", "STK"), ("ESLT", "STK"),
            ("VGSX", "STK"),
        ]

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

    def test_same_expiry_on_two_venues_consumes_one_slot(self):
        """ES lists each expiration on CME AND IB's QBALGO algo venue —
        without per-date dedup the user saw Sep twice and never Dec."""
        chain = [
            _chain_row(1, "20260918"),                      # Sep @ CME
            {**_chain_row(2, "20260918"), "exchange": "QBALGO"},
            {**_chain_row(3, "20261218"), "exchange": "QBALGO"},
            _chain_row(4, "20261218"),                      # Dec @ CME
        ]
        out = ssp.pick_front_expiries(chain, today=self._TODAY)
        # Two DISTINCT months, primary venue preferred on each.
        assert [r["conid"] for r in out] == [1, 4]
        assert all(r["exchange"] == "CME" for r in out)

    def test_qbalgo_only_expiry_still_shown(self):
        # If an expiry ONLY trades on the algo venue it must still appear
        # rather than being filtered out entirely.
        chain = [_chain_row(1, "20260918"),
                 {**_chain_row(2, "20261218"), "exchange": "QBALGO"}]
        out = ssp.pick_front_expiries(chain, today=self._TODAY)
        assert [r["conid"] for r in out] == [1, 2]

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


class TestApplyFuturePick:
    @pytest.fixture
    def store(self, monkeypatch):
        """Patch the resolver + upsert: apply_future_pick now re-points
        chip/hot-button metadata instead of rewriting a contracts DB."""
        import src.core.instruments as instruments
        state = {"stored": None, "upserts": []}
        monkeypatch.setattr(
            instruments, "get_contract",
            lambda q: dict(state["stored"]) if state["stored"] else None)
        monkeypatch.setattr(
            ssp.config_manager, "upsert_instrument",
            lambda sym, meta, path=None:
                state["upserts"].append((sym, meta)) or True)
        return state

    @staticmethod
    def _answer(monkeypatch, action):
        """Patch the three-way dialog (replace / pin / cancel)."""
        calls = []
        monkeypatch.setattr(
            ssp, "_ask_future_action",
            lambda *a, **k: calls.append(a) or action)
        return calls

    def _entry(self, conid=2, expiry="20261218"):
        return {**_chain_row(conid, expiry), "source": "ibkr",
                "name": "E-mini Nasdaq 100"}

    def test_new_root_saves_without_prompt(self, store, monkeypatch):
        calls = self._answer(monkeypatch, "cancel")
        sym, replaced = ssp.apply_future_pick(None, self._entry())
        assert (sym, replaced) == ("NQ", False)
        assert calls == []  # no confirmation needed
        assert store["upserts"][0][0] == "NQ"
        assert store["upserts"][0][1]["conid"] == "2"

    def test_same_contract_heals_without_prompt(self, store, monkeypatch):
        store["stored"] = {"symbol": "NQ", "conid": "2",
                           "sec_type": "FUT", "expiry": "20261218"}
        calls = self._answer(monkeypatch, None)
        sym, replaced = ssp.apply_future_pick(None, self._entry(conid=2))
        assert (sym, replaced) == ("NQ", False)
        assert calls == []
        assert len(store["upserts"]) == 1   # heal writes metadata through

    def test_different_expiry_yes_replaces_everywhere(self, store,
                                                      monkeypatch):
        store["stored"] = {"symbol": "NQ", "conid": "1",
                           "sec_type": "FUT", "expiry": "20260918"}
        calls = self._answer(monkeypatch, "replace")
        sym, replaced = ssp.apply_future_pick(
            None, self._entry(conid=2, expiry="20261218"))
        assert (sym, replaced) == ("NQ", True)
        assert len(calls) == 1              # user was asked
        # upsert fans the new expiry out to every chip / hot button.
        assert store["upserts"][0][1]["expiry"] == "20261218"

    def test_different_expiry_no_cancels(self, store, monkeypatch):
        store["stored"] = {"symbol": "NQ", "conid": "1",
                           "sec_type": "FUT", "expiry": "20260918"}
        self._answer(monkeypatch, "cancel")
        sym, replaced = ssp.apply_future_pick(
            None, self._entry(conid=2, expiry="20261218"))
        assert (sym, replaced) == ("", False)
        assert store["upserts"] == []

    def test_different_expiry_pin_adds_local_symbol_chip(self, store,
                                                         monkeypatch):
        """Pin keeps the root untouched and returns the picked
        contract's LOCAL symbol as the chip key — an expiry-keyed chip
        frozen to that contract."""
        store["stored"] = {"symbol": "NQ", "conid": "1",
                           "sec_type": "FUT", "expiry": "20260918"}
        self._answer(monkeypatch, "pin")
        sym, replaced = ssp.apply_future_pick(
            None, self._entry(conid=2, expiry="20261218"))
        assert replaced is False
        assert sym == "NQ20261218"        # _chain_row's local_symbol
        assert store["upserts"] == []     # root references untouched


class TestDisplayExchange:
    def test_island_maps_to_nasdaq(self):
        assert display_exchange("ISLAND") == "NASDAQ"
        assert display_exchange("island") == "NASDAQ"

    def test_other_codes_pass_through(self):
        assert display_exchange("TSE") == "TSE"
        assert display_exchange("NYSE") == "NYSE"
        assert display_exchange("") == ""


class TestEnsureContractSaved:
    """ensure_contract_saved no longer persists anything — it resolves a
    popup pick into (symbol, chip metadata) for the host's _add_symbol."""

    def test_local_row_resolves_meta(self):
        sym, meta = ssp.ensure_contract_saved(
            {"symbol": "nq", "sec_type": "FUT", "conid": "11",
             "source": "local"})
        assert sym == "NQ"
        assert meta["conid"] == "11" and meta["sec_type"] == "FUT"

    def test_ibkr_future_returns_meta(self):
        sym, meta = ssp.ensure_contract_saved(
            {"symbol": "es", "sec_type": "FUT", "conid": 11004968,
             "currency": "USD", "source": "ibkr"})
        assert sym == "ES"
        assert meta["conid"] == "11004968"

    def test_ibkr_stk_pick_defaults_smart_and_local(self):
        sym, meta = ssp.ensure_contract_saved({
            "symbol": "googl", "sec_type": "STK", "conid": 208813720,
            "currency": "USD", "primary_exchange": "NASDAQ",
            "name": "Alphabet Inc (Class A)",
            "source": "ibkr",
        })
        assert sym == "GOOGL"
        assert meta["exchange"] == "SMART"
        assert meta["local_symbol"] == "GOOGL"
        assert meta["primary_exchange"] == "NASDAQ"
        assert meta["name"] == "Alphabet Inc (Class A)"

    def test_conidless_row_returns_no_meta(self):
        sym, meta = ssp.ensure_contract_saved(
            {"symbol": "nq", "sec_type": "FUT", "source": "local"})
        assert sym == "NQ"
        assert meta is None or "conid" not in meta

    def test_empty_symbol_returns_empty(self):
        assert ssp.ensure_contract_saved({"source": "ibkr"}) == ("", None)
