"""Positions go stale, not blank, when a connection drops.

A position is a fact about the ACCOUNT — it survives the socket — so a
dropped connection must never remove the row: an empty Positions tab
reads as "you are flat", the most dangerous misreading possible while
carrying risk.  But the quote-derived columns freeze the instant the feed
stops, and a frozen number is indistinguishable on screen from a live
one.  So the row stays, quantity and average cost keep rendering, and
everything the feed fed renders as ``STALE_TEXT``.

Fills are deliberately NOT covered: a fill is a historical event that a
dropped connection does not un-happen, and IB replays only a limited
window, so discarding them would be unrecoverable.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from src.ui.widgets.account_position_model import (
    COL_AVG_PRICE,
    COL_DAY_PNL,
    COL_LAST_PRICE,
    COL_MARKET_VALUE,
    COL_POSITION,
    COL_REALIZED_PNL,
    COL_UNREALIZED_PNL,
    AccountPositionModel,
)
from src.ui.widgets.positions_staleness import STALE_TEXT
from src.ui.widgets.positions_summary_model import (
    S_COL_ACCOUNTS,
    S_COL_AVG_COST,
    S_COL_MARKET_PRICE,
    S_COL_MARKET_VALUE,
    S_COL_POSITION,
    S_COL_SYMBOL,
    S_COL_UNREALIZED_PNL,
    PositionsSummaryModel,
)

_LIVE_FLAT = (COL_DAY_PNL, COL_UNREALIZED_PNL, COL_LAST_PRICE,
              COL_REALIZED_PNL, COL_MARKET_VALUE)
_FACT_FLAT = (COL_POSITION, COL_AVG_PRICE)
_LIVE_SUM = (S_COL_MARKET_PRICE, S_COL_UNREALIZED_PNL, S_COL_MARKET_VALUE)


def _pos(label, account, con_id=1, symbol="MNQU6", position=5.0):
    return {
        "label": label, "account": account, "con_id": con_id,
        "symbol": symbol, "local_symbol": symbol, "sec_type": "FUT",
        "exchange": "CME", "currency": "USD", "multiplier": "2",
        "position": position, "avg_cost": 29362.03,
        "market_price": 29373.80, "market_value": 293738.0,
        "unrealized_pnl": 235.0, "realized_pnl": 1390.0,
        "daily_pnl": 104.45,
    }


def _txt(model, row, col):
    return model.data(model.index(row, col), Qt.ItemDataRole.DisplayRole)


def _tip(model, row, col):
    return model.data(model.index(row, col), Qt.ItemDataRole.ToolTipRole)


class TestFlatModel:
    def _model(self):
        m = AccountPositionModel()
        m.update_positions([_pos("J-ib", "U111"), _pos("Q-ib", "U222")])
        return m

    def _row_for(self, m, account):
        for r in range(m.rowCount()):
            rd = m._rows[r]
            if rd.get("account") == account and "con_id" in rd:
                return r
        raise AssertionError(f"no position row for {account}")

    def test_rows_survive_a_disconnect(self):
        """The row must not vanish — that would read as flat."""
        m = self._model()
        before = m.rowCount()
        m.set_stale_labels({"J-ib"})
        assert m.rowCount() == before
        assert self._row_for(m, "U111") is not None

    def test_live_columns_blank_on_the_stale_connection(self):
        m = self._model()
        m.set_stale_labels({"J-ib"})
        r = self._row_for(m, "U111")
        for col in _LIVE_FLAT:
            assert _txt(m, r, col) == STALE_TEXT

    def test_facts_keep_rendering(self):
        """Quantity and average cost are properties of the position, not
        of the feed — hiding them would lose real information."""
        m = self._model()
        m.set_stale_labels({"J-ib"})
        r = self._row_for(m, "U111")
        for col in _FACT_FLAT:
            v = _txt(m, r, col)
            assert v not in (STALE_TEXT, "", None)
        assert "5" in _txt(m, r, COL_POSITION)

    def test_the_live_connection_is_untouched(self):
        m = self._model()
        m.set_stale_labels({"J-ib"})
        r = self._row_for(m, "U222")
        for col in _LIVE_FLAT:
            assert _txt(m, r, col) != STALE_TEXT

    def test_tooltip_names_the_connection(self):
        m = self._model()
        m.set_stale_labels({"J-ib"})
        r = self._row_for(m, "U111")
        assert "J-ib" in (_tip(m, r, COL_LAST_PRICE) or "")

    def test_reconnect_restores_the_columns(self):
        m = self._model()
        r = self._row_for(m, "U111")
        live = [_txt(m, r, c) for c in _LIVE_FLAT]
        m.set_stale_labels({"J-ib"})
        m.set_stale_labels(set())
        assert [_txt(m, r, c) for c in _LIVE_FLAT] == live

    def test_setter_is_idempotent(self):
        """Repainting on every status tick would fight the user's
        selection and scroll for no reason."""
        m = self._model()
        m.set_stale_labels({"J-ib"})
        seen = []
        m.dataChanged.connect(lambda *a: seen.append(a))
        m.set_stale_labels({"J-ib"})
        assert seen == []
        m.set_stale_labels({"J-ib", "Q-ib"})
        assert seen != []


class TestSummaryModel:
    def _model(self, same_contract=True):
        m = PositionsSummaryModel()
        cid2 = 1 if same_contract else 2
        m.update_positions([
            _pos("J-ib", "U111", con_id=1),
            _pos("Q-ib", "U222", con_id=cid2, symbol="MNQU6" if same_contract
                 else "NQU6"),
        ])
        return m

    def _row_for(self, m, con_id):
        return m._order.index(con_id)

    def test_any_stale_contributor_blanks_the_aggregate(self):
        """The sums span accounts, so one dead connection turns
        Unrealized and Mkt Value into partial totals that read as
        complete."""
        m = self._model()
        m.set_stale_labels({"J-ib"})
        r = self._row_for(m, 1)
        for col in _LIVE_SUM:
            assert _txt(m, r, col) == STALE_TEXT

    def test_facts_and_account_count_survive(self):
        m = self._model()
        m.set_stale_labels({"J-ib"})
        r = self._row_for(m, 1)
        assert _txt(m, r, S_COL_POSITION) not in (STALE_TEXT, "", None)
        assert _txt(m, r, S_COL_AVG_COST) not in (STALE_TEXT, "", None)
        assert _txt(m, r, S_COL_ACCOUNTS) == "2"

    def test_a_fully_live_aggregate_is_untouched(self):
        m = self._model(same_contract=False)
        m.set_stale_labels({"J-ib"})
        r = self._row_for(m, 2)          # Q-ib's own contract
        for col in _LIVE_SUM:
            assert _txt(m, r, col) != STALE_TEXT

    def test_total_excludes_stale_rows_and_says_so(self):
        m = self._model(same_contract=False)
        total = m.rowCount() - 1
        assert _txt(m, total, S_COL_SYMBOL) == "TOTAL"
        m.set_stale_labels({"J-ib"})
        assert _txt(m, total, S_COL_SYMBOL) == "TOTAL *"
        assert "J-ib" in (_tip(m, total, S_COL_MARKET_VALUE) or "")

    def test_total_sums_only_live_rows(self):
        m = self._model(same_contract=False)
        both = _txt(m, m.rowCount() - 1, S_COL_MARKET_VALUE)
        m.set_stale_labels({"J-ib"})
        one = _txt(m, m.rowCount() - 1, S_COL_MARKET_VALUE)
        assert one != both

    def test_labels_recorded_even_for_flat_positions(self):
        """A zero qty is still a reading from a feed; if that feed is
        down the zero is only the last known one."""
        m = PositionsSummaryModel()
        m.set_show_zero(True)
        m.update_positions([_pos("J-ib", "U111", position=0.0)])
        assert m._agg[1]["labels"] == {"J-ib"}


class TestFlashTradeWiring:
    def test_disconnect_marks_reconnect_clears(self, qtbot):
        from src.ui.widgets.flash_trade_view import FlashTradeView
        view = FlashTradeView()
        qtbot.addWidget(view)
        view._on_connection_status_for_positions("J-ib", False)
        assert view._account_position._model.stale_labels() == {"J-ib"}
        view._on_connection_status_for_positions("Q-ib", False)
        assert view._account_position._summary_model.stale_labels() == {
            "J-ib", "Q-ib"}
        view._on_connection_status_for_positions("J-ib", True)
        assert view._account_position._model.stale_labels() == {"Q-ib"}

    def test_fills_are_never_dropped_on_disconnect(self, qtbot):
        """The whole point of leaving Trades alone: IB replays only a
        limited window, so a discarded fill is gone for good."""
        from src.ui.widgets.flash_trade_view import FlashTradeView
        view = FlashTradeView()
        qtbot.addWidget(view)
        view._trades_panel.add_fill({
            "exec_id": "e1", "con_id": 1, "symbol": "MNQ",
            "local_symbol": "MNQU6", "sec_type": "FUT", "side": "BOT",
            "shares": 1, "price": 29500.0, "exchange": "CME",
            "time_sort": "2026-08-21T10:00:00", "label": "J-ib",
        })
        before = view._trades_panel._flat_model.rowCount()
        view._on_connection_status_for_positions("J-ib", False)
        assert view._trades_panel._flat_model.rowCount() == before
