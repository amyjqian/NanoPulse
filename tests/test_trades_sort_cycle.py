"""Header-click sorting on the Trades views.

Clicking a column caption cycles descending → ascending → unsorted
(source order).  Sorting compares raw values (SortValueRole), so numeric
columns order 9.5 < 10 < 300 instead of lexicographically, and the
summary's P&L totals footer stays pinned to the bottom whatever the
active sort.
"""
from __future__ import annotations

from datetime import date

import pytest
from PySide6.QtCore import Qt

from src.ui.widgets.table_theme import IsFooterRowRole
from src.ui.widgets.trades_model import COL_PRICE, COL_SYMBOL
from src.ui.widgets.trades_panel import TradesPanel
from src.ui.widgets.trades_summary_model import S_COL_NET_TOTAL, S_COL_SYMBOL


# All fills share one ET calendar date so the default "today" window
# keeps them regardless of when the suite runs.
_DAY = "2026-05-29T10:00:0{n}"


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    import src.ui.widgets.trades_filter_proxy as tfp
    import src.ui.widgets.trades_summary_model as tsm
    pinned = date(2026, 5, 29)
    monkeypatch.setattr(tfp, "today_et", lambda: pinned)
    monkeypatch.setattr(tsm, "today_et", lambda: pinned)


def _fill(exec_id, symbol, side, shares, price, *, con_id, second):
    return {
        "exec_id": exec_id,
        "con_id": con_id,
        "symbol": symbol,
        "local_symbol": symbol,
        "sec_type": "STK",
        "side": side,
        "shares": shares,
        "price": price,
        "exchange": "NASDAQ",
        "time_sort": _DAY.format(n=second),
    }


def _flat_symbols(panel: TradesPanel) -> list[str]:
    proxy = panel._flat_proxy
    return [proxy.index(r, COL_SYMBOL).data()
            for r in range(proxy.rowCount())]


def _click(panel: TradesPanel, kind: str, col: int) -> None:
    table = panel._flat_table if kind == "flat" else panel._summary_table
    table.horizontalHeader().sectionClicked.emit(col)


def test_flat_click_cycle_desc_asc_clear(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    # Prices chosen so lexicographic order ("9.50" > "300.00") differs
    # from numeric order — proves the sort uses raw values.
    panel.add_fill(_fill("e1", "AAA", "BOT", 1, 9.5, con_id=1, second=1))
    panel.add_fill(_fill("e2", "BBB", "BOT", 1, 300.0, con_id=2, second=2))
    panel.add_fill(_fill("e3", "CCC", "BOT", 1, 25.0, con_id=3, second=3))

    # Natural order: newest first.
    assert _flat_symbols(panel) == ["CCC", "BBB", "AAA"]

    _click(panel, "flat", COL_PRICE)     # 1st click → descending
    assert _flat_symbols(panel) == ["BBB", "CCC", "AAA"]

    _click(panel, "flat", COL_PRICE)     # 2nd click → ascending
    assert _flat_symbols(panel) == ["AAA", "CCC", "BBB"]

    _click(panel, "flat", COL_PRICE)     # 3rd click → unsorted
    assert _flat_symbols(panel) == ["CCC", "BBB", "AAA"]
    assert panel._sort_state["flat"] == (-1, None)


def test_flat_new_column_restarts_at_descending(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel.add_fill(_fill("e1", "AAA", "BOT", 1, 9.5, con_id=1, second=1))
    panel.add_fill(_fill("e2", "BBB", "BOT", 1, 300.0, con_id=2, second=2))

    _click(panel, "flat", COL_PRICE)     # price descending
    _click(panel, "flat", COL_SYMBOL)    # switch column → symbol descending
    assert _flat_symbols(panel) == ["BBB", "AAA"]
    assert panel._sort_state["flat"] == (
        COL_SYMBOL, Qt.SortOrder.DescendingOrder)


def test_summary_sort_keeps_footer_pinned(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    # AAA round trip +10, BBB round trip −10.
    panel.add_fill(_fill("a1", "AAA", "BOT", 1, 100.0, con_id=1, second=1))
    panel.add_fill(_fill("a2", "AAA", "SLD", 1, 110.0, con_id=1, second=2))
    panel.add_fill(_fill("b1", "BBB", "BOT", 1, 100.0, con_id=2, second=3))
    panel.add_fill(_fill("b2", "BBB", "SLD", 1, 90.0, con_id=2, second=4))

    proxy = panel._summary_proxy

    def rows():
        return [proxy.index(r, S_COL_SYMBOL).data()
                for r in range(proxy.rowCount())]

    def footer_last():
        last = proxy.index(proxy.rowCount() - 1, 0)
        return bool(last.data(IsFooterRowRole))

    assert rows() == ["AAA", "BBB", "P&L"]

    _click(panel, "summary", S_COL_NET_TOTAL)   # descending: +10 first
    assert rows() == ["AAA", "BBB", "P&L"]
    assert footer_last()

    _click(panel, "summary", S_COL_NET_TOTAL)   # ascending: −10 first
    assert rows() == ["BBB", "AAA", "P&L"]
    assert footer_last()

    _click(panel, "summary", S_COL_NET_TOTAL)   # cleared: insertion order
    assert rows() == ["AAA", "BBB", "P&L"]
    assert footer_last()
