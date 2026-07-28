"""Trades summary view — hiding a symbol row updates the totals footer
*and* the control-bar trade count.

The count in the control bar is sourced from the flat proxy when the
flat view is active and from the summary model's visible fill count when
the Summary toggle is on, so hiding/restoring a summary row moves the
counter in lock-step with the totals footer.
"""
from __future__ import annotations

from src.ui.widgets.trades_summary_model import (
    TradesSummaryModel,
    S_COL_NET_TOTAL,
)
from src.ui.widgets.trades_panel import TradesPanel


# All fills share one ET calendar date so the default "today" window
# (days_back=1, anchor falls back to the newest fill date) keeps them
# regardless of when the suite runs.
_DAY = "2026-05-29T10:00:00"


def _fill(exec_id, symbol, side, shares, price, *, con_id):
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
        "time_sort": _DAY,
    }


def test_match_buy_sell_trims_and_recomputes():
    m = TradesSummaryModel()
    # 10 bought @100, 4 sold @110 → net long 6, raw net total = -560.
    m.add_fill(_fill("e1", "AAA", "BOT", 10, 100.0, con_id=1))
    m.add_fill(_fill("e2", "AAA", "SLD", 4, 110.0, con_id=1))
    key = m.row_key(0)

    assert m.can_match(key) is True
    assert m._net_total(m._effective_agg(key)) == -560.0

    m.match_symbol(key)
    eff = m._effective_agg(key)
    # Larger (buy) leg trimmed to 4, avg buy price preserved (@100).
    assert eff["buys_qty"] == 4
    assert eff["sells_qty"] == 4
    # Matched round-trip: 4 sold @110 − 4 bought @100 = +40.
    assert m._net_total(eff) == 40.0
    assert m.is_matched(key) is True

    m.unmatch_symbol(key)
    assert m.is_matched(key) is False
    assert m._net_total(m._effective_agg(key)) == -560.0


def test_can_match_false_when_balanced_or_one_sided():
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "AAA", "BOT", 5, 100.0, con_id=1))
    m.add_fill(_fill("e2", "AAA", "SLD", 5, 100.0, con_id=1))
    m.add_fill(_fill("e3", "BBB", "BOT", 7, 200.0, con_id=2))  # one-sided

    aaa = m.row_key(0)
    bbb = m.row_key(1)
    assert m.can_match(aaa) is False   # balanced
    assert m.can_match(bbb) is False   # no sell leg
    # match_symbol is a no-op on an ineligible row.
    m.match_symbol(bbb)
    assert m.is_matched(bbb) is False


def test_match_reflected_in_totals():
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "AAA", "BOT", 10, 100.0, con_id=1))
    m.add_fill(_fill("e2", "AAA", "SLD", 4, 110.0, con_id=1))
    key = m.row_key(0)

    # Totals fold over effective aggs, so matching changes the footer.
    before = m._total_cell_text(S_COL_NET_TOTAL)
    m.match_symbol(key)
    after = m._total_cell_text(S_COL_NET_TOTAL)
    assert before != after
    assert after == "+40.00"


def test_visible_fill_count_excludes_hidden():
    m = TradesSummaryModel()
    m.add_fill(_fill("e1", "AAA", "BOT", 10, 100.0, con_id=1))
    m.add_fill(_fill("e2", "AAA", "SLD", 10, 101.0, con_id=1))
    m.add_fill(_fill("e3", "BBB", "BOT", 5, 200.0, con_id=2))
    assert m.visible_fill_count() == 3

    m.hide_symbol(m.row_key(0))  # hide AAA (2 fills)
    assert m.visible_fill_count() == 1

    m.restore_hidden()
    assert m.visible_fill_count() == 3


def test_hide_multiple_selected_summary_rows(qtbot):
    from PySide6.QtCore import QItemSelectionModel

    panel = TradesPanel()
    qtbot.addWidget(panel)
    bar = panel.control_bar
    panel.add_fill(_fill("e1", "AAA", "BOT", 10, 100.0, con_id=1))
    panel.add_fill(_fill("e2", "BBB", "BOT", 5, 200.0, con_id=2))
    panel.add_fill(_fill("e3", "CCC", "BOT", 7, 300.0, con_id=3))
    bar.setSummary(True)

    table = panel._summary_table
    proxy = panel._summary_proxy
    model = panel._summary_model

    # Select the AAA (row 0) and BBB (row 1) rows.
    sm = table.selectionModel()
    flag = (QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows)
    for r in (0, 1):
        sm.select(proxy.index(r, 0), flag)

    panel._hide_selected_summary_rows()

    remaining = {model.row_data(r)["symbol"] for r in range(len(model._order))}
    assert remaining == {"CCC"}
    assert model.hidden_count() == 2
    # Counter reflects only CCC's single fill now.
    assert "1 trd" in bar._trades_count_label.text()

    model.restore_hidden()
    assert model.hidden_count() == 0
    assert "3 trds" in bar._trades_count_label.text()


def test_hide_multiple_selected_flat_rows(qtbot):
    from PySide6.QtCore import QItemSelectionModel

    panel = TradesPanel()
    qtbot.addWidget(panel)
    bar = panel.control_bar
    panel.add_fill(_fill("e1", "AAA", "BOT", 10, 100.0, con_id=1))
    panel.add_fill(_fill("e2", "BBB", "BOT", 5, 200.0, con_id=2))
    panel.add_fill(_fill("e3", "CCC", "BOT", 7, 300.0, con_id=3))
    # Flat list view is the default page.

    table = panel._flat_table
    proxy = panel._flat_proxy
    assert proxy.rowCount() == 3
    assert "3 trds" in bar._trades_count_label.text()

    # Select the first two fill rows.
    sm = table.selectionModel()
    flag = (QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows)
    for r in (0, 1):
        sm.select(proxy.index(r, 0), flag)

    panel._hide_selected_flat_rows()
    assert proxy.rowCount() == 1
    assert proxy.hiddenCount() == 2
    assert "1 trd" in bar._trades_count_label.text()

    proxy.restoreHidden()
    assert proxy.rowCount() == 3
    assert proxy.hiddenCount() == 0
    assert "3 trds" in bar._trades_count_label.text()


def test_flat_hide_propagates_to_summary(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel.add_fill(_fill("e1", "AAA", "BOT", 10, 100.0, con_id=1))
    panel.add_fill(_fill("e2", "AAA", "BOT", 10, 102.0, con_id=1))
    panel.add_fill(_fill("e3", "BBB", "BOT", 5, 200.0, con_id=2))

    model = panel._summary_model

    def agg_for(sym):
        for r in range(len(model._order)):
            d = model.row_data(r)
            if d["symbol"] == sym:
                return d
        return None

    assert agg_for("AAA")["buys_qty"] == 20
    assert agg_for("AAA")["fill_count"] == 2

    # Hiding one AAA fill in the flat list shrinks AAA's aggregate.
    panel._flat_proxy.hideKeys(["e1"])
    assert agg_for("AAA")["buys_qty"] == 10
    assert agg_for("AAA")["fill_count"] == 1
    assert agg_for("BBB")["buys_qty"] == 5

    # Hiding the last AAA fill removes the whole AAA row from the summary.
    panel._flat_proxy.hideKeys(["e2"])
    assert agg_for("AAA") is None
    assert agg_for("BBB") is not None

    # Restore re-includes every hidden fill in both views.
    panel._flat_proxy.restoreHidden()
    assert agg_for("AAA")["buys_qty"] == 20
    assert agg_for("AAA")["fill_count"] == 2


def test_flat_hide_cleared_on_filter_change(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel.add_fill(_fill("e1", "AAA", "BOT", 10, 100.0, con_id=1))
    panel.add_fill(_fill("e2", "BBB", "BOT", 5, 200.0, con_id=2))

    panel._flat_proxy.hideKeys(["e1"])
    assert panel._flat_proxy.hiddenCount() == 1

    # A date-window change rebuilds the visible set — the hide is cleared
    # so it can't silently exclude rows from a different window.
    panel._flat_proxy.setDaysBack(3)
    assert panel._flat_proxy.hiddenCount() == 0


def test_summary_view_trade_count_tracks_hide(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    bar = panel.control_bar

    panel.add_fill(_fill("e1", "AAA", "BOT", 10, 100.0, con_id=1))
    panel.add_fill(_fill("e2", "AAA", "SLD", 10, 101.0, con_id=1))
    panel.add_fill(_fill("e3", "BBB", "BOT", 5, 200.0, con_id=2))

    # Flat view (default) counts every fill.
    assert "3 trds" in bar._trades_count_label.text()

    # Switch to summary view — same fill count while nothing is hidden.
    bar.setSummary(True)
    assert "3 trds" in bar._trades_count_label.text()

    # Hiding AAA (2 fills) drops the count to BBB's single fill.
    panel._summary_model.hide_symbol(panel._summary_model.row_key(0))
    assert "1 trd" in bar._trades_count_label.text()

    # Restoring brings the count back.
    panel._summary_model.restore_hidden()
    assert "3 trds" in bar._trades_count_label.text()

    # Toggling back to flat re-sources the count from the flat proxy.
    bar.setSummary(False)
    assert "3 trds" in bar._trades_count_label.text()
