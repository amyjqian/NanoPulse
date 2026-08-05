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
        removed = []
        monkeypatch.setattr(btv.config_manager,
                            "remove_stock_contract_if_unused",
                            removed.append)
        monkeypatch.setattr(btv, "_sec_type_for",
                            lambda s: "FUT" if s.startswith("F") else "STK")
        v = btv.BookTraderView(active_threads={})
        qtbot.addWidget(v)
        v._removed_contracts = removed
        return v

    def test_full_stk_group_replaces_last_chip(self, view):
        view._symbols = [f"S{i:02d}" for i in range(15)] + ["F00"]
        view._add_symbol("NEW")
        stk = [s for s in view._symbols if not s.startswith("F")]
        assert len(stk) == 15
        assert stk[-1] == "NEW"                 # takes row 3, column 5
        assert "S14" not in view._symbols       # previous last dropped
        assert view._removed_contracts == ["S14"]
        assert view._active_symbol == "NEW"
        # FUT membership untouched.
        assert "F00" in view._symbols

    def test_below_cap_appends_normally(self, view):
        view._symbols = [f"S{i:02d}" for i in range(14)]
        view._add_symbol("NEW")
        assert len(view._symbols) == 15
        assert view._removed_contracts == []

    def test_fut_add_never_evicts_stk(self, view):
        view._symbols = [f"S{i:02d}" for i in range(15)]
        view._add_symbol("F99")
        assert len(view._symbols) == 16         # future appended, no eviction
        assert view._removed_contracts == []
