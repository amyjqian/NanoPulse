"""Tests for DOMLadderModel — pure data transforms, no Qt event loop."""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from src.ui.widgets.dom_ladder import (
    COL_ASK_SZ,
    COL_BID_SZ,
    COL_PRICE,
    DOMLadderModel,
    AboveLastRole,
    IsLastRole,
    LastTradeSizeRole,
    MaxSizeRole,
    PriceRole,
    SizeRole,
    TickDirRole,
)


@pytest.fixture(scope="module")
def qapp():
    """QAbstractTableModel needs a QCoreApplication for its meta-object."""
    app = QApplication.instance() or QApplication([])
    return app


class TestRecenter:
    def test_recenter_builds_descending_ladder(self, qapp):
        m = DOMLadderModel(rows_each_side=5, tick_size=0.25)
        m.recenter(26700.0)
        assert m.prices[0] == 26701.25   # top: 5 ticks above
        assert m.prices[5] == 26700.00   # centre
        assert m.prices[-1] == 26698.75  # bottom: 5 ticks below
        assert len(m.prices) == 11

    def test_recenter_snaps_off_grid_price(self, qapp):
        """centre should be snapped to the tick grid."""
        m = DOMLadderModel(rows_each_side=3, tick_size=0.25)
        m.recenter(26700.13)
        # 26700.13 / 0.25 = 106800.52 → rounds to 106801 → 26700.25
        assert m.prices[3] == 26700.25

    def test_set_tick_size_triggers_rebuild(self, qapp):
        m = DOMLadderModel(rows_each_side=3, tick_size=0.25)
        m.feed_last(26700.0)
        m.set_tick_size(0.50)
        assert m.tick_size == 0.50
        assert m.prices[3] == 26700.00    # centre snapped to 0.50
        assert m.prices[0] == 26701.50    # 3 × 0.50 above


class TestFeedDepth:
    def test_feed_depth_populates_size_dicts(self, qapp):
        m = DOMLadderModel(rows_each_side=5, tick_size=0.25)
        m.recenter(26700.0)
        bids = [(26699.75, 50.0), (26699.50, 100.0), (26699.00, 200.0)]
        asks = [(26700.25, 75.0), (26700.75, 150.0)]
        m.feed_depth(bids, asks)
        # Query via model.data
        idx = m.index(m.prices.index(26699.50), COL_BID_SZ)
        assert m.data(idx, SizeRole) == 100.0
        idx = m.index(m.prices.index(26700.75), COL_ASK_SZ)
        assert m.data(idx, SizeRole) == 150.0

    def test_feed_depth_recomputes_max(self, qapp):
        m = DOMLadderModel(rows_each_side=3, tick_size=0.25)
        m.recenter(100.0)
        m.feed_depth([(99.75, 30)], [(100.25, 80)])
        idx = m.index(0, COL_BID_SZ)
        assert m.data(idx, MaxSizeRole) == 80.0   # biggest across both sides
        # New snapshot with smaller max.
        m.feed_depth([(99.75, 10)], [(100.25, 20)])
        assert m.data(idx, MaxSizeRole) == 20.0

    def test_feed_depth_wholesale_replaces(self, qapp):
        """ib_async gives us the full current state, not diffs —
        feed_depth must wipe stale prices."""
        m = DOMLadderModel(rows_each_side=3, tick_size=0.25)
        m.recenter(100.0)
        m.feed_depth([(99.75, 30), (99.50, 40)], [])
        m.feed_depth([(99.50, 40)], [])            # 99.75 no longer in book
        idx = m.index(m.prices.index(99.75), COL_BID_SZ)
        assert m.data(idx, SizeRole) == 0.0
        idx = m.index(m.prices.index(99.50), COL_BID_SZ)
        assert m.data(idx, SizeRole) == 40.0

    def test_feed_depth_snaps_off_grid_prices(self, qapp):
        """Any price that arrives off-grid (different tick) is snapped
        to the ladder's tick grid before it's stored."""
        m = DOMLadderModel(rows_each_side=5, tick_size=0.25)
        m.recenter(100.0)
        m.feed_depth([(99.88, 10)], [])            # off grid → 99.875? no, round to 0.25
        # 99.88 / 0.25 = 399.52 → rounds to 400 → 100.00  (ties to even / near)
        # Python's round() uses banker's rounding, but 399.52 clearly rounds to 400.
        idx = m.index(m.prices.index(100.00), COL_BID_SZ)
        assert m.data(idx, SizeRole) == 10.0


class TestFeedLast:
    def test_feed_last_sets_last_and_highlight(self, qapp):
        m = DOMLadderModel(rows_each_side=3, tick_size=0.25)
        m.recenter(100.0)
        m.feed_last(100.0)
        assert m.last_price == 100.0
        mid = m.index(m.prices.index(100.0), COL_PRICE)
        assert m.data(mid, IsLastRole) is True

    def test_tick_dir_transitions(self, qapp):
        m = DOMLadderModel(rows_each_side=3, tick_size=0.25)
        m.recenter(100.0)
        m.feed_last(100.0)      # first last, dir stays "flat"
        assert m.data(m.index(0, 0), TickDirRole) == "flat"
        m.feed_last(100.25)     # up
        assert m.data(m.index(0, 0), TickDirRole) == "up"
        m.feed_last(100.00)     # down
        assert m.data(m.index(0, 0), TickDirRole) == "down"
        m.feed_last(100.00)     # identical — dir stays "down"
        assert m.data(m.index(0, 0), TickDirRole) == "down"

    def test_above_last_partition(self, qapp):
        m = DOMLadderModel(rows_each_side=3, tick_size=0.25)
        m.recenter(100.0)
        m.feed_last(100.0)
        # 100.25 is above; 99.75 is below.
        above = m.index(m.prices.index(100.25), COL_PRICE)
        below = m.index(m.prices.index(99.75), COL_PRICE)
        assert m.data(above, AboveLastRole) is True
        assert m.data(below, AboveLastRole) is False


class TestDisplayFormatting:
    def test_empty_size_renders_blank(self, qapp):
        m = DOMLadderModel(rows_each_side=3, tick_size=0.25)
        m.recenter(100.0)
        m.feed_depth([(99.50, 42)], [])
        # 99.75 has no bid
        idx = m.index(m.prices.index(99.75), COL_BID_SZ)
        assert m.data(idx) == ""
        # 99.50 has size 42
        idx = m.index(m.prices.index(99.50), COL_BID_SZ)
        assert m.data(idx) == "42"

    def test_price_display_has_thousand_separator(self, qapp):
        m = DOMLadderModel(rows_each_side=2, tick_size=0.25)
        m.recenter(26700.0)
        idx = m.index(m.prices.index(26700.0), COL_PRICE)
        assert m.data(idx) == "26,700.00"


class TestFeedLastTrade:
    """TWS-style {size} @ {price} stripe support — drives LastTradeSizeRole."""

    def test_feed_last_trade_marks_matching_row(self, qapp):
        m = DOMLadderModel(rows_each_side=5, tick_size=0.01)
        m.recenter(199.76)
        m.feed_last_trade(199.76, 104)
        idx = m.index(m.prices.index(199.76), COL_PRICE)
        assert m.data(idx, LastTradeSizeRole) == 104.0
        # Other rows report 0 — stripe is painted only on the matching row.
        other = m.index(m.prices.index(199.77), COL_PRICE)
        assert m.data(other, LastTradeSizeRole) == 0.0

    def test_feed_last_trade_only_on_price_column(self, qapp):
        m = DOMLadderModel(rows_each_side=3, tick_size=0.01)
        m.recenter(199.76)
        m.feed_last_trade(199.76, 50)
        bid_idx = m.index(m.prices.index(199.76), COL_BID_SZ)
        ask_idx = m.index(m.prices.index(199.76), COL_ASK_SZ)
        assert m.data(bid_idx, LastTradeSizeRole) == 0.0
        assert m.data(ask_idx, LastTradeSizeRole) == 0.0

    def test_feed_last_trade_moves_to_new_row(self, qapp):
        m = DOMLadderModel(rows_each_side=5, tick_size=0.01)
        m.recenter(199.76)
        m.feed_last_trade(199.76, 100)
        m.feed_last_trade(199.77, 200)
        old = m.index(m.prices.index(199.76), COL_PRICE)
        new = m.index(m.prices.index(199.77), COL_PRICE)
        assert m.data(old, LastTradeSizeRole) == 0.0
        assert m.data(new, LastTradeSizeRole) == 200.0

    def test_feed_last_trade_snaps_off_grid_price(self, qapp):
        m = DOMLadderModel(rows_each_side=5, tick_size=0.01)
        m.recenter(199.76)
        m.feed_last_trade(199.763, 75)      # mid-tick → snaps to 199.76
        idx = m.index(m.prices.index(199.76), COL_PRICE)
        assert m.data(idx, LastTradeSizeRole) == 75.0

    def test_non_positive_inputs_ignored(self, qapp):
        m = DOMLadderModel(rows_each_side=3, tick_size=0.01)
        m.recenter(199.76)
        m.feed_last_trade(199.76, 0)        # zero size
        m.feed_last_trade(0, 50)            # zero price
        m.feed_last_trade(-1, 50)           # negative price
        idx = m.index(m.prices.index(199.76), COL_PRICE)
        assert m.data(idx, LastTradeSizeRole) == 0.0
