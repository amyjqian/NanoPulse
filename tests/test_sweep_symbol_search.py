"""Sweep-symbol pickers: type-ahead live IBKR search, stocks only.

The Cash Management page's four symbol fields share one inline
SymbolSearchPopup (external-input mode, same engine as the hot-button
modal) filtered to STK rows — futures roots and option chips never
appear in a cash-sweep picker.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
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

    def test_dropdown_mode_stocks_only(self, page):
        # The hot-button modal pattern: the FIELD is the search box —
        # the popup opens in external-input mode (its own box hidden),
        # filtered to stocks.
        edit = page._symbol_edits[0]
        page._show_dropdown(edit)
        assert page._popup is not None
        assert page._popup._stocks_only is True
        assert page._popup._ext_input is edit
        assert page._popup._search.isHidden()
        page._popup.close()

    def test_typing_lands_in_field_and_opens_dropdown(self, page, qtbot):
        edit = page._symbol_edits[0]
        qtbot.keyClick(edit, Qt.Key.Key_B)
        assert edit.text() == "b"
        assert page._popup is not None
        assert page._popup._ext_input is edit
        page._popup.close()

    def test_dropdown_is_shared_and_reattached(self, page):
        # One reusable instance serves all four fields; opening it from
        # another field re-points the live query at that field.
        e0, e1 = page._symbol_edits[0], page._symbol_edits[1]
        page._show_dropdown(e0)
        first = page._popup
        page._show_dropdown(e1)
        assert page._popup is first
        assert page._popup._ext_input is e1
        page._popup.close()

    def test_pick_fills_field_and_persists(self, page):
        edit = page._symbol_edits[0]
        page._show_dropdown(edit)
        page._on_symbol_picked({"symbol": "bil", "sec_type": "STK",
                                "conid": "9", "source": "ibkr"})
        assert edit.text() == "BIL"
        cm = page._prefs["cash_management"]
        assert cm["sweep_symbols"] == ["BIL"]
        assert cm["sweep_symbol"] == "BIL"
        assert not page._popup.isVisible()   # pick dismisses the list

    def test_no_inline_clear_button(self, page):
        # The field edits like a normal line edit — no inline "x",
        # keeping the minimal search-box look.
        for edit in page._symbol_edits:
            assert edit.actions() == []

    def test_backspace_edits_text_normally(self, page, qtbot):
        from PySide6.QtCore import Qt as _Qt
        edit = page._symbol_edits[0]
        edit.setText("BIL")
        qtbot.keyClick(edit, _Qt.Key.Key_Backspace)
        assert edit.text() == "BI"
        page._popup.close()

    def test_escape_dismisses_dropdown(self, page, qtbot):
        from PySide6.QtCore import Qt as _Qt
        edit = page._symbol_edits[0]
        page._show_dropdown(edit)
        assert page._popup.isVisible()
        qtbot.keyClick(edit, _Qt.Key.Key_Escape)
        assert not page._popup.isVisible()

    def test_click_outside_dismisses_dropdown(self, page, qtbot):
        # The dropdown is a non-activating Tool window (no Qt.Popup
        # mouse grab), and the page's FocusOut hook only fires when the
        # click lands on a focusable widget — a click on the page's own
        # background used to leave the list hanging open.
        from PySide6.QtCore import QPoint
        from PySide6.QtTest import QTest
        page.resize(600, 900)
        page.show()
        qtbot.waitExposed(page)
        edit = page._symbol_edits[0]
        page._show_dropdown(edit)
        assert page._popup.isVisible()
        # Bottom-right corner of the page — blank background, well
        # clear of the dropdown's own rect.
        spot = QPoint(page.width() - 5, page.height() - 5)
        assert not page._popup.frameGeometry().contains(page.mapToGlobal(spot))
        QTest.mouseClick(page, Qt.MouseButton.LeftButton, pos=spot)
        assert not page._popup.isVisible()

    def test_click_inside_dropdown_keeps_it_open(self, page, qtbot):
        from PySide6.QtTest import QTest
        page.show()
        qtbot.waitExposed(page)
        page._show_dropdown(page._symbol_edits[0])
        pop = page._popup
        QTest.mouseClick(pop, Qt.MouseButton.LeftButton,
                         pos=pop.rect().center())
        assert pop.isVisible()
        pop.close()

    def test_click_on_attached_field_keeps_it_open(self, page, qtbot):
        # The field's own press reopens the dropdown — the outside-click
        # watch must not shut it first, or the list flickers.
        from PySide6.QtTest import QTest
        page.show()
        qtbot.waitExposed(page)
        edit = page._symbol_edits[0]
        page._show_dropdown(edit)
        QTest.mouseClick(edit, Qt.MouseButton.LeftButton)
        assert page._popup.isVisible()
        page._popup.close()

    def test_pick_stores_contract_meta(self, page):
        # The pick's metadata is kept under sweep_contracts — that's
        # what lets the symbol price/trade without being a chip.
        edit = page._symbol_edits[0]
        page._show_dropdown(edit)
        page._on_symbol_picked({"symbol": "SGOV", "sec_type": "STK",
                                "conid": "563439551", "source": "ibkr",
                                "primary_exchange": "ARCA"})
        meta = page._prefs["cash_management"]["sweep_contracts"]["SGOV"]
        assert meta["conid"] == "563439551"
        assert meta["exchange"] == "SMART"   # STK default

    def test_cleared_symbol_prunes_its_contract(self, page):
        edit = page._symbol_edits[0]
        page._show_dropdown(edit)
        page._on_symbol_picked({"symbol": "SGOV", "sec_type": "STK",
                                "conid": "563439551", "source": "ibkr"})
        edit.clear()
        page._on_edit()
        assert page._prefs["cash_management"]["sweep_contracts"] == {}

    def test_focus_out_commits_text(self, page):
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QFocusEvent
        edit = page._symbol_edits[0]
        edit.setText("sgov")
        page.eventFilter(edit, QFocusEvent(QEvent.Type.FocusOut,
                                           Qt.FocusReason.TabFocusReason))
        assert edit.text() == "SGOV"
        assert (page._prefs["cash_management"]["sweep_symbols"]
                == ["SGOV"])

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
    """The keyboard cursor paints on the row widget itself — the ONLY
    mechanism that renders in every context (::item:selected paints
    nothing under the Settings dialog's ancestor stylesheet).  Child
    labels pin background:transparent so the container's blue can't
    surface opaque boxes behind the text/pill."""

    def _popup(self, qtbot):
        host = QLineEdit()
        qtbot.addWidget(host)
        p = SymbolSearchPopup(host, workers=[], exclude=set(),
                              external_input=host)
        host._popup_pin = p
        p._local = [_entry("NVDA", "STK"), _entry("SGOV", "STK"),
                    _entry("BIL", "STK")]
        p._rebuild()
        return p

    def test_stepped_row_gets_distinct_background(self, qtbot):
        p = self._popup(qtbot)

        def _bg(i):
            return p._lst.itemWidget(p._lst.item(i)).styleSheet()

        # First data row highlighted after rebuild, others clear.
        assert "background" in _bg(0)
        assert _bg(1) == "" and _bg(2) == ""
        p._step(+1)
        assert p._lst.currentRow() == 1
        assert _bg(0) == "" and "background" in _bg(1)
        p._step(-1)
        assert "background" in _bg(0) and _bg(1) == ""

    def test_child_labels_pin_transparent_backgrounds(self, qtbot):
        # Guards against the nested-boxes artifact: styling the row
        # container must never surface opaque fills behind children.
        p = self._popup(qtbot)
        row = p._lst.itemWidget(p._lst.item(0))
        from PySide6.QtWidgets import QLabel
        labels = row.findChildren(QLabel)
        assert labels
        transparent = [lb for lb in labels
                       if "background:transparent" in lb.styleSheet()]
        # All but the circular avatar (which owns an opaque fill).
        assert len(transparent) == len(labels) - 1

    def test_search_box_has_no_focus_accent_frame(self, qtbot):
        p = self._popup(qtbot)
        assert ":focus" not in p._search.styleSheet()
