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
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from src.ui.widgets.table_theme import IsFooterRowRole, SortValueRole
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


def _click(panel: TradesPanel, kind: str, col: int, *,
           shift: bool = False) -> None:
    """Click a caption.  ``shift=True`` stands in for the ⇧-press the
    real header sees — ``sectionClicked`` carries no modifiers, so the
    panel latches the press separately (see ``TradesPanel.eventFilter``)."""
    table = panel._flat_table if kind == "flat" else panel._summary_table
    panel._additive_click = shift
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
    assert panel._flat_proxy.sortSpecs() == []


def test_flat_new_column_restarts_at_descending(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel.add_fill(_fill("e1", "AAA", "BOT", 1, 9.5, con_id=1, second=1))
    panel.add_fill(_fill("e2", "BBB", "BOT", 1, 300.0, con_id=2, second=2))

    _click(panel, "flat", COL_PRICE)     # price descending
    _click(panel, "flat", COL_SYMBOL)    # switch column → symbol descending
    assert _flat_symbols(panel) == ["BBB", "AAA"]
    assert panel._flat_proxy.sortSpecs() == [
        (COL_SYMBOL, Qt.SortOrder.DescendingOrder)]


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

    def bands_pinned():
        # The section headers and the two TOTAL rows all keep their
        # spots under any sort.
        return all(bool(proxy.index(r, 0).data(IsFooterRowRole))
                   for r in (0, 3, 4, 7))

    stacked = ["TRADE LEVEL", "AAA", "BBB", "TOTAL",
               "ACCOUNT LEVEL", "AAA", "BBB", "TOTAL"]
    assert rows() == stacked

    _click(panel, "summary", S_COL_NET_TOTAL)   # descending: +10 first
    assert rows() == stacked
    assert bands_pinned()

    _click(panel, "summary", S_COL_NET_TOTAL)   # ascending: −10 first
    # The account section's P&L cells are blank (realized-only), so its
    # rows keep source order under a P&L-column sort.
    assert rows() == ["TRADE LEVEL", "BBB", "AAA", "TOTAL",
                      "ACCOUNT LEVEL", "AAA", "BBB", "TOTAL"]
    assert bands_pinned()

    _click(panel, "summary", S_COL_NET_TOTAL)   # cleared: insertion order
    assert rows() == stacked
    assert bands_pinned()


def _caption(panel: TradesPanel, kind: str, col: int) -> str:
    proxy = panel._flat_proxy if kind == "flat" else panel._summary_proxy
    return proxy.headerData(col, Qt.Orientation.Horizontal)


def test_caption_carries_the_sort_arrow(qtbot):
    """The sorted column's caption grows a ▼/▲ and loses it again on the
    clear click — Qt's own indicator never renders through the
    style-sheeted header, so the glyph IS the affordance."""
    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel.add_fill(_fill("e1", "AAA", "BOT", 1, 9.5, con_id=1, second=1))
    panel.add_fill(_fill("e2", "BBB", "BOT", 1, 300.0, con_id=2, second=2))

    bare = _caption(panel, "flat", COL_PRICE)
    assert "\u25bc" not in bare and "\u25b2" not in bare

    _click(panel, "flat", COL_PRICE)
    assert _caption(panel, "flat", COL_PRICE) == f"{bare} \u25bc"
    # Only the sorted column is decorated.
    assert "\u25bc" not in _caption(panel, "flat", COL_SYMBOL)

    _click(panel, "flat", COL_PRICE)
    assert _caption(panel, "flat", COL_PRICE) == f"{bare} \u25b2"

    _click(panel, "flat", COL_PRICE)
    assert _caption(panel, "flat", COL_PRICE) == bare


def test_summary_caption_carries_the_sort_arrow(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel.add_fill(_fill("a1", "AAA", "BOT", 1, 100.0, con_id=1, second=1))
    panel.add_fill(_fill("a2", "AAA", "SLD", 1, 110.0, con_id=1, second=2))

    bare = _caption(panel, "summary", S_COL_NET_TOTAL)
    _click(panel, "summary", S_COL_NET_TOTAL)
    assert _caption(panel, "summary", S_COL_NET_TOTAL) == f"{bare} \u25bc"
    _click(panel, "summary", S_COL_NET_TOTAL)
    assert _caption(panel, "summary", S_COL_NET_TOTAL) == f"{bare} \u25b2"
    _click(panel, "summary", S_COL_NET_TOTAL)
    assert _caption(panel, "summary", S_COL_NET_TOTAL) == bare


# ── Multi-column sort (⇧-click adds a level) ────────────────────────────────

def _flat_pairs(panel: TradesPanel) -> list[tuple[str, float]]:
    proxy = panel._flat_proxy
    return [(proxy.index(r, COL_SYMBOL).data(),
             proxy.index(r, COL_PRICE).data(SortValueRole))
            for r in range(proxy.rowCount())]


def _two_by_two(panel: TradesPanel) -> None:
    """Two symbols × two prices, so a symbol sort leaves ties for a
    second column to break."""
    panel.add_fill(_fill("e1", "AAA", "BOT", 1, 9.5, con_id=1, second=1))
    panel.add_fill(_fill("e2", "AAA", "BOT", 1, 300.0, con_id=1, second=2))
    panel.add_fill(_fill("e3", "BBB", "BOT", 1, 25.0, con_id=2, second=3))
    panel.add_fill(_fill("e4", "BBB", "BOT", 1, 100.0, con_id=2, second=4))


def test_shift_click_adds_a_second_sort_level(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    _two_by_two(panel)

    _click(panel, "flat", COL_SYMBOL)                  # symbol descending
    _click(panel, "flat", COL_PRICE, shift=True)       # + price descending
    assert panel._flat_proxy.sortSpecs() == [
        (COL_SYMBOL, Qt.SortOrder.DescendingOrder),
        (COL_PRICE, Qt.SortOrder.DescendingOrder)]
    assert _flat_pairs(panel) == [
        ("BBB", 100.0), ("BBB", 25.0), ("AAA", 300.0), ("AAA", 9.5)]

    # The secondary level flips on its own, without disturbing the primary.
    _click(panel, "flat", COL_PRICE, shift=True)       # price ascending
    assert _flat_pairs(panel) == [
        ("BBB", 25.0), ("BBB", 100.0), ("AAA", 9.5), ("AAA", 300.0)]

    # …and the primary flips without dropping the secondary — the case
    # where Qt's own inversion has to be pre-cancelled for level 2.
    _click(panel, "flat", COL_SYMBOL, shift=True)      # symbol ascending
    assert _flat_pairs(panel) == [
        ("AAA", 9.5), ("AAA", 300.0), ("BBB", 25.0), ("BBB", 100.0)]


def test_shift_click_third_time_drops_that_level(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    _two_by_two(panel)

    _click(panel, "flat", COL_SYMBOL)
    _click(panel, "flat", COL_PRICE, shift=True)
    _click(panel, "flat", COL_PRICE, shift=True)
    _click(panel, "flat", COL_PRICE, shift=True)       # 3rd ⇧-click → gone
    assert panel._flat_proxy.sortSpecs() == [
        (COL_SYMBOL, Qt.SortOrder.DescendingOrder)]


def test_plain_click_collapses_a_multi_column_sort(qtbot):
    """The one-off re-sort has to stay one click: a plain click drops
    every other level rather than appending to them."""
    panel = TradesPanel()
    qtbot.addWidget(panel)
    _two_by_two(panel)

    _click(panel, "flat", COL_SYMBOL)
    _click(panel, "flat", COL_PRICE, shift=True)
    _click(panel, "flat", COL_PRICE)                   # plain → price alone
    assert panel._flat_proxy.sortSpecs() == [
        (COL_PRICE, Qt.SortOrder.DescendingOrder)]
    assert _flat_pairs(panel) == [
        ("AAA", 300.0), ("BBB", 100.0), ("BBB", 25.0), ("AAA", 9.5)]


def test_secondary_caption_carries_its_rank(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    _two_by_two(panel)
    bare_sym = _caption(panel, "flat", COL_SYMBOL)
    bare_px = _caption(panel, "flat", COL_PRICE)

    _click(panel, "flat", COL_SYMBOL)
    _click(panel, "flat", COL_PRICE, shift=True)
    # Primary keeps the plain arrow; level 2 carries the rank digit.
    assert _caption(panel, "flat", COL_SYMBOL) == f"{bare_sym} \u25bc"
    assert _caption(panel, "flat", COL_PRICE) == f"{bare_px} \u25bc2"


def test_summary_multi_column_sort_keeps_bands_pinned(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel.add_fill(_fill("a1", "AAA", "BOT", 1, 100.0, con_id=1, second=1))
    panel.add_fill(_fill("a2", "AAA", "SLD", 1, 110.0, con_id=1, second=2))
    panel.add_fill(_fill("b1", "BBB", "BOT", 1, 100.0, con_id=2, second=3))
    panel.add_fill(_fill("b2", "BBB", "SLD", 1, 90.0, con_id=2, second=4))
    proxy = panel._summary_proxy

    _click(panel, "summary", S_COL_NET_TOTAL)
    _click(panel, "summary", S_COL_SYMBOL, shift=True)
    assert proxy.sortSpecs() == [
        (S_COL_NET_TOTAL, Qt.SortOrder.DescendingOrder),
        (S_COL_SYMBOL, Qt.SortOrder.DescendingOrder)]
    # Section headers and both TOTAL footers stay in their slots.
    assert all(bool(proxy.index(r, 0).data(IsFooterRowRole))
               for r in (0, 3, 4, 7))


def test_real_shift_click_reaches_the_panel(qtbot):
    """End-to-end on the modifier latch.

    The rest of the ⇧ tests set the flag directly, so they'd pass even if
    no real press ever set it — this one drives the actual header.  The
    press lands on the header's VIEWPORT, which is what the filter has to
    be installed on.
    """
    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    _two_by_two(panel)

    hdr = panel._flat_table.horizontalHeader()

    def click(col, modifier):
        x = (hdr.sectionViewportPosition(col) + hdr.sectionSize(col) // 2)
        QTest.mouseClick(hdr.viewport(), Qt.MouseButton.LeftButton,
                         modifier, QPoint(x, hdr.height() // 2))

    click(COL_SYMBOL, Qt.KeyboardModifier.NoModifier)
    click(COL_PRICE, Qt.KeyboardModifier.ShiftModifier)
    assert panel._flat_proxy.sortSpecs() == [
        (COL_SYMBOL, Qt.SortOrder.DescendingOrder),
        (COL_PRICE, Qt.SortOrder.DescendingOrder)]

    # A plain click after it still collapses to the one column.
    click(COL_PRICE, Qt.KeyboardModifier.NoModifier)
    assert panel._flat_proxy.sortSpecs() == [
        (COL_PRICE, Qt.SortOrder.DescendingOrder)]
