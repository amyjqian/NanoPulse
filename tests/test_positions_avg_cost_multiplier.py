"""Avg cost is shown in the same unit as the quote beside it.

IB reports ``Position.avgCost`` per CONTRACT for anything with a
multiplier, while ``market_price`` is per point.  A QQQ put trading at
1.42 arrives as 156.95 on a 100x contract, so the Positions row read
"Avg price 156.95 / Last price 1.42" — a cost basis two orders of
magnitude above the instrument's own last price.  The correction used to
cover ``FUT`` only.

One correction point, two views: ``AccountPositionModel`` applies it to
its copy, and ``PositionsSummaryModel.update_positions`` calls the same
helper on its own copy before aggregating.
"""
from __future__ import annotations

import pytest

from src.ui.widgets.account_position_model import (
    COL_AVG_PRICE, AccountPositionModel,
)
from src.ui.widgets.account_position_panel import AccountPositionPanel
from src.ui.widgets.positions_summary_model import S_COL_AVG_COST


def _pos(**kw) -> dict:
    base = {
        "label": "IB", "account": "U1", "con_id": 11, "symbol": "QQQ",
        "local_symbol": "QQQ   260828P00690000", "sec_type": "OPT",
        "multiplier": "100", "position": -1.0, "avg_cost": 156.95,
        "market_price": 1.42, "market_value": -142.0,
        "unrealized_pnl": 15.0, "realized_pnl": 0.0,
    }
    base.update(kw)
    return base


def _display_avg(pos: dict) -> float:
    pos = dict(pos)
    AccountPositionModel._compute_display_avg(pos)
    return pos["display_avg"]


# ── The correction itself ───────────────────────────────────────────────────

def test_option_avg_cost_is_divided_by_the_multiplier():
    assert _display_avg(_pos()) == pytest.approx(1.5695)


def test_future_avg_cost_still_corrected():
    assert _display_avg(_pos(
        sec_type="FUT", symbol="MNQ", local_symbol="MNQU6", multiplier="2",
        position=4.0, avg_cost=58333.82, market_price=29160.60,
        market_value=233285.0)) == pytest.approx(29166.91)


def test_future_option_is_corrected_too():
    assert _display_avg(_pos(sec_type="FOP", multiplier="50",
                             avg_cost=75.0, market_price=1.5,
                             market_value=-75.0)) == pytest.approx(1.5)


def test_stock_avg_cost_is_left_alone():
    """avgCost is already per share, and the multiplier is 1."""
    assert _display_avg(_pos(
        sec_type="STK", symbol="NVDA", local_symbol="NVDA", multiplier="",
        position=200.0, avg_cost=212.79, market_price=209.25,
        market_value=41850.0)) == pytest.approx(212.79)


def test_an_already_per_point_avg_cost_is_not_divided_again():
    """IB is not perfectly consistent — dividing a figure that is already
    per point would push it away from the quote, so it is left as is."""
    assert _display_avg(_pos(
        sec_type="FUT", multiplier="2", position=4.0,
        avg_cost=29166.91, market_price=29160.60,
        market_value=233285.0)) == pytest.approx(29166.91)


def test_the_declared_multiplier_is_used_before_the_implied_one():
    """Without a quote there is no market value to imply from, so the
    contract's own multiplier has to carry it."""
    assert _display_avg(_pos(market_price=0.0, market_value=0.0)) == \
        pytest.approx(1.5695)


def test_multiplier_falls_back_to_the_implied_one():
    """Older cached entries predate the multiplier field."""
    pos = _pos()
    del pos["multiplier"]
    assert _display_avg(pos) == pytest.approx(1.5695)


def test_no_multiplier_and_no_quote_leaves_avg_cost_untouched():
    pos = _pos(market_price=0.0, market_value=0.0)
    del pos["multiplier"]
    assert _display_avg(pos) == pytest.approx(156.95)


def test_zero_avg_cost_is_safe():
    assert _display_avg(_pos(avg_cost=0.0)) == 0.0


# ── Both views render the corrected figure ──────────────────────────────────

def test_both_views_show_the_per_point_avg(qtbot):
    panel = AccountPositionPanel()
    qtbot.addWidget(panel)
    panel.update_positions([_pos()])
    panel._flush_pending_summary()

    flat = panel._model
    shown = [flat.index(r, COL_AVG_PRICE).data()
             for r in range(flat.rowCount())
             if flat.index(r, COL_AVG_PRICE).data()]
    assert shown == ["1.57"]

    summary = panel._summary_model
    shown = [summary.index(r, S_COL_AVG_COST).data()
             for r in range(summary.rowCount())
             if summary.index(r, S_COL_AVG_COST).data()]
    assert shown == ["1.57"]
