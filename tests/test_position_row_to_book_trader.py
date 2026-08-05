"""Position-row click → Book Trader chip select/add + qty prefill.

Covers the pieces individually (BookTraderView itself is worker-heavy):
chip display labels for OCC option symbols, OPT qty presets, the pure
chip-key derivation, the OPT contract-save helper, and the panel's
``position_row_clicked`` signal filtering (header rows, Flatten column,
TOTAL footer must not emit).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.ui.widgets.book_trader_view as btv
from src.ui.widgets.account_position_model import (
    COL_FLATTEN,
    COL_SYMBOL,
    ROW_TYPE_HEADER,
    ROW_TYPE_POSITION,
    RowTypeRole,
)
from src.ui.widgets.account_position_panel import AccountPositionPanel
from src.ui.widgets.order_panel_parts import SymbolChipBar

_OCC = "QQQ   260807P00670000"


def _pos(**over) -> dict:
    base = {
        "account": "ACC1", "con_id": 111, "symbol": "QQQ",
        "local_symbol": _OCC, "sec_type": "OPT", "exchange": "SMART",
        "currency": "USD", "multiplier": "100", "position": -3.0,
        "avg_cost": 2.5, "market_price": 2.1, "market_value": -630.0,
        "unrealized_pnl": 120.0, "realized_pnl": 0.0,
    }
    base.update(over)
    return base


# ── Chip display labels ───────────────────────────────────────────────────────

class TestChipLabels:
    def _bar(self, qtbot) -> SymbolChipBar:
        bar = SymbolChipBar(columns=5, max_rows_per_group=2)
        qtbot.addWidget(bar)
        bar.set_symbols([("AAPL", "STK"), (_OCC.upper(), "OPT"),
                         ("MNQ", "FUT")])
        return bar

    def test_occ_chip_gets_compact_small_label(self, qtbot):
        bar = self._bar(qtbot)
        chip = bar._buttons[_OCC.upper()]
        # Two-line label: root on top, expiry/strike/right underneath.
        assert chip.text() == "QQQ\nAug7 670 P"
        assert chip.property("chip_small") is True
        assert "font-size:9px" in chip.styleSheet()

    def test_stk_fut_chips_unchanged(self, qtbot):
        bar = self._bar(qtbot)
        for sym in ("AAPL", "MNQ"):
            chip = bar._buttons[sym]
            assert chip.text() == sym
            assert not chip.property("chip_small")
            assert "font-size:12px" in chip.styleSheet()

    def test_buttons_keyed_by_raw_symbol(self, qtbot):
        bar = self._bar(qtbot)
        assert set(bar._buttons) == {"AAPL", _OCC.upper(), "MNQ"}

    def test_small_font_survives_set_active(self, qtbot):
        bar = self._bar(qtbot)
        bar.set_active(_OCC.upper())
        chip = bar._buttons[_OCC.upper()]
        assert "font-size:9px" in chip.styleSheet()
        assert "#539BF5" in chip.styleSheet()  # selected style applied


# ── OPT qty presets ───────────────────────────────────────────────────────────

def test_opt_presets():
    presets, default, popup = btv._presets_for_sec("OPT")
    assert presets == [1, 2, 3, 5]
    assert default == 1
    assert popup == list(range(1, 101))


# ── Chip-key derivation ───────────────────────────────────────────────────────

class TestChipKey:
    def test_opt_uses_raw_occ_local_symbol(self):
        key, sec = btv._chip_key_for_position(_pos())
        assert key == _OCC.upper()
        assert sec == "OPT"

    def test_stk_and_fut_use_root(self):
        key, sec = btv._chip_key_for_position(
            _pos(sec_type="STK", symbol="aapl", local_symbol="AAPL"))
        assert (key, sec) == ("AAPL", "STK")
        key, sec = btv._chip_key_for_position(
            _pos(sec_type="FUT", symbol="MNQ", local_symbol="MNQM6"))
        assert (key, sec) == ("MNQ", "FUT")

    def test_empty_symbol_guard(self):
        key, _sec = btv._chip_key_for_position(
            _pos(sec_type="OPT", local_symbol=""))
        assert key == ""
        key, _sec = btv._chip_key_for_position(
            _pos(sec_type="STK", symbol=None))
        assert key == ""


# ── Add-symbol insert position ────────────────────────────────────────────────

class TestAddSymbolInsertPosition:
    """OPT chips always occupy the last position of the chip bar: options
    append to the tail, and later stock adds slot in BEFORE the options."""

    def _host(self, monkeypatch, symbols: list[str]) -> SimpleNamespace:
        sec_types = {"AAPL": "STK", "MSFT": "STK", "MNQ": "FUT",
                     "MES": "FUT", _OCC.upper(): "OPT"}
        monkeypatch.setattr(
            btv, "_sec_type_for", lambda s: sec_types.get(s, "STK"))
        monkeypatch.setattr(
            btv.config_manager, "save_book_trader_symbols",
            lambda syms, path=None: None)
        host = SimpleNamespace(
            _symbols=list(symbols),
            _chip_bar=SimpleNamespace(set_symbols=lambda entries: None),
            _on_symbol_selected=lambda sym, sec: None,
        )
        return host

    def test_opt_appends_last(self, monkeypatch):
        host = self._host(monkeypatch, ["AAPL", "MSFT", "MNQ"])
        btv.BookTraderView._add_symbol(host, _OCC)
        assert host._symbols == ["AAPL", "MSFT", "MNQ", _OCC.upper()]

    def test_unsaved_occ_shape_still_appends_last(self, monkeypatch):
        # Contract save failed (no conid) → _sec_type_for returns "?";
        # the OCC shape alone must still route the chip to the tail.
        host = self._host(monkeypatch, ["AAPL", "MNQ"])
        monkeypatch.setattr(btv, "_sec_type_for", lambda s: "?")
        btv.BookTraderView._add_symbol(host, _OCC)
        assert host._symbols[-1] == _OCC.upper()

    def test_stk_inserts_before_existing_opt(self, monkeypatch):
        host = self._host(monkeypatch, ["AAPL", _OCC.upper(), "MNQ"])
        btv.BookTraderView._add_symbol(host, "TSLA")
        assert host._symbols == ["AAPL", "TSLA", _OCC.upper(), "MNQ"]

    def test_stk_appends_when_no_opt_present(self, monkeypatch):
        host = self._host(monkeypatch, ["AAPL", "MNQ"])
        btv.BookTraderView._add_symbol(host, "TSLA")
        assert host._symbols == ["AAPL", "MNQ", "TSLA"]

    def test_fut_always_appends(self, monkeypatch):
        # FUT renders in its own group, so its list position relative to
        # the option chips is irrelevant — plain append.
        host = self._host(monkeypatch, ["AAPL", _OCC.upper()])
        btv.BookTraderView._add_symbol(host, "MES")
        assert host._symbols == ["AAPL", _OCC.upper(), "MES"]


# ── OPT contract save helper ──────────────────────────────────────────────────

@pytest.fixture
def contracts_env(monkeypatch):
    state = {"contracts": [], "saved": None, "reloaded": 0}
    monkeypatch.setattr(btv.config_manager, "load_contracts",
                        lambda path=None: list(state["contracts"]))

    def _save(contracts, path=None):
        state["saved"] = contracts
        state["contracts"] = list(contracts)
    monkeypatch.setattr(btv.config_manager, "save_contracts", _save)

    import src.core.reference_data as rd

    def _reload():
        state["reloaded"] += 1
    monkeypatch.setattr(
        rd, "get_instance",
        lambda: SimpleNamespace(reload_contracts=_reload))
    # Unresolvable until a row is saved.
    monkeypatch.setattr(
        btv, "_sec_type_for",
        lambda sym: "OPT" if any(
            c.get("symbol") == sym for c in state["contracts"]) else "?")
    return state


class TestEnsurePositionContractSaved:
    def test_saves_row_with_occ_symbol_key(self, contracts_env):
        btv._ensure_position_contract_saved(_pos())
        row = contracts_env["saved"][0]
        assert row["symbol"] == _OCC.upper()
        assert row["local_symbol"] == _OCC
        assert row["sec_type"] == "OPT"
        assert row["conid"] == "111"
        assert row["multiplier"] == "100"
        assert row["exchange"] == "SMART"
        assert contracts_env["reloaded"] == 1

    def test_second_call_is_noop(self, contracts_env):
        btv._ensure_position_contract_saved(_pos())
        btv._ensure_position_contract_saved(_pos())
        assert contracts_env["reloaded"] == 1

    def test_dedupes_on_conid(self, contracts_env):
        contracts_env["contracts"] = [
            {"conid": "111", "symbol": "SOMETHING", "sec_type": "OPT"}]
        btv._ensure_position_contract_saved(_pos())
        assert contracts_env["saved"] is None

    def test_ignores_non_opt(self, contracts_env):
        btv._ensure_position_contract_saved(_pos(sec_type="STK"))
        assert contracts_env["saved"] is None

    def test_missing_conid_does_not_save(self, contracts_env):
        """A conid-less row could never subscribe market data, and saving
        it would permanently block the retry that has the conid."""
        btv._ensure_position_contract_saved(_pos(con_id=None))
        assert contracts_env["saved"] is None
        # A later call WITH the conid must still be able to save.
        btv._ensure_position_contract_saved(_pos())
        assert contracts_env["saved"][0]["conid"] == "111"

    def test_heals_row_saved_without_conid(self, contracts_env):
        """Rows saved by older builds with conid='' get their conid
        filled in instead of blocking on 'already resolvable'."""
        contracts_env["contracts"] = [
            {"conid": "", "symbol": _OCC.upper(), "sec_type": "OPT",
             "local_symbol": _OCC, "multiplier": "100"}]
        btv._ensure_position_contract_saved(_pos())
        assert len(contracts_env["saved"]) == 1
        assert contracts_env["saved"][0]["conid"] == "111"
        assert contracts_env["reloaded"] == 1


# ── Panel signal filtering ────────────────────────────────────────────────────

@pytest.fixture
def panel(qtbot) -> AccountPositionPanel:
    p = AccountPositionPanel()
    qtbot.addWidget(p)
    p.update_positions([
        _pos(),
        _pos(account="ACC1", con_id=222, symbol="AAPL",
             local_symbol="AAPL", sec_type="STK", position=100.0),
    ])
    return p


def _first_index_of_type(model, row_type, column):
    for row in range(model.rowCount()):
        idx = model.index(row, column)
        if idx.data(RowTypeRole) == row_type:
            return idx
    raise AssertionError(f"no {row_type} row found")


class TestPanelSignal:
    def test_position_row_click_emits_payload(self, panel, qtbot):
        idx = _first_index_of_type(panel._model, ROW_TYPE_POSITION,
                                   COL_SYMBOL)
        with qtbot.waitSignal(panel.position_row_clicked,
                              timeout=1000) as blocker:
            panel._on_table_row_clicked(idx)
        payload = blocker.args[0]
        assert payload["account"] == "ACC1"
        assert payload["sec_type"] in ("OPT", "STK")
        assert "position" in payload and "local_symbol" in payload

    def test_header_row_does_not_emit(self, panel, qtbot):
        idx = _first_index_of_type(panel._model, ROW_TYPE_HEADER,
                                   COL_SYMBOL)
        with qtbot.assertNotEmitted(panel.position_row_clicked):
            panel._on_table_row_clicked(idx)

    def test_flatten_column_does_not_emit(self, panel, qtbot):
        idx = _first_index_of_type(panel._model, ROW_TYPE_POSITION,
                                   COL_FLATTEN)
        with qtbot.assertNotEmitted(panel.position_row_clicked):
            panel._on_table_row_clicked(idx)

    def test_summary_row_emits_with_blank_account(self, panel, qtbot):
        model = panel._summary_model
        target = None
        for row in range(model.rowCount()):
            if model.row_data(row) is not None:
                target = row
                break
        assert target is not None
        with qtbot.waitSignal(panel.position_row_clicked,
                              timeout=1000) as blocker:
            panel._on_summary_row_clicked(model.index(target, 0))
        payload = blocker.args[0]
        assert payload["account"] == ""
        assert "accounts" not in payload

    def test_summary_total_row_does_not_emit(self, panel, qtbot):
        model = panel._summary_model
        total_row = None
        for row in range(model.rowCount()):
            if model.row_data(row) is None:
                total_row = row
                break
        assert total_row is not None, "expected a TOTAL footer row"
        with qtbot.assertNotEmitted(panel.position_row_clicked):
            panel._on_summary_row_clicked(model.index(total_row, 0))


# ── Ladder warm-start from the price cache ────────────────────────────────────

class TestLadderWarmStart:
    """Re-selecting a symbol replays its last-known tick.  The worker
    skips already-subscribed symbols (no fresh snapshot on re-select),
    so without the cache a thinly-traded contract — e.g. an option —
    left the ladder blank until its next natural tick."""

    @pytest.fixture
    def view(self, qtbot, monkeypatch):
        monkeypatch.setattr(btv.config_manager,
                            "save_book_trader_active_symbol",
                            lambda s, path=None: None)
        monkeypatch.setattr(btv.config_manager, "save_book_trader_symbols",
                            lambda s, path=None: None)
        v = btv.BookTraderView(active_threads={})
        qtbot.addWidget(v)
        v._symbols = ["AAPL", "MNQ"]
        v._active_symbol = "AAPL"
        return v

    def test_inactive_ticks_cached_and_merged(self, view):
        view.feed_price("MNQ", 23450.25, 23450.0, 23450.5, 23300.0)
        # A bid-only tick must not erase the known last/ask.
        view.feed_price("MNQ", 0.0, 23451.0, 0.0, 0.0)
        assert view._bg_price_cache["MNQ"] == (
            23450.25, 23451.0, 23450.5, 23300.0)
        assert view._last_price == 0.0  # inactive symbol never rendered

    def test_switch_replays_cached_tick(self, view):
        view.feed_price("MNQ", 23450.25, 23450.0, 23450.5, 23300.0)
        view._on_symbol_selected("MNQ", "FUT")
        assert view._active_symbol == "MNQ"
        assert view._last_price == 23450.25
        assert view._last_bid == 23450.0
        assert view._last_ask == 23450.5

    def test_switch_without_cache_stays_blank(self, view):
        view._on_symbol_selected("MNQ", "FUT")
        assert view._last_price == 0.0


# ── Cancel-all matching for lean option contracts ─────────────────────────────

class TestTradeMatchesSymbol:
    """Option orders are placed on a conId-only contract (error 478
    workaround) — the Trade carries no symbol, so cancel-all must fall
    back to the orderRef → display-symbol cache."""

    @pytest.fixture
    def worker(self):
        from src.core.ib_worker import IBWorkerThread
        return IBWorkerThread("test")

    @staticmethod
    def _trade(symbol="", local="", ref=""):
        contract = SimpleNamespace(symbol=symbol, localSymbol=local)
        order = SimpleNamespace(orderRef=ref)
        return SimpleNamespace(contract=contract, order=order)

    def test_full_contract_matches_root_and_local(self, worker):
        t = self._trade(symbol="MNQ", local="MNQM6")
        assert worker._trade_matches_symbol(t, "MNQ")
        assert worker._trade_matches_symbol(t, "MNQM6")
        assert not worker._trade_matches_symbol(t, "AAPL")

    def test_lean_option_contract_falls_back_to_ref_cache(self, worker):
        ref = "nano_0803_120000_abcd"
        worker._order_symbols[ref] = _OCC
        t = self._trade(ref=ref)  # no symbol/localSymbol on the contract
        assert worker._trade_matches_symbol(t, _OCC.upper())
        assert not worker._trade_matches_symbol(t, "QQQ")

    def test_lean_contract_without_cache_does_not_match(self, worker):
        t = self._trade(ref="nano_0803_120000_zzzz")
        assert not worker._trade_matches_symbol(t, _OCC.upper())


# ── Forced local cancel for held (outside-RTH) orders ─────────────────────────

class TestForceCancelIfStuck:
    """A held (PreSubmitted) order's first cancel leaves it PendingCancel
    — TWS defers the real cancellation to the exchange open.  The worker
    must auto-issue the second cancelOrder (local cancel); an
    exchange-working order must NEVER be force-cancelled."""

    @staticmethod
    def _setup(status_now="PendingCancel", in_open=True):
        from src.core.ib_worker import IBWorkerThread
        worker = IBWorkerThread("test")
        order = SimpleNamespace(orderId=13411, orderRef="nano_x")
        trade = SimpleNamespace(
            order=order,
            orderStatus=SimpleNamespace(status=status_now),
            contract=SimpleNamespace(symbol="", localSymbol=""))
        cancels = []
        worker._ib = SimpleNamespace(
            isConnected=lambda: True,
            openTrades=lambda: [trade] if in_open else [],
            cancelOrder=cancels.append)
        return worker, trade, cancels

    def _run(self, worker, trade, pre_status):
        import asyncio
        asyncio.run(worker._force_cancel_if_stuck(
            "key", trade, pre_status, delay=0.0))

    def test_held_order_stuck_pendingcancel_reissues(self):
        worker, trade, cancels = self._setup()
        self._run(worker, trade, "PreSubmitted")
        assert cancels == [trade.order]

    def test_exchange_working_order_never_force_cancelled(self):
        worker, trade, cancels = self._setup()
        self._run(worker, trade, "Submitted")
        assert cancels == []

    def test_completed_cancel_not_reissued(self):
        worker, trade, cancels = self._setup(status_now="Cancelled")
        self._run(worker, trade, "PreSubmitted")
        assert cancels == []

    def test_order_gone_from_open_trades_not_reissued(self):
        worker, trade, cancels = self._setup(in_open=False)
        self._run(worker, trade, "PreSubmitted")
        assert cancels == []


# ── set_qty: non-preset values land in the custom Qty input ───────────────────

class TestSetQtyCustomInput:
    @pytest.fixture
    def row(self, qtbot):
        from src.ui.widgets.order_panel_parts import QtyChipRow
        r = QtyChipRow()
        qtbot.addWidget(r)
        r.set_presets([10, 25, 50, 100, 500], 100, list(range(1, 101)))
        return r

    def test_preset_qty_lights_chip_and_clears_custom(self, row):
        row.set_qty(37)          # first park a custom value
        row.set_qty(100)         # then a preset
        assert row.current_qty() == 100
        assert row._custom.text() == ""
        assert row._sel_by_unit[row.current_unit()] == 100
        assert row._current_chip() == 100

    def test_non_preset_qty_shows_in_custom_input(self, row):
        row.set_qty(37)
        assert row.current_qty() == 37
        assert row._custom.text() == "37"
        assert row._current_chip() is None  # no chip highlighted

    def test_set_qty_emits_no_signal(self, row, qtbot):
        with qtbot.assertNotEmitted(row.qty_changed):
            row.set_qty(37)
            row.set_qty(100)
