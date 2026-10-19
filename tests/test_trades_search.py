"""Trades search box — ``SearchQuery`` syntax on the flat proxy and on
the summary model (both TRADE LEVEL and ACCOUNT LEVEL fold the same
subset).  A typed time still becomes a window, never a text term."""
from __future__ import annotations

from datetime import date

import pytest
from PySide6.QtCore import Qt

import src.ui.widgets.trades_filter_proxy as tfp
import src.ui.widgets.trades_summary_model as tsm
from src.ui.widgets.trades_filter_proxy import TradesFilterProxy
from src.ui.widgets.trades_model import TradesModel
from src.ui.widgets.trades_summary_model import TradesSummaryModel

_DAY = "2026-05-29T10:00:00"


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    pinned = date(2026, 5, 29)
    monkeypatch.setattr(tfp, "today_et", lambda: pinned)
    monkeypatch.setattr(tsm, "today_et", lambda: pinned)


def _fill(exec_id, symbol, *, side="BOT", account="ACC1", con_id=None,
          t=_DAY, exchange="NASDAQ"):
    return {
        "exec_id": exec_id, "con_id": con_id or hash(symbol) % 1000,
        "symbol": symbol, "local_symbol": symbol, "sec_type": "STK",
        "side": side, "shares": 10, "price": 100.0, "exchange": exchange,
        "account": account, "account_id": account, "time_sort": t,
    }


def _symbols(proxy) -> list[str]:
    out = []
    for r in range(proxy.rowCount()):
        src = proxy.mapToSource(proxy.index(r, 0))
        out.append(proxy.sourceModel().row_data(src.row())["symbol"])
    return out


class TestFlatProxy:
    def _proxy(self):
        model = TradesModel()
        proxy = TradesFilterProxy(is_summary=False)
        proxy.setSourceModel(model)
        model.add_fill(_fill("e1", "NVDA"))
        model.add_fill(_fill("e2", "SGOV", side="SLD"))
        model.add_fill(_fill("e3", "QQQ", account="ACC2"))
        return proxy

    def test_plain_term_unchanged(self, qtbot):
        p = self._proxy()
        p.setSearchText("nvda")
        assert _symbols(p) == ["NVDA"]

    def test_not_hides_the_symbol(self, qtbot):
        p = self._proxy()
        p.setSearchText("not sgov")
        assert sorted(_symbols(p)) == ["NVDA", "QQQ"]
        p.setSearchText("-sgov")
        assert sorted(_symbols(p)) == ["NVDA", "QQQ"]

    def test_not_on_any_searched_field(self, qtbot):
        p = self._proxy()
        p.setSearchText("not sld")            # side
        assert sorted(_symbols(p)) == ["NVDA", "QQQ"]
        p.setSearchText("not acc2")           # account
        assert sorted(_symbols(p)) == ["NVDA", "SGOV"]

    def test_mixed(self, qtbot):
        p = self._proxy()
        p.setSearchText("acc1 not sgov")
        assert _symbols(p) == ["NVDA"]

    def test_clearing_restores(self, qtbot):
        p = self._proxy()
        p.setSearchText("not sgov")
        p.setSearchText("")
        assert p.rowCount() == 3

    def test_a_time_is_still_a_window_not_text(self, qtbot):
        p = self._proxy()
        p.setSearchText("12:00")
        assert p.rowCount() == 0              # all fills are at 10:00
        assert p.effectiveWindow().key == "search"
        p.setSearchText("not sgov")
        assert p.effectiveWindow().key != "search"


class TestSummaryModel:
    def _model(self):
        m = TradesSummaryModel()
        m.add_fill(_fill("e1", "NVDA"))
        m.add_fill(_fill("e2", "SGOV", side="SLD"))
        m.add_fill(_fill("e3", "QQQ", account="ACC2"))
        return m

    def _labels(self, m):
        return [m.data(m.index(r, 0), Qt.ItemDataRole.DisplayRole)
                for r in range(m.rowCount())]

    def test_not_drops_the_symbol_from_both_sections(self):
        m = self._model()
        m.set_search_text("not sgov")
        labels = self._labels(m)
        assert "SGOV" not in labels
        assert labels.count("NVDA") == 2 and labels.count("QQQ") == 2
        assert m.visible_fill_count() == 2

    def test_not_on_an_account_field(self):
        m = self._model()
        m.set_search_text("not acc2")
        labels = self._labels(m)
        assert "QQQ" not in labels
        assert m.visible_fill_count() == 2

    def test_clearing_restores(self):
        m = self._model()
        m.set_search_text("-sgov")
        m.set_search_text("")
        assert m.visible_fill_count() == 3
