"""Sweep-symbol pickers: type-ahead live IBKR search, stocks only.

The Cash Management page's four symbol fields share one inline
SymbolSearchPopup (external-input mode, same engine as the hot-button
modal) filtered to STK rows — futures roots and option chips never
appear in a cash-sweep picker.
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLineEdit

from src.ui.widgets.symbol_search_popup import SymbolSearchPopup


def _entry(sym, sec):
    return {"symbol": sym, "sec_type": sec, "conid": "1", "name": sym,
            "detail": "", "primary_exchange": "", "badge": sec,
            "source": "local"}


class TestStocksOnlyFilter:
    def _popup(self, qtbot, *, stocks_only):
        host = QLineEdit()
        qtbot.addWidget(host)
        p = SymbolSearchPopup(host, workers=[], exclude=set(),
                              external_input=host,
                              stocks_only=stocks_only)
        host._popup_pin = p  # pin: pytest-qt holds widgets weakly
        p._local = [_entry("NVDA", "STK"), _entry("NQ", "FUT"),
                    _entry("SGOV", "STK")]
        return p

    def test_fut_rows_hidden_when_stocks_only(self, qtbot):
        p = self._popup(qtbot, stocks_only=True)
        p._rebuild()
        assert p._lst.count() == 2          # NVDA + SGOV, no NQ

    def test_all_rows_without_filter(self, qtbot):
        p = self._popup(qtbot, stocks_only=False)
        p._rebuild()
        assert p._lst.count() == 3

    def test_remote_fut_results_filtered_too(self, qtbot):
        p = self._popup(qtbot, stocks_only=True)
        p._local = []
        p._ext_input.setText("E")
        p._remote = [{"symbol": "ES", "sec_type": "FUT", "conid": "2",
                      "name": "E-mini"},
                     {"symbol": "EFG", "sec_type": "STK", "conid": "3",
                      "name": "EFG Corp"}]
        p._remote_ready = True
        p._rebuild()
        # One STK result row + the "No TWS connection" note (workers
        # is empty here) — the FUT result contributes nothing.
        assert p._lst.count() == 2


class TestCashPageDropdown:
    @pytest.fixture
    def page(self, qtbot):
        from src.ui.widgets.preferences_dialog import _CashManagementPage
        prefs = {"cash_management": {"sweep_symbols": []}}
        pg = _CashManagementPage(prefs, workers=[])
        qtbot.addWidget(pg)
        return pg

    def test_dropdown_created_stocks_only_and_follows_field(self, page):
        e0, e1 = page._symbol_edits[0], page._symbol_edits[1]
        page._show_dropdown(e0)
        assert page._dropdown is not None
        assert page._dropdown._stocks_only is True
        assert page._dropdown_edit is e0
        # Same dropdown re-anchors to another field.
        dd = page._dropdown
        page._show_dropdown(e1)
        assert page._dropdown is dd
        assert page._dropdown._ext_input is e1

    def test_pick_fills_field_and_persists(self, page):
        edit = page._symbol_edits[0]
        page._show_dropdown(edit)
        page._on_symbol_picked({"symbol": "bil", "sec_type": "STK",
                                "conid": "9", "source": "ibkr"})
        assert edit.text() == "BIL"
        cm = page._prefs["cash_management"]
        assert cm["sweep_symbols"] == ["BIL"]
        assert cm["sweep_symbol"] == "BIL"
        assert not page._dropdown.isVisible()

    def test_collect_reports_all_fields(self, page):
        for i, sym in enumerate(["SGOV", "BIL", "", "sgov"]):
            page._symbol_edits[i].setText(sym)
        out = page.collect()
        # Blanks skipped, dupes dropped, upper-cased.
        assert out["sweep_symbols"] == ["SGOV", "BIL"]
        assert out["sweep_symbol"] == "SGOV"
