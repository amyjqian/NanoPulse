"""STK chip group: 3 rows max, replace-last-chip when full.

The STK group renders up to columns × 3 chips (15); FUT keeps 2 rows
(10).  Adding a stock to a full STK group replaces the group's LAST
chip (row 3, column 5) instead of persisting an invisible overflow
entry.
"""
from __future__ import annotations

import pytest

import src.ui.widgets.book_trader_view as btv
from src.ui.widgets.order_panel_parts import SymbolChipBar


def _stk(n: int) -> list[tuple[str, str]]:
    return [(f"S{i:02d}", "STK") for i in range(n)]


def _fut(n: int) -> list[tuple[str, str]]:
    return [(f"F{i:02d}", "FUT") for i in range(n)]


class TestChipBarCaps:
    def test_stk_renders_up_to_three_rows(self, qtbot):
        bar = SymbolChipBar(columns=5, max_rows_per_group=2)
        qtbot.addWidget(bar)
        bar.set_symbols(_stk(16))
        assert len(bar._buttons) == 15          # 5 × 3, 16th not rendered
        assert "S14" in bar._buttons and "S15" not in bar._buttons

    def test_fut_still_capped_at_two_rows(self, qtbot):
        bar = SymbolChipBar(columns=5, max_rows_per_group=2)
        qtbot.addWidget(bar)
        bar.set_symbols(_fut(12))
        assert len(bar._buttons) == 10          # 5 × 2 unchanged


class TestBookTraderReplaceLast:
    @pytest.fixture
    def view(self, qtbot, monkeypatch):
        monkeypatch.setattr(btv.config_manager, "save_book_trader_symbols",
                            lambda s, path=None: None)
        monkeypatch.setattr(btv.config_manager,
                            "save_book_trader_active_symbol",
                            lambda s, path=None: None)
        monkeypatch.setattr(btv, "_sec_type_for",
                            lambda s: "FUT" if s.startswith("F") else "STK")
        v = btv.BookTraderView(active_threads={})
        qtbot.addWidget(v)
        return v

    def test_full_stk_group_replaces_last_chip(self, view):
        view._symbols = [f"S{i:02d}" for i in range(15)] + ["F00"]
        view._add_symbol("NEW")
        stk = [s for s in view._symbols if not s.startswith("F")]
        assert len(stk) == 15
        assert stk[-1] == "NEW"                 # takes row 3, column 5
        # Dropping the chip entry IS the cleanup — its metadata leaves
        # with it (no shared contract store to scrub any more).
        assert "S14" not in view._symbols       # previous last dropped
        assert view._active_symbol == "NEW"
        # FUT membership untouched.
        assert "F00" in view._symbols

    def test_below_cap_appends_normally(self, view):
        view._symbols = [f"S{i:02d}" for i in range(14)]
        view._add_symbol("NEW")
        assert len(view._symbols) == 15

    def test_fut_add_never_evicts_stk(self, view):
        view._symbols = [f"S{i:02d}" for i in range(15)]
        view._add_symbol("F99")
        assert len(view._symbols) == 16         # future appended, no eviction


class TestChipMetadataLifecycle:
    """A chip owns its contract identity: added with metadata it can
    resolve/subscribe; removing the chip removes the metadata with it."""

    def test_add_with_meta_persists_and_remove_drops(self, qtbot, monkeypatch):
        import src.core.config_manager as cm
        from src.core import instruments
        monkeypatch.setattr(btv.config_manager,
                            "save_book_trader_active_symbol",
                            lambda s, path=None: None)
        v = btv.BookTraderView(active_threads={})
        qtbot.addWidget(v)

        v._add_symbol("NVDA", meta={"conid": "4815747", "sec_type": "STK",
                                    "exchange": "SMART", "currency": "USD",
                                    "local_symbol": "NVDA"})
        entries = {e["symbol"]: e for e in cm.load_book_trader_entries()}
        assert entries["NVDA"]["conid"] == "4815747"
        assert instruments.get_contract("NVDA")["conid"] == "4815747"
        assert instruments.build_md_params("NVDA")["conid"] == 4815747

        v._on_symbol_remove("NVDA")
        assert instruments.get_contract("NVDA") is None   # gone with the chip


class TestChipDragReorder:
    """Drag-reorder takes the drop target's position — any direction,
    any slot (same semantics as the account-group pills)."""

    def _bar(self, qtbot, entries):
        bar = SymbolChipBar(columns=5, max_rows_per_group=2)
        qtbot.addWidget(bar)
        bar.set_symbols(entries)
        orders = []
        bar.symbols_reordered.connect(orders.append)
        return bar, orders

    def test_forward_drag_lands_after_target(self, qtbot):
        bar, orders = self._bar(qtbot, _stk(4))
        bar._on_drop("S00", "S02")
        assert orders[-1] == ["S01", "S02", "S00", "S03"]

    def test_drag_to_last_slot_reachable(self, qtbot):
        bar, orders = self._bar(qtbot, _stk(4))
        bar._on_drop("S00", "S03")
        assert orders[-1] == ["S01", "S02", "S03", "S00"]

    def test_adjacent_forward_swap_not_a_noop(self, qtbot):
        bar, orders = self._bar(qtbot, _stk(3))
        bar._on_drop("S00", "S01")
        assert orders[-1] == ["S01", "S00", "S02"]

    def test_backward_drag_lands_before_target(self, qtbot):
        bar, orders = self._bar(qtbot, _stk(4))
        bar._on_drop("S03", "S00")
        assert orders[-1] == ["S03", "S00", "S01", "S02"]

    def test_reorder_within_group_across_mixed_list(self, qtbot):
        # STK chips separated by FUT entries in the flat list still get
        # take-position semantics within their displayed group.
        bar, orders = self._bar(
            qtbot, [("S00", "STK"), ("F00", "FUT"), ("S01", "STK"),
                    ("S02", "STK")])
        bar._on_drop("S00", "S02")
        assert orders[-1] == ["F00", "S01", "S02", "S00"]

    def test_unknown_symbols_ignored(self, qtbot):
        bar, orders = self._bar(qtbot, _stk(3))
        bar._on_drop("NOPE", "S01")
        bar._on_drop("S01", "NOPE")
        assert orders == []


def test_set_symbols_frozen_during_chip_drag(qtbot):
    # A rebuild landing while a chip drag is in flight must NOT destroy
    # the chips (the native drag loop would deliver dragging updates to
    # freed widgets — segfault); the newest deferred list applies when
    # the drag ends.
    from src.ui.widgets.order_panel_parts import SymbolChipBar
    bar = SymbolChipBar(columns=5)
    qtbot.addWidget(bar)
    bar.set_symbols([("A", "STK"), ("B", "STK")])
    buttons_before = dict(bar._buttons)
    bar._begin_chip_drag()
    bar.set_symbols([("C", "STK")])
    assert bar._buttons == buttons_before      # untouched mid-drag
    bar.set_symbols([("D", "STK"), ("E", "STK")])
    bar.remove("A")                            # host removal also defers
    assert bar._buttons == buttons_before
    bar._end_chip_drag()
    assert set(bar._buttons) == {"D", "E"}     # newest deferred wins
    # Balanced begin/end with nothing pending changes nothing.
    bar._begin_chip_drag()
    bar._end_chip_drag()
    assert set(bar._buttons) == {"D", "E"}
