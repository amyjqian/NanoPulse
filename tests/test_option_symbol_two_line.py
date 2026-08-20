"""Two-line option-symbol display in the Trades views.

Option symbols render as two stacked lines in the symbol column — root +
expiry on top, strike + right below (``format_option_symbol_two_line``);
the shared table delegate paints any newline'd cell at a smaller font.
Everything else (stocks, futures, price-cache keys, sorting) stays on the
single-line / raw forms.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from src.ui.widgets.trades_model import COL_SYMBOL, TradesModel
from src.ui.widgets.trades_summary_model import (
    S_COL_SYMBOL, TradesSummaryModel)
from src.utils.helpers import (
    format_option_symbol, format_option_symbol_two_line)

_OCC = "AAPL  260918C00250000"


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    """The summary's date filter is strict-today, so pin "today" to the
    fixture fills' date — otherwise the fills fall outside the default
    1-day window whenever the suite runs on a later date."""
    from datetime import date

    import src.ui.widgets.trades_filter_proxy as tfp
    import src.ui.widgets.trades_summary_model as tsm
    pinned = date(2026, 8, 19)          # the day the fills are dated
    monkeypatch.setattr(tfp, "today_et", lambda: pinned)
    monkeypatch.setattr(tsm, "today_et", lambda: pinned)


def _fill(**over) -> dict:
    d = {
        "exec_id": "x1",
        "time": "Aug19 11:33:57",
        "time_sort": "2026-08-19T11:33:57-04:00",
        "con_id": 712345678,
        "symbol": "AAPL",
        "local_symbol": _OCC,
        "sec_type": "OPT",
        "multiplier": 100.0,
        "side": "SLD",
        "shares": 1.0,
        "price": 0.8,
        "exchange": "CBOE",
        "order_ref": "nano_260819_113355_abcd",
        "account": "DU111",
        "account_id": "DU111",
        "expiry": "20260918",
        "strike": 250.0,
        "right": "C",
        "commission": 1.05,
        "realized_pnl": None,
        "order_id": 7,
        "client_id": 1,
        "liquidation": 0,
        "last_liquidity": 1,
        "label": "test",
    }
    d.update(over)
    return d


def _position_row(**over) -> dict:
    d = {
        "account": "DU111", "con_id": 712345678, "symbol": "AAPL",
        "local_symbol": _OCC, "sec_type": "OPT", "exchange": "SMART",
        "currency": "USD", "multiplier": "100", "position": -1.0,
        "avg_cost": 80.0, "market_price": 0.8, "market_value": -80.0,
        "unrealized_pnl": 0.0, "realized_pnl": 0.0,
    }
    d.update(over)
    return d


class TestFormatter:
    def test_occ_splits_into_two_lines(self):
        assert format_option_symbol_two_line(_OCC) == "AAPL Sep18\n250 C"

    def test_two_line_agrees_with_one_line(self):
        one = format_option_symbol(_OCC)
        two = format_option_symbol_two_line(_OCC)
        assert two.replace("\n", " ") == one

    def test_non_occ_passes_through_single_line(self):
        for sym in ("AAPL", "MNQU6", "QQQ Aug7 670 P", ""):
            assert format_option_symbol_two_line(sym) == sym


class TestTradesViews:
    def test_flat_symbol_cell_is_two_line(self, qtbot):
        m = TradesModel()
        m.add_fill(_fill())
        text = m.index(0, COL_SYMBOL).data(Qt.ItemDataRole.DisplayRole)
        assert text == "AAPL Sep18\n250 C"

    def test_flat_stock_symbol_stays_single_line(self, qtbot):
        m = TradesModel()
        m.add_fill(_fill(local_symbol="", symbol="AAPL", sec_type="STK",
                         multiplier=1.0, strike=0.0, right=""))
        text = m.index(0, COL_SYMBOL).data(Qt.ItemDataRole.DisplayRole)
        assert text == "AAPL"

    def test_summary_symbol_cell_is_two_line(self, qtbot):
        m = TradesSummaryModel()
        m.add_fill(_fill())
        # Row 0 is the "TRADE LEVEL" band; the symbol row sits below it.
        text = m.index(1, S_COL_SYMBOL).data(Qt.ItemDataRole.DisplayRole)
        assert text == "AAPL Sep18\n250 C"

    def test_positions_flat_symbol_cell_is_two_line(self, qtbot):
        from src.ui.widgets.account_position_model import (
            COL_SYMBOL as P_COL_SYMBOL, ROW_TYPE_POSITION,
            AccountPositionModel, RowTypeRole)
        m = AccountPositionModel(get_alias=lambda a: "")
        m.update_positions([_position_row()])
        texts = [m.index(r, P_COL_SYMBOL).data(Qt.ItemDataRole.DisplayRole)
                 for r in range(m.rowCount())
                 if m.index(r, 0).data(RowTypeRole) == ROW_TYPE_POSITION]
        assert texts == ["AAPL Sep18\n250 C"]

    def test_positions_summary_symbol_cell_is_two_line(self, qtbot):
        from src.ui.widgets.positions_summary_model import (
            S_COL_SYMBOL as PS_COL_SYMBOL, PositionsSummaryModel)
        m = PositionsSummaryModel(get_alias=lambda a: "")
        m.update_positions([_position_row()])
        text = m.index(0, PS_COL_SYMBOL).data(Qt.ItemDataRole.DisplayRole)
        assert text == "AAPL Sep18\n250 C"

    def test_orders_symbol_cell_is_two_line(self, qtbot):
        from src.ui.widgets.order_status_model import (
            COL_SYMBOL as O_COL_SYMBOL, OrderStatusModel)
        m = OrderStatusModel()
        m.update_order("DU111_nano_260819_113355_abcd", {
            "order_id": "DU111_nano_260819_113355_abcd",
            "order_ref": "nano_260819_113355_abcd",
            "symbol": _OCC, "con_id": 712345678, "sec_type": "OPT",
            "account": "DU111", "action": "SELL", "order_type": "LMT",
            "quantity": "1", "limit_price": "0.80", "stop_price": "–",
            "tif": "DAY", "fill_price": "–", "status": "Submitted",
            "time": "11:33:55", "time_sort": "2026-08-19T11:33:55-04:00",
        })
        text = m.index(0, O_COL_SYMBOL).data(Qt.ItemDataRole.DisplayRole)
        assert text == "AAPL Sep18\n250 C"

    def test_order_entry_chip_label_is_two_line(self, qtbot):
        # The Order Entry symbol chips share the two-line option label
        # (and the chip_small styling) with the Book Trader chip bar.
        from src.ui.widgets.order_entry_panel import OrderEntryPanel
        panel = OrderEntryPanel(active_threads={})
        qtbot.addWidget(panel)
        chip = panel._make_chip(_OCC, "OPT")
        assert chip.text() == "AAPL Sep18\n250 C"
        assert chip.property("chip_small") is True
        assert "font-size:9px" in chip.styleSheet()
        stock = panel._make_chip("AAPL", "STK")
        assert stock.text() == "AAPL"
        assert not stock.property("chip_small")

    def test_summary_sort_value_stays_raw(self, qtbot):
        # Sorting keys off the raw aggregate symbol, not the two-line
        # display text — a newline in the sort key would order options
        # inconsistently against stocks.
        from src.ui.widgets.table_theme import SortValueRole
        m = TradesSummaryModel()
        m.add_fill(_fill())
        v = m.index(1, S_COL_SYMBOL).data(SortValueRole)
        assert "\n" not in v
