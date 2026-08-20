"""Trades view follows the Account List panel's checked accounts.

Fills carry ``account_id`` (the raw IB id — ``account`` holds the display
alias, which can be renamed mid-session).  The flat view filters rows in
``TradesFilterProxy``; the summary re-aggregates from only the matching
fills because an aggregate row can span accounts.  Empty selection means
"show all", mirroring the positions view.
"""
from __future__ import annotations

import pytest
from datetime import date

from src.ui.widgets.trades_filter_proxy import TradesFilterProxy
from src.ui.widgets.trades_model import TradesModel
from src.ui.widgets.trades_panel import TradesPanel
from src.ui.widgets.trades_summary_model import TradesSummaryModel

_DAY = "2026-05-29T10:00:00"

@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    """The date filter is strict-today (trading-day lookback), so pin
    "today" to the fixture fills' date — otherwise every fill would fall
    outside the default 1-day window whenever the suite runs later."""
    from datetime import date

    import src.ui.widgets.trades_filter_proxy as tfp
    import src.ui.widgets.trades_summary_model as tsm
    pinned = date(2026, 5, 29)          # the Friday the fills are dated
    monkeypatch.setattr(tfp, "today_et", lambda: pinned)
    monkeypatch.setattr(tsm, "today_et", lambda: pinned)




def _fill(exec_id, account_id, *, symbol="AAA", side="BOT",
          shares=10, price=100.0, con_id=1):
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
        "account": f"alias-{account_id}",   # display alias
        "account_id": account_id,           # raw IB id — the filter key
        "time_sort": _DAY,
    }


class TestFlatProxy:
    def _proxy(self, qtbot) -> tuple[TradesFilterProxy, TradesModel]:
        model = TradesModel()
        proxy = TradesFilterProxy(is_summary=False)
        proxy.setSourceModel(model)
        model.add_fill(_fill("e1", "U111"))
        model.add_fill(_fill("e2", "U222"))
        model.add_fill(_fill("e3", "U111"))
        return proxy, model

    def test_filters_rows_by_account_id(self, qtbot):
        proxy, _model = self._proxy(qtbot)
        assert proxy.rowCount() == 3
        proxy.setSelectedAccounts({"U111"})
        assert proxy.rowCount() == 2
        proxy.setSelectedAccounts({"U222"})
        assert proxy.rowCount() == 1

    def test_empty_selection_shows_all(self, qtbot):
        proxy, _model = self._proxy(qtbot)
        proxy.setSelectedAccounts({"U111"})
        proxy.setSelectedAccounts(set())
        assert proxy.rowCount() == 3
        proxy.setSelectedAccounts({"U111"})
        proxy.setSelectedAccounts(None)
        assert proxy.rowCount() == 3

    def test_rows_without_account_id_stay_visible(self, qtbot):
        proxy, model = self._proxy(qtbot)
        legacy = _fill("e4", "")
        legacy.pop("account_id")
        model.add_fill(legacy)
        proxy.setSelectedAccounts({"U111"})
        assert proxy.rowCount() == 3  # 2 × U111 + the id-less row

    def test_selection_change_keeps_user_hidden_fills(self, qtbot):
        proxy, _model = self._proxy(qtbot)
        proxy.hideKeys(["e1"])
        assert proxy.rowCount() == 2
        proxy.setSelectedAccounts({"U111"})
        # e1 is U111 but stays hidden; only e3 shows.
        assert proxy.rowCount() == 1
        assert proxy.hiddenCount() == 1


class TestSummaryModel:
    def test_reaggregates_for_selected_accounts(self):
        m = TradesSummaryModel()
        m.add_fill(_fill("e1", "U111", shares=10))
        m.add_fill(_fill("e2", "U222", shares=4))
        # Row 0 is the "TRADE LEVEL" band; the symbol row sits below it.
        key = m.row_key(1)
        assert m._effective_agg(key)["buys_qty"] == 14

        m.set_selected_accounts({"U111"})
        assert m._effective_agg(m.row_key(1))["buys_qty"] == 10

        m.set_selected_accounts(set())
        assert m._effective_agg(m.row_key(1))["buys_qty"] == 14

    def test_symbol_row_drops_when_no_fill_matches(self):
        m = TradesSummaryModel()
        m.add_fill(_fill("e1", "U111", symbol="AAA", con_id=1))
        m.add_fill(_fill("e2", "U222", symbol="BBB", con_id=2))
        # Two symbol rows in each section + the four band rows (the two
        # section headers and the two TOTAL rows).
        assert m.rowCount() == 8
        m.set_selected_accounts({"U111"})
        assert m.rowCount() == 6  # AAA in each section + the bands


def test_panel_forwards_to_both_views(qtbot):
    panel = TradesPanel()
    qtbot.addWidget(panel)
    panel.add_fill(_fill("e1", "U111"))
    panel.add_fill(_fill("e2", "U222"))
    panel.set_selected_accounts({"U111"})
    assert panel._flat_proxy.rowCount() == 1
    assert panel._summary_model._effective_agg(
        panel._summary_model.row_key(1))["buys_qty"] == 10
    panel.set_selected_accounts(set())
    assert panel._flat_proxy.rowCount() == 2
