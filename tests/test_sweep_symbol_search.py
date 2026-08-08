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


class TestOneControlFeel:
    def test_first_keystroke_kicks_search(self, qtbot):
        # Dropdown is constructed lazily on the first typed character —
        # that keystroke predates the textChanged wiring, so the ctor
        # must kick the search itself (empty "Results" box otherwise).
        host = QLineEdit()
        qtbot.addWidget(host)
        host.setText("S")
        p = SymbolSearchPopup(host, workers=[object()], exclude=set(),
                              external_input=host)
        host._popup_pin = p
        assert p._searching is True
        p._debounce.stop()   # fake worker can't take the real search

    def test_attach_input_rewires_query_feed(self, qtbot):
        e0, e1 = QLineEdit(), QLineEdit()
        qtbot.addWidget(e0)
        qtbot.addWidget(e1)
        p = SymbolSearchPopup(e0, workers=[object()], exclude=set(),
                              external_input=e0)
        e0._popup_pin = p
        e1.setText("BI")
        p.attach_input(e1)
        assert p._query() == "BI"
        assert p._searching is True
        # The old field no longer feeds the query.
        p._searching = False
        e0.setText("XYZ")
        assert p._query() == "BI"
        assert p._searching is False
        # The new field does.
        e1.setText("BIL")
        assert p._searching is True
        p._debounce.stop()   # fake worker can't take the real search

    def test_dropdown_matches_anchor_width(self, qtbot):
        host = QLineEdit()
        qtbot.addWidget(host)
        host.setFixedWidth(300)
        host.show()
        p = SymbolSearchPopup(host, workers=[], exclude=set(),
                              external_input=host)
        host._popup_pin = p
        p.open_below(host)
        assert p.width() == 300
        # Floor keeps rows legible under very narrow anchors.
        host.setFixedWidth(120)
        p.open_below(host)
        assert p.width() == 240


class TestCursorHighlight:
    def test_stepped_row_gets_distinct_background(self, qtbot):
        host = QLineEdit()
        qtbot.addWidget(host)
        p = SymbolSearchPopup(host, workers=[], exclude=set(),
                              external_input=host)
        host._popup_pin = p
        p._local = [_entry("NVDA", "STK"), _entry("SGOV", "STK"),
                    _entry("BIL", "STK")]
        p._rebuild()

        def _bg(i):
            return p._lst.itemWidget(p._lst.item(i)).styleSheet()

        # First data row highlighted after rebuild, others clear.
        assert "background" in _bg(0)
        assert _bg(1) == "" and _bg(2) == ""
        p._step(+1)
        assert _bg(0) == "" and "background" in _bg(1)
        p._step(-1)
        assert "background" in _bg(0) and _bg(1) == ""
