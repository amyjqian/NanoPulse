"""Positions views follow the Account List panel's checked accounts.

Both the flat account-grouped model and the per-contract Summary model
take the same tri-state selection: ``None`` clears the filter (show
all), an EMPTY set shows nothing, ``{...}`` shows only those accounts.

The empty case is what the Account List emits when every account is
unchecked (the "None" pill).  The Summary model used to collapse it to
"show all", so switching to None left every account's positions — and a
TOTAL line summing all of them — on screen while the flat view beside
it went blank.
"""
from __future__ import annotations

from src.ui.widgets.account_position_model import (
    AccountPositionModel,
    ROW_TYPE_HEADER,
    ROW_TYPE_POSITION,
)
from src.ui.widgets.account_position_panel import AccountPositionPanel
from src.ui.widgets.positions_summary_model import PositionsSummaryModel


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


def _flat_rows(m: AccountPositionModel) -> tuple[set[str], set[str]]:
    """(header accounts, position symbols) currently rendered."""
    headers = {r["account"] for r in m._rows
               if r.get("_row_type") == ROW_TYPE_HEADER}
    symbols = {r["symbol"] for r in m._rows
               if r.get("_row_type") == ROW_TYPE_POSITION}
    return headers, symbols


def _summary_symbols(m: PositionsSummaryModel) -> set[str]:
    return {m.row_data(r)["symbol"] for r in range(len(m._order))}


class TestFlatModel:
    def _model(self) -> AccountPositionModel:
        m = AccountPositionModel()
        m.update_positions(_BATCH)
        return m

    def test_no_filter_shows_every_account(self):
        m = self._model()
        headers, symbols = _flat_rows(m)
        assert headers == {"ACC1", "ACC2"}
        assert symbols == {"AAPL", "MSFT", "TSLA"}

    def test_selection_keeps_only_those_accounts(self):
        m = self._model()
        m.set_selected_accounts({"ACC2"})
        headers, symbols = _flat_rows(m)
        assert headers == {"ACC2"}
        assert symbols == {"AAPL", "TSLA"}

    def test_empty_selection_shows_nothing(self):
        m = self._model()
        m.set_selected_accounts(set())
        assert _flat_rows(m) == (set(), set())
        assert m.rowCount() == 0

    def test_none_clears_the_filter(self):
        m = self._model()
        m.set_selected_accounts(set())
        m.set_selected_accounts(None)
        headers, _symbols = _flat_rows(m)
        assert headers == {"ACC1", "ACC2"}


class TestSummaryModel:
    def _model(self) -> PositionsSummaryModel:
        m = PositionsSummaryModel()
        m.update_positions(_BATCH)
        return m

    def test_no_filter_folds_every_account(self):
        m = self._model()
        assert _summary_symbols(m) == {"AAPL", "MSFT", "TSLA"}
        aapl = next(m.row_data(r) for r in range(len(m._order))
                    if m.row_data(r)["symbol"] == "AAPL")
        assert aapl["position"] == 20.0      # both accounts' AAPL

    def test_selection_folds_only_those_accounts(self):
        m = self._model()
        m.set_selected_accounts({"ACC1"})
        assert _summary_symbols(m) == {"AAPL", "MSFT"}
        aapl = next(m.row_data(r) for r in range(len(m._order))
                    if m.row_data(r)["symbol"] == "AAPL")
        assert aapl["position"] == 10.0      # ACC1 only

    def test_empty_selection_shows_nothing(self):
        """The regression: an empty set used to be read as "show all"."""
        m = self._model()
        m.set_selected_accounts({"ACC1"})
        m.set_selected_accounts(set())
        assert _summary_symbols(m) == set()
        assert m.rowCount() == 0
        # A list — what the Account List panel actually emits — too.
        m.set_selected_accounts({"ACC1"})
        m.set_selected_accounts([])
        assert m.rowCount() == 0

    def test_none_clears_the_filter(self):
        m = self._model()
        m.set_selected_accounts(set())
        m.set_selected_accounts(None)
        assert _summary_symbols(m) == {"AAPL", "MSFT", "TSLA"}


def test_panel_forwards_to_both_models(qtbot):
    panel = AccountPositionPanel()
    qtbot.addWidget(panel)
    # The panel parks summary batches while the flat view is showing
    # and folds them on switch — show the Summary view so both models
    # actually hold the batch.
    panel._on_summary_toggled(True)
    panel.update_positions(_BATCH)

    panel.set_selected_accounts({"ACC2"})
    headers, _ = _flat_rows(panel._model)
    assert headers == {"ACC2"}
    assert _summary_symbols(panel._summary_model) == {"AAPL", "TSLA"}

    # "None" pill → the panel receives an empty list; both views empty.
    panel.set_selected_accounts([])
    assert panel._model.rowCount() == 0
    assert panel._summary_model.rowCount() == 0

    panel.set_selected_accounts(None)
    headers, _ = _flat_rows(panel._model)
    assert headers == {"ACC1", "ACC2"}
    assert _summary_symbols(panel._summary_model) == {"AAPL", "MSFT", "TSLA"}
