"""Hot-button modal — live symbol search picks own their contract.

The SPECIFIC symbol field opens the shared SymbolSearchPopup; a pick
stores qualified contract metadata on the draft (the hot button owns its
contract identity), and a manual edit away from the picked symbol drops
that metadata so a stale conid can never ride along.
"""
from __future__ import annotations

import pytest

from src.ui.widgets.order_entry_panel import _HotButtonModal


@pytest.fixture
def modal(qtbot) -> _HotButtonModal:
    dlg = _HotButtonModal(None, default_specific=True, workers=[])
    qtbot.addWidget(dlg)
    return dlg


def test_stk_pick_stores_contract_and_symbol(modal):
    modal._on_symbol_picked({
        "symbol": "nvda", "sec_type": "STK", "conid": "4815747",
        "currency": "USD", "primary_exchange": "ISLAND",
        "name": "NVIDIA", "source": "ibkr",
    })
    assert modal._draft["symbol"] == "NVDA"
    meta = modal._draft["contract"]
    assert meta["conid"] == "4815747"
    assert meta["exchange"] == "SMART"
    # Status line resolves from the draft's own metadata.
    assert "not found" not in modal._lbl_sym_status.text()


def test_manual_edit_drops_picked_contract(modal):
    modal._on_symbol_picked({
        "symbol": "NVDA", "sec_type": "STK", "conid": "4815747",
        "currency": "USD", "source": "ibkr",
    })
    assert "contract" in modal._draft
    modal._inp_symbol.setText("NVDX")          # user types something else
    assert "contract" not in modal._draft


def test_pick_survives_values_roundtrip(modal):
    modal._on_symbol_picked({
        "symbol": "NVDA", "sec_type": "STK", "conid": "4815747",
        "currency": "USD", "source": "ibkr",
    })
    values = modal.values()
    assert values["symbol"] == "NVDA"
    assert values["contract"]["conid"] == "4815747"


def test_conidless_pick_stores_no_contract(modal):
    modal._on_symbol_picked({
        "symbol": "NQ", "sec_type": "FUT", "source": "local"})
    # local FUT row without conid → symbol adopted, no metadata stored
    assert modal._draft["symbol"] == "NQ"
    assert "contract" not in modal._draft


def test_typing_in_symbol_field_searches_inline(modal, qtbot):
    """Typing stays in the modal's own field and drives an inline
    results dropdown — no separate search box to click into."""
    from PySide6.QtCore import Qt

    modal.show()
    qtbot.waitExposed(modal)
    qtbot.mouseClick(modal._inp_symbol, Qt.MouseButton.LeftButton)
    dd = modal._symbol_dropdown
    assert dd is not None and dd.isVisible()

    qtbot.keyClick(modal._inp_symbol, "c")
    assert modal._inp_symbol.text() == "c"     # typing stays in the field
    assert dd._query() == "c"                  # dropdown reads it live

    # Esc dismisses the dropdown only — the dialog stays open.
    qtbot.keyClick(modal._inp_symbol, Qt.Key.Key_Escape)
    assert not dd.isVisible()
    assert modal.isVisible()


def test_pick_closes_dropdown_and_keeps_field(modal, qtbot):
    from PySide6.QtCore import Qt
    modal.show()
    qtbot.waitExposed(modal)
    qtbot.mouseClick(modal._inp_symbol, Qt.MouseButton.LeftButton)
    modal._on_symbol_picked({
        "symbol": "NVDA", "sec_type": "STK", "conid": "4815747",
        "currency": "USD", "source": "ibkr",
    })
    assert modal._inp_symbol.text() == "NVDA"
    assert modal._symbol_dropdown is not None
    assert not modal._symbol_dropdown.isVisible()   # closed after pick


def test_closing_dialog_closes_dropdown(modal, qtbot):
    """Cancel/Save/Esc on the dialog must take the floating dropdown
    down with it — it's a tool window that won't close on its own."""
    from PySide6.QtCore import Qt
    modal.show()
    qtbot.waitExposed(modal)
    qtbot.mouseClick(modal._inp_symbol, Qt.MouseButton.LeftButton)
    assert modal._symbol_dropdown.isVisible()
    modal.reject()                              # Cancel path → done()
    assert not modal._symbol_dropdown.isVisible()


def test_bracket_inputs_are_tick_steppers_without_unit_label(modal):
    """TP/SL/TS value inputs use the shared TickPriceInput (rounded box,
    wheel/arrow stepping, no '$ price' label, no trailing zeros)."""
    from src.ui.widgets.order_entry_panel import TickPriceInput
    for w in (modal._tp_widgets, modal._sl_widgets, modal._tr_widgets):
        assert isinstance(w["val"], TickPriceInput)
        assert "unit_lbl" not in w
        assert w["val"].text() == "80"          # not "80.0"


def test_bracket_tick_follows_target_symbol(modal):
    # Unknown / DEFAULT-mode symbol → whole-dollar steps.
    assert modal._bracket_tick() == 1.0
    # Picked future with captured min_tick → its tick.
    modal._draft["symbol"] = "MNQ"
    modal._draft["contract"] = {"conid": "1", "sec_type": "FUT",
                                "min_tick": 0.25}
    assert modal._bracket_tick() == 0.25
    # Known futures root without metadata → canonical table.
    modal._draft["contract"] = {}
    modal._draft["symbol"] = "PL"
    assert modal._bracket_tick() == 0.10
    # Stock metadata → cent steps.
    modal._draft["symbol"] = "NVDA"
    modal._draft["contract"] = {"conid": "2", "sec_type": "STK"}
    assert modal._bracket_tick() == 0.01


def test_typing_reopens_dropdown_without_focus_dependence(modal, qtbot):
    """Regression: the dropdown trigger rides textEdited (user
    keystrokes), NOT textChanged + hasFocus() — hasFocus() went stale
    after the Tool dropdown stole activation, leaving the user typing
    with no search popup."""
    from PySide6.QtCore import Qt

    modal.show()
    qtbot.waitExposed(modal)
    qtbot.mouseClick(modal._inp_symbol, Qt.MouseButton.LeftButton)
    dd = modal._symbol_dropdown
    qtbot.keyClick(modal._inp_symbol, Qt.Key.Key_Escape)   # dismiss
    assert not dd.isVisible()
    # Simulate the stale state: field reports no focus, user types.
    modal._inp_symbol.clearFocus()
    qtbot.keyClick(modal._inp_symbol, "b")
    assert modal._symbol_dropdown.isVisible()
    assert modal._symbol_dropdown._query() == "b"
    modal._symbol_dropdown.close()


def test_programmatic_settext_never_opens_dropdown(modal):
    # Draft loads / pick adoption use setText — textEdited stays
    # silent for those, so no popup flashes during dialog setup.
    modal._inp_symbol.setText("NVDA")
    assert (modal._symbol_dropdown is None
            or not modal._symbol_dropdown.isVisible())
