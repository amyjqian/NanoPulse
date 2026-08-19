"""Order blotter auto-scroll: new/filled orders snap the view to the top.

New rows insert at row 0 (newest-first sort) and a fill recolours a row
that may be scrolled out of view — in both cases the table must scroll
back to the top so the fresh activity is visible.  Plain in-place status
updates (e.g. Submitted → PreSubmitted) must NOT hijack the scrollbar.
"""
from __future__ import annotations

import pytest

from src.ui.widgets.order_status_panel import OrderStatusPanel


def _order(i: int, **over) -> dict:
    base = {
        "order_ref": f"nano_260819_1000{i:02d}_abcd",
        "symbol": "AAPL", "con_id": 0, "sec_type": "",
        "account": "ACC1", "action": "BUY", "order_type": "LMT",
        "quantity": "5", "limit_price": "100", "stop_price": "–",
        "tif": "DAY", "fill_price": "–", "status": "Submitted",
        "time": f"10:00:{i:02d}", "time_sort": f"2026-08-19T10:00:{i:02d}",
        "error_message": "",
    }
    base.update(over)
    return base


@pytest.fixture
def panel(qtbot):
    p = OrderStatusPanel()
    qtbot.addWidget(p)
    # Small viewport so a modest row count overflows and scrolls.
    p.resize(600, 120)
    p.show()
    qtbot.waitExposed(p)
    for i in range(30):
        p.add_order(f"oid{i}", _order(i))
    return p


def _vbar(panel):
    return panel._table.verticalScrollBar()


def _scroll_to_bottom(panel):
    bar = _vbar(panel)
    bar.setValue(bar.maximum())
    assert bar.value() > 0, "viewport did not overflow — test setup broken"


def test_new_order_scrolls_to_top(panel):
    _scroll_to_bottom(panel)
    panel.add_order("oid_new", _order(31))
    assert _vbar(panel).value() == 0


def test_fill_scrolls_to_top(panel):
    _scroll_to_bottom(panel)
    panel.update_order("oid5", _order(5, status="Filled", fill_price="101"))
    assert _vbar(panel).value() == 0


def test_plain_status_update_keeps_scroll(panel):
    _scroll_to_bottom(panel)
    before = _vbar(panel).value()
    panel.update_order("oid5", _order(5, status="PreSubmitted"))
    assert _vbar(panel).value() == before


def test_repeated_filled_update_keeps_scroll(panel):
    panel.update_order("oid5", _order(5, status="Filled", fill_price="101"))
    _scroll_to_bottom(panel)
    before = _vbar(panel).value()
    panel.update_order("oid5", _order(5, status="Filled", fill_price="101"))
    assert _vbar(panel).value() == before
