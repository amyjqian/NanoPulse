"""Positions-view search field: filters both the flat account-grouped
model and the per-contract summary model by account id, account alias,
or symbol (case-insensitive substring)."""
from __future__ import annotations

import pytest

from src.ui.widgets.account_position_model import (
    AccountPositionModel,
    ROW_TYPE_HEADER,
    ROW_TYPE_POSITION,
    RowTypeRole,
)
from src.ui.widgets.account_position_panel import AccountPositionPanel
from src.ui.widgets.positions_summary_model import (
    PositionsSummaryModel,
    S_COL_SYMBOL,
)
from src.ui.widgets.search_query import SearchQuery

_ALIASES = {"ACC1": "Momentum", "ACC2": "Swing"}


def _pos(account: str, con_id: int, symbol: str, **over) -> dict:
    base = {
        "account": account, "con_id": con_id, "symbol": symbol,
        "local_symbol": symbol, "sec_type": "STK", "exchange": "SMART",
        "currency": "USD", "multiplier": "", "position": 10.0,
        "avg_cost": 100.0, "market_price": 101.0, "market_value": 1010.0,
        "unrealized_pnl": 10.0, "realized_pnl": 0.0,
    }
    base.update(over)
    return base


_BATCH = [
    _pos("ACC1", 1, "AAPL"),
    _pos("ACC1", 2, "MSFT"),
    _pos("ACC2", 1, "AAPL"),
    _pos("ACC2", 3, "TSLA"),
]


def _flat_model() -> AccountPositionModel:
    m = AccountPositionModel(get_alias=lambda a: _ALIASES.get(a, ""))
    m.update_positions(_BATCH)
    return m


def _visible_rows(m: AccountPositionModel) -> list[tuple[str, str]]:
    """(row_type, account/symbol) per visible row."""
    out = []
    for row in range(m.rowCount()):
        idx = m.index(row, 0)
        rt = idx.data(RowTypeRole)
        rd = m._rows[row]
        label = rd["account"] if rt == ROW_TYPE_HEADER \
            else rd.get("symbol", "")
        out.append((rt, label))
    return out


class TestFlatModelSearch:
    def test_no_search_shows_everything(self):
        m = _flat_model()
        assert len(_visible_rows(m)) == 6  # 2 headers + 4 positions

    def test_symbol_search_keeps_matching_rows_only(self):
        m = _flat_model()
        m.set_search_text("aapl")
        rows = _visible_rows(m)
        assert (ROW_TYPE_POSITION, "AAPL") in rows
        assert all(sym == "AAPL" for rt, sym in rows
                   if rt == ROW_TYPE_POSITION)
        # Both accounts hold AAPL → both headers survive.
        headers = [a for rt, a in rows if rt == ROW_TYPE_HEADER]
        assert headers == ["ACC1", "ACC2"]

    def test_alias_search_keeps_whole_account(self):
        m = _flat_model()
        m.set_search_text("momen")   # alias of ACC1
        rows = _visible_rows(m)
        assert (ROW_TYPE_HEADER, "ACC1") in rows
        assert (ROW_TYPE_POSITION, "AAPL") in rows
        assert (ROW_TYPE_POSITION, "MSFT") in rows
        assert all(a == "ACC1" for rt, a in rows if rt == ROW_TYPE_HEADER)

    def test_account_id_search_matches(self):
        m = _flat_model()
        m.set_search_text("acc2")
        headers = [a for rt, a in _visible_rows(m) if rt == ROW_TYPE_HEADER]
        assert headers == ["ACC2"]

    def test_clearing_search_restores_all_rows(self):
        m = _flat_model()
        m.set_search_text("tsla")
        assert len(_visible_rows(m)) < 6
        m.set_search_text("")
        assert len(_visible_rows(m)) == 6

    def test_no_match_shows_nothing(self):
        m = _flat_model()
        m.set_search_text("zzz")
        assert m.rowCount() == 0

    def test_positionless_account_header_hidden_unless_name_matches(self):
        m = _flat_model()
        m.set_account_data("ACC3", net_liq=5000.0)
        m.set_search_text("aapl")
        headers = [a for rt, a in _visible_rows(m) if rt == ROW_TYPE_HEADER]
        assert "ACC3" not in headers
        m.set_search_text("acc3")
        headers = [a for rt, a in _visible_rows(m) if rt == ROW_TYPE_HEADER]
        assert headers == ["ACC3"]

    def test_not_hides_matching_positions_only(self):
        m = _flat_model()
        m.set_search_text("not aapl")
        rows = _visible_rows(m)
        assert (ROW_TYPE_POSITION, "AAPL") not in rows
        assert (ROW_TYPE_POSITION, "MSFT") in rows
        assert (ROW_TYPE_POSITION, "TSLA") in rows
        headers = [a for rt, a in rows if rt == ROW_TYPE_HEADER]
        assert headers == ["ACC1", "ACC2"]

    def test_dash_is_the_same_as_not(self):
        m = _flat_model()
        m.set_search_text("-aapl")
        a = _visible_rows(m)
        m.set_search_text("not aapl")
        assert _visible_rows(m) == a

    def test_not_on_an_alias_drops_the_whole_account(self):
        m = _flat_model()
        m.set_search_text("not momen")      # ACC1's alias
        rows = _visible_rows(m)
        assert [a for rt, a in rows if rt == ROW_TYPE_HEADER] == ["ACC2"]
        assert (ROW_TYPE_POSITION, "MSFT") not in rows

    def test_mixed_include_and_exclude(self):
        m = _flat_model()
        m.set_search_text("acc1 not aapl")
        rows = _visible_rows(m)
        assert rows == [(ROW_TYPE_HEADER, "ACC1"),
                        (ROW_TYPE_POSITION, "MSFT")]

    def test_positionless_header_survives_an_exclusion_that_misses_it(self):
        m = _flat_model()
        m.set_account_data("ACC3", net_liq=5000.0)
        m.set_search_text("not aapl")
        headers = [a for rt, a in _visible_rows(m) if rt == ROW_TYPE_HEADER]
        assert "ACC3" in headers
        m.set_search_text("not acc3")
        headers = [a for rt, a in _visible_rows(m) if rt == ROW_TYPE_HEADER]
        assert "ACC3" not in headers


class TestSummaryModelSearch:
    def _model(self) -> PositionsSummaryModel:
        m = PositionsSummaryModel(get_alias=lambda a: _ALIASES.get(a, ""))
        m.update_positions(_BATCH)
        return m

    def _symbols(self, m: PositionsSummaryModel) -> list[str]:
        return [m.row_data(r)["symbol"] for r in range(len(m._order))]

    def test_symbol_search_filters_aggregates(self):
        m = self._model()
        m.set_search_text("aapl")
        assert self._symbols(m) == ["AAPL"]
        # Both accounts' AAPL folded in.
        assert m.row_data(0)["position"] == 20.0

    def test_alias_search_folds_only_that_account(self):
        m = self._model()
        m.set_search_text("momen")   # ACC1
        assert set(self._symbols(m)) == {"AAPL", "MSFT"}
        aapl = next(a for a in (m.row_data(r) for r in range(len(m._order)))
                    if a["symbol"] == "AAPL")
        assert aapl["position"] == 10.0  # ACC1 only

    def test_not_drops_only_that_aggregate(self):
        m = self._model()
        m.set_search_text("not aapl")
        assert set(self._symbols(m)) == {"MSFT", "TSLA"}

    def test_not_on_an_alias_folds_the_other_account_only(self):
        m = self._model()
        m.set_search_text("not swing")   # ACC2
        assert set(self._symbols(m)) == {"AAPL", "MSFT"}
        aapl = next(a for a in (m.row_data(r) for r in range(len(m._order)))
                    if a["symbol"] == "AAPL")
        assert aapl["position"] == 10.0  # ACC1 only

    def test_clearing_restores(self):
        m = self._model()
        m.set_search_text("tsla")
        assert self._symbols(m) == ["TSLA"]
        m.set_search_text("")
        assert set(self._symbols(m)) == {"AAPL", "MSFT", "TSLA"}


class TestPanelWiring:
    def test_search_field_fans_out_to_both_models(self, qtbot):
        panel = AccountPositionPanel()
        qtbot.addWidget(panel)
        panel.update_positions(_BATCH)
        panel._search_field.setText("AAPL")
        assert panel._model._search == SearchQuery(("aapl",), ())
        assert panel._summary_model._search == SearchQuery(("aapl",), ())
        pos_rows = [r for r in panel._model._rows
                    if r.get("_row_type") == ROW_TYPE_POSITION]
        assert {r["symbol"] for r in pos_rows} == {"AAPL"}

    def test_escape_clears_field(self, qtbot):
        from PySide6.QtCore import Qt

        panel = AccountPositionPanel()
        qtbot.addWidget(panel)
        panel._search_field.setText("AAPL")
        qtbot.keyClick(panel._search_field, Qt.Key.Key_Escape)
        assert panel._search_field.text() == ""
        assert not panel._model._search
