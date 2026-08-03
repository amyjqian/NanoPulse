"""QtyChipRow SH/% unit-toggle behaviour (pytest-qt).

Locks in: share mode renders the numeric-chip (JetBrains Mono) styles,
toggle cycling, per-unit preset + custom-spinner memory, the 0–100
percent validator, disabled-chip states and resolved-share signalling.
"""
from __future__ import annotations

import pytest

from src.ui.widgets.order_panel_parts import QtyChipRow
from src.ui.widgets.size_unit_parts import UNIT_PCT, UNIT_SH

_STK = ([10, 25, 50, 100, 500], 100, list(range(1, 101)))
# Provider rows: (account, net_liq, buying_power, held_shares_of_symbol)
_ACCT = [("ACC1", 1_420_000.0, 0.0, 0.0)]


def _make(qtbot, *, allow=True, accounts=None, price=182.40):
    row = QtyChipRow()
    qtbot.addWidget(row)
    row.set_presets(*_STK, allow_percent=allow)
    acc = _ACCT if accounts is None else accounts
    row.set_price_provider(lambda: price)
    row.set_symbol_provider(lambda: "NVDA")
    row.set_percent_context(lambda _sym: acc)
    return row


def _chip(row, value):
    for btn in row._buttons:
        if btn.property("qty_value") == value:
            return btn
    raise AssertionError(f"no chip with value {value}")


def test_no_toggle_without_allow_percent(qtbot):
    row = _make(qtbot, allow=False)
    assert row._btn_unit.isHidden()
    assert row.current_unit() == UNIT_SH
    assert row.current_percent() is None


def test_share_mode_stylesheets_byte_identical(qtbot):
    # SH chips must carry the exact numeric-chip stylesheet from
    # size_unit_parts (JetBrains Mono — numbers you scan), which
    # deliberately diverges from the Inter symbol-chip style.
    from src.ui.widgets.size_unit_parts import _SH_CHIP_ON, _SH_CHIP_OFF
    row = _make(qtbot)
    assert _chip(row, 100).styleSheet() == _SH_CHIP_ON
    assert _chip(row, 25).styleSheet() == _SH_CHIP_OFF
    assert "JetBrains Mono" in _chip(row, 100).styleSheet()


def test_toggle_cycles_and_relabels(qtbot):
    row = _make(qtbot)
    assert row._btn_unit.text() == "SH"
    row._btn_unit.click()
    assert row.current_unit() == UNIT_PCT
    assert row._btn_unit.text() == "%"
    # Plain numbers in percent mode too — the gold styling is the unit
    # signal, not a % suffix.
    assert [b.text() for b in row._buttons] == \
        ["10", "25", "50", "75", "100"]
    row._btn_unit.click()
    assert row.current_unit() == UNIT_SH
    assert [b.text() for b in row._buttons] == \
        ["10", "25", "50", "100", "500"]


def test_percent_resolves_shares(qtbot):
    row = _make(qtbot)
    row._btn_unit.click()
    fired = []
    row.qty_changed.connect(fired.append)
    previews = []
    row.preview_changed.connect(lambda b, s: previews.append((b, s)))
    _chip(row, 25).click()
    # 25% × $1.42M @ 182.40 → 1,946 (spec acceptance case).
    assert fired[-1] == 1946
    assert row.current_qty() == 1946
    assert row.current_percent() == 25
    assert previews[-1] == ("B:1,946", "")


def test_per_unit_selection_and_custom_memory(qtbot):
    row = _make(qtbot)
    _chip(row, 500).click()                       # share preset
    row._btn_unit.click()                         # → %
    _chip(row, 50).click()                        # percent preset
    row._btn_unit.click()                         # → SH
    assert row.current_qty() == 500
    assert _chip(row, 500).styleSheet() != _chip(row, 10).styleSheet()
    row._btn_unit.click()                         # → %
    assert row.current_percent() == 50

    # Custom values are remembered per unit: 750 shares survives a
    # percent round-trip (and never becomes 750%).
    row._btn_unit.click()                         # → SH
    row._custom.setText("750")
    assert row.current_qty() == 750
    row._btn_unit.click()                         # → %
    assert row._custom.text() == ""               # % has its own memory
    assert row.current_percent() == 50
    row._custom.setText("3")
    row._btn_unit.click()                         # → SH
    assert row._custom.text() == "750"
    assert row.current_qty() == 750
    row._btn_unit.click()                         # → %
    assert row._custom.text() == "3"
    assert row.current_percent() == 3


def test_percent_validator_capped_at_100(qtbot):
    row = _make(qtbot)
    row._btn_unit.click()
    assert row._custom._edit.validator().top() == 100
    row._btn_unit.click()
    assert row._custom._edit.validator().top() == 999_999_999


def test_custom_input_recolors_with_unit(qtbot):
    # The input is ONE rounded box: bg/border/radius live on the
    # container widget; percent mode swaps it to the chips' dark-gold
    # palette and toggling back restores the grey share palette.
    row = _make(qtbot)
    box = row._custom
    assert "#232A3B" in box.styleSheet()            # grey share box
    assert "border-radius:6px" in box.styleSheet()
    assert "background:transparent" in box._edit.styleSheet()
    row._btn_unit.click()                          # → %
    assert "rgba(45,180,164,.12)" in box.styleSheet()            # CLR_PCT_BG box
    assert "border-radius:6px" in box.styleSheet()
    assert "#2DB4A4" in box._edit.styleSheet()      # CLR_PCT_TEXT
    assert box._edit.placeholderText() == "%"
    row._btn_unit.click()                          # → SH
    assert box._edit.placeholderText() == "Qty"
    assert "#232A3B" in box.styleSheet()
    assert "#A3AFC2" in box._edit.styleSheet()


def test_chips_disable_without_price_or_accounts(qtbot):
    row = _make(qtbot, price=0.0)
    row._btn_unit.click()
    assert all(not b.isEnabled() for b in row._buttons)
    assert row.current_qty() == 0

    row2 = _make(qtbot, accounts=[])
    row2._btn_unit.click()
    assert all(not b.isEnabled() for b in row2._buttons)


def test_multi_account_preview_shows_total(qtbot):
    row = _make(qtbot, accounts=[("A", 1_420_000.0, 0.0, 0.0),
                                 ("B", 1_000_000.0, 0.0, 0.0)])
    row._btn_unit.click()
    previews = []
    row.preview_changed.connect(lambda b, s: previews.append((b, s)))
    _chip(row, 25).click()
    # A → 1,946; B → 250,000 / 182.40 = 1,370.6 → 1,370.  The readout
    # shows the TOTAL going out across accounts.
    assert previews[-1] == ("B:3,316", "")
    # Conservative internal preview: the smallest account's count.
    assert row.current_qty() == 1370


def test_sell_side_resolves_from_position(qtbot):
    # 600 shares held → SELL 25% = 150 (position basis); BUY 25% still
    # resolves from net liq.  Readout shows both sides.
    row = _make(qtbot, accounts=[("A", 1_420_000.0, 0.0, 600.0)])
    row._btn_unit.click()
    previews = []
    row.preview_changed.connect(lambda b, s: previews.append((b, s)))
    _chip(row, 25).click()
    assert row.percent_side_qty("BUY") == 1946
    assert row.percent_side_qty("SELL") == 150
    assert previews[-1] == ("B:1,946", "S:150")


def test_sell_enabled_without_price(qtbot):
    # No live price: BUY can't resolve, but a holder can still
    # percent-SELL — chips stay enabled on the sell side alone.
    row = _make(qtbot, accounts=[("A", 1_420_000.0, 0.0, 600.0)],
                price=0.0)
    row._btn_unit.click()
    assert all(b.isEnabled() for b in row._buttons)
    assert row.percent_side_qty("BUY") == 0
    assert row.percent_side_qty("SELL") == 150            # 25% default
    assert row._pct_ctx.sell_preview_total(25) == 150


def test_custom_focus_drops_chip_highlight(qtbot):
    from src.ui.widgets.size_unit_parts import _SH_CHIP_ON, _SH_CHIP_OFF
    row = _make(qtbot)
    assert _chip(row, 100).styleSheet() == _SH_CHIP_ON
    # Cursor enters the custom field → no chip reads selected.
    row._custom.focusChanged.emit(True)
    assert _chip(row, 100).styleSheet() == _SH_CHIP_OFF
    # Leaves it empty → the active chip highlight comes back.
    row._custom.focusChanged.emit(False)
    assert _chip(row, 100).styleSheet() == _SH_CHIP_ON
    # Leaves it with a typed value → chips stay unselected.
    row._custom.setText("750")
    row._custom.focusChanged.emit(False)
    assert _chip(row, 100).styleSheet() == _SH_CHIP_OFF
    assert row.current_qty() == 750


def test_custom_value_keeps_selected_look_unfocused(qtbot):
    row = _make(qtbot)
    box = row._custom
    # Typed share value → box renders like the selected (blue) chip even
    # after focus moves elsewhere.
    box.setText("750")
    box.focusChanged.emit(False)
    assert "#539BF5" in box.styleSheet()
    # Percent mode, empty field → idle dark gold.
    row._btn_unit.click()
    assert "rgba(45,180,164,.12)" in box.styleSheet()
    # Typed percent → solid gold selected look, focused or not.
    box.setText("12")
    box.focusChanged.emit(False)
    assert "#2DB4A4" in box.styleSheet()               # Theme.CLR_PCT
    # Clicking a preset clears the field → back to idle.
    _chip(row, 25).click()
    assert "rgba(45,180,164,.12)" in box.styleSheet()


def test_typed_preset_value_adopts_the_chip(qtbot):
    from src.ui.widgets.size_unit_parts import _SH_CHIP_ON
    row = _make(qtbot)
    box = row._custom
    # Typing an existing preset value → on focus-out the chip takes
    # over: chip selected, field cleared and back to idle.
    box.setText("50")
    box.focusChanged.emit(False)
    assert box.text() == ""
    assert _chip(row, 50).styleSheet() == _SH_CHIP_ON
    assert "#539BF5" not in box.styleSheet()           # box unselected
    assert row.current_qty() == 50
    # A non-preset value stays in the box (selected look).
    box.setText("750")
    box.focusChanged.emit(False)
    assert box.text() == "750"
    assert "#539BF5" in box.styleSheet()
    # Same in percent mode: 25 is a percent preset.
    row._btn_unit.click()
    box.setText("25")
    box.focusChanged.emit(False)
    assert box.text() == ""
    assert row.current_percent() == 25
    assert "rgba(45,180,164,.12)" in box.styleSheet()               # idle dark gold


def test_spinbox_arrow_keys_step_ladder(qtbot):
    from PySide6.QtCore import Qt as _Qt
    # Share popup ladder like the real one: 1–10, then 20…100, 200…1000.
    ladder = (list(range(1, 11)) + list(range(20, 110, 10))
              + list(range(200, 1100, 100)))
    row = QtyChipRow()
    qtbot.addWidget(row)
    row.set_presets([10, 25, 50, 100, 500], 100, ladder, allow_percent=True)
    row.set_price_provider(lambda: 182.40)
    row.set_symbol_provider(lambda: "NVDA")
    row.set_percent_context(lambda _sym: _ACCT)
    edit = row._custom._edit

    qtbot.keyClick(edit, _Qt.Key.Key_Up)          # empty → first rung
    assert row._custom.text() == "1"
    qtbot.keyClick(edit, _Qt.Key.Key_Up)
    assert row._custom.text() == "2"
    qtbot.keyClick(edit, _Qt.Key.Key_Down)
    assert row._custom.text() == "1"
    qtbot.keyClick(edit, _Qt.Key.Key_Down)        # floor at first rung
    assert row._custom.text() == "1"
    # Ladder jumps: 10 → 20 → 30; between-rung values snap outward.
    row._custom.setText("10")
    qtbot.keyClick(edit, _Qt.Key.Key_Up)
    assert row._custom.text() == "20"
    qtbot.keyClick(edit, _Qt.Key.Key_Up)
    assert row._custom.text() == "30"
    row._custom.setText("25")
    qtbot.keyClick(edit, _Qt.Key.Key_Up)
    assert row._custom.text() == "30"
    row._custom.setText("25")
    qtbot.keyClick(edit, _Qt.Key.Key_Down)
    assert row._custom.text() == "20"
    row._custom.setText("100")
    qtbot.keyClick(edit, _Qt.Key.Key_Up)
    assert row._custom.text() == "200"
    # Top of the ladder: stepping up stays put.
    row._custom.setText("1000")
    qtbot.keyClick(edit, _Qt.Key.Key_Up)
    assert row._custom.text() == "1000"

    # Percent mode: ladder is 1–100, so stepping is ±1, clamped at 100.
    row._btn_unit.click()
    row._custom.setText("100")
    qtbot.keyClick(edit, _Qt.Key.Key_Up)
    assert row._custom.text() == "100"
    qtbot.keyClick(edit, _Qt.Key.Key_Down)
    assert row._custom.text() == "99"


def test_symbol_switch_keeps_percent_mode(qtbot):
    row = _make(qtbot)
    row._btn_unit.click()
    _chip(row, 50).click()                        # pick 50%
    assert row.current_percent() == 50
    # Stock → stock switch: STAYS in % mode, pct resets to the default.
    row.set_presets(*_STK, allow_percent=True)
    assert row.current_unit() == UNIT_PCT
    assert row.current_percent() == 25
    assert row._custom._edit.validator().top() == 100
    assert [b.text() for b in row._buttons] == \
        ["10", "25", "50", "75", "100"]
    # Switching to a non-percent symbol (FUT) forces shares.
    row.set_presets([1, 2, 3, 5], 1, list(range(1, 101)),
                    allow_percent=False)
    assert row.current_unit() == UNIT_SH
    assert row._btn_unit.isHidden()
    # And a fresh SH row still starts in shares after re-allowing %.
    row.set_presets(*_STK, allow_percent=True)
    assert row.current_unit() == UNIT_SH
    assert row.current_qty() == 100
    # Toggle survives the rebuilds (it must not be deleteLater'd).
    row._btn_unit.click()
    assert row.current_unit() == UNIT_PCT
