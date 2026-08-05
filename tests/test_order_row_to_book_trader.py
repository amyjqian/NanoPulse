"""Order-blotter Symbol click → Book Trader chip select/add + qty prefill.

Covers the order→position payload translation (blotter symbols are
display symbols: bare roots, futures localSymbols, or raw OCC strings),
``focus_order``'s delegation to the shared ``focus_position`` flow, and
the OrderStatusPanel's ``symbol_clicked`` signal (Symbol cell only —
other columns and blank-symbol rows must not emit).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.ui.widgets.book_trader_view as btv
from src.ui.widgets.order_status_model import COL_SYMBOL, COL_QTY
from src.ui.widgets.order_status_panel import OrderStatusPanel

_OCC = "QQQ   260807P00670000"


def _order(**over) -> dict:
    base = {
        "order_id": "ACC1_nano_0804_100000_abcd",
        "order_ref": "nano_0804_100000_abcd",
        "symbol": "AAPL", "con_id": 0, "sec_type": "",
        "account": "ACC1", "action": "BUY", "order_type": "LMT",
        "quantity": "5", "limit_price": "100", "stop_price": "–",
        "tif": "DAY", "fill_price": "–", "status": "Submitted",
        "time": "10:00:00", "time_sort": "2026-08-04T10:00:00",
        "error_message": "",
    }
    base.update(over)
    return base


# ── Order → position-payload translation ──────────────────────────────────────

def _patch_reference_data(monkeypatch) -> dict:
    """Point ReferenceData at a canned contracts cache: one FUT root
    (two expiries) and one STK."""
    by_symbol = {
        "MNQ": [
            {"symbol": "MNQ", "local_symbol": "MNQM6", "sec_type": "FUT",
             "conid": "1001", "exchange": "CME", "currency": "USD",
             "multiplier": "2"},
            {"symbol": "MNQ", "local_symbol": "MNQU6", "sec_type": "FUT",
             "conid": "1002", "exchange": "CME", "currency": "USD",
             "multiplier": "2"},
        ],
        "AAPL": [
            {"symbol": "AAPL", "local_symbol": "AAPL", "sec_type": "STK",
             "conid": "2001", "exchange": "SMART", "currency": "USD",
             "multiplier": ""},
        ],
    }
    def _get_sec_type(sym: str) -> str:
        for c in by_symbol.get(sym.upper(), []):
            return c.get("sec_type", "?")
        return "?"

    import src.core.reference_data as rd
    monkeypatch.setattr(
        rd, "get_instance",
        lambda: SimpleNamespace(contracts_by_symbol=by_symbol,
                                get_sec_type=_get_sec_type))
    return by_symbol


@pytest.fixture
def saved_contracts(monkeypatch):
    return _patch_reference_data(monkeypatch)


class TestPositionLikeFromOrder:
    def test_empty_symbol_returns_none(self, saved_contracts):
        assert btv._position_like_from_order(_order(symbol="")) is None
        assert btv._position_like_from_order(_order(symbol="  ")) is None

    def test_fut_local_symbol_resolves_to_root(self, saved_contracts):
        pos = btv._position_like_from_order(
            _order(symbol="MNQU6", quantity="2"))
        assert pos["symbol"] == "MNQ"
        assert pos["local_symbol"] == "MNQU6"
        assert pos["sec_type"] == "FUT"
        assert pos["con_id"] == "1002"
        assert pos["position"] == 2
        # The chip key derives from the root, exactly like a position row.
        assert btv._chip_key_for_position(pos) == ("MNQ", "FUT")

    def test_root_symbol_resolves(self, saved_contracts):
        pos = btv._position_like_from_order(_order(symbol="mnq"))
        assert pos["symbol"] == "MNQ"
        assert pos["sec_type"] == "FUT"

    def test_saved_stk(self, saved_contracts):
        pos = btv._position_like_from_order(_order(symbol="AAPL"))
        assert pos["sec_type"] == "STK"
        assert btv._chip_key_for_position(pos) == ("AAPL", "STK")

    def test_unsaved_occ_symbol_becomes_opt(self, saved_contracts):
        pos = btv._position_like_from_order(
            _order(symbol=_OCC, con_id=333, sec_type="", quantity="3"))
        assert pos["sec_type"] == "OPT"
        assert pos["local_symbol"] == _OCC.upper()
        assert pos["con_id"] == 333       # carried from the order dict
        assert pos["position"] == 3
        assert btv._chip_key_for_position(pos) == (_OCC.upper(), "OPT")

    def test_unsaved_plain_symbol_falls_back_to_order_fields(
            self, saved_contracts):
        pos = btv._position_like_from_order(
            _order(symbol="tsla", sec_type="STK"))
        assert pos["symbol"] == "TSLA"
        assert pos["sec_type"] == "STK"
        assert pos["local_symbol"] == ""

    def test_bad_quantity_becomes_zero(self, saved_contracts):
        pos = btv._position_like_from_order(
            _order(symbol="AAPL", quantity="–"))
        assert pos["position"] == 0

    def test_order_conid_backfills_conidless_saved_row(
            self, saved_contracts):
        """A saved OPT row with conid='' (older builds) must not shadow
        the con_id carried on the order dict — it's what lets
        _ensure_position_contract_saved heal the row."""
        saved_contracts[_OCC.upper()] = [
            {"symbol": _OCC.upper(), "local_symbol": _OCC,
             "sec_type": "OPT", "conid": "", "exchange": "SMART",
             "currency": "USD", "multiplier": "100"}]
        pos = btv._position_like_from_order(
            _order(symbol=_OCC, con_id=896505480))
        assert pos["con_id"] == 896505480


# ── focus_order delegates to focus_position ───────────────────────────────────

class TestFocusOrder:
    @pytest.fixture
    def view(self, qtbot, monkeypatch):
        monkeypatch.setattr(btv.config_manager,
                            "save_book_trader_active_symbol",
                            lambda s, path=None: None)
        monkeypatch.setattr(btv.config_manager, "save_book_trader_symbols",
                            lambda s, path=None: None)
        # Construct against the real ReferenceData (the constructor needs
        # its full API), then swap in the canned contracts cache.
        v = btv.BookTraderView(active_threads={})
        qtbot.addWidget(v)
        _patch_reference_data(monkeypatch)
        return v

    def test_delegates_translated_payload(self, view, monkeypatch):
        seen = []
        monkeypatch.setattr(view, "focus_position", seen.append)
        view.focus_order(_order(symbol="MNQU6", quantity="2"))
        assert len(seen) == 1
        assert seen[0]["symbol"] == "MNQ"
        assert seen[0]["position"] == 2

    def test_empty_symbol_is_noop(self, view, monkeypatch):
        seen = []
        monkeypatch.setattr(view, "focus_position", seen.append)
        view.focus_order(_order(symbol=""))
        assert seen == []


# ── Panel signal ──────────────────────────────────────────────────────────────

@pytest.fixture
def panel(qtbot) -> OrderStatusPanel:
    p = OrderStatusPanel()
    qtbot.addWidget(p)
    p.add_order("oid1", _order())
    return p


class TestPanelSignal:
    def test_symbol_cell_click_emits_order_dict(self, panel, qtbot):
        idx = panel._proxy.index(0, COL_SYMBOL)
        with qtbot.waitSignal(panel.symbol_clicked, timeout=1000) as blocker:
            panel._on_cell_clicked(idx)
        payload = blocker.args[0]
        assert payload["symbol"] == "AAPL"
        assert payload["quantity"] == "5"
        # A copy — mutating it must not touch the model's row.
        payload["symbol"] = "XXX"
        assert panel._model.order_at(0)["symbol"] == "AAPL"

    def test_other_columns_do_not_emit(self, panel, qtbot):
        with qtbot.assertNotEmitted(panel.symbol_clicked):
            panel._on_cell_clicked(panel._proxy.index(0, COL_QTY))

    def test_status_update_backfills_con_id(self, panel, qtbot):
        """The row is created by the pre-check emit, which may carry
        con_id=0; the real order's first status must fill it in."""
        panel.add_order("oid3", _order(order_id="oid3", con_id=0,
                                       sec_type=""))
        panel.update_order("oid3", _order(order_id="oid3", con_id=896505480,
                                          sec_type="OPT",
                                          status="Submitted"))
        src_row = panel._model._order_map["oid3"]
        stored = panel._model.order_at(src_row)
        assert stored["con_id"] == 896505480
        assert stored["sec_type"] == "OPT"

    def test_blank_symbol_row_does_not_emit(self, panel, qtbot):
        panel.add_order("oid2", _order(symbol="", order_id="oid2"))
        row = next(r for r in range(panel._proxy.rowCount())
                   if panel._model.order_at(
                       panel._proxy.mapToSource(
                           panel._proxy.index(r, 0)).row()
                   ).get("order_id") == "oid2")
        with qtbot.assertNotEmitted(panel.symbol_clicked):
            panel._on_cell_clicked(panel._proxy.index(row, COL_SYMBOL))
