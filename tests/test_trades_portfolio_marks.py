"""Portfolio market prices must reach the status-bar U.P&L.

Regression for the 2026-08-12 incident: the toolbar showed U = -119
(every open position marked at its own fill price — a no-op mark for a
same-day buy) while the Positions panel showed live prices for the same
symbols.  The portfolio feed stored its mark under the local symbol
only, but ``_last_price_for`` checks the ROOT key first — an L1 tick
that landed once before the market-data stream died left a stale root
price that shadowed every fresh portfolio mark.  ``portfolio_mark_keys``
now makes the portfolio feed refresh every key the lookup can resolve.
"""
from __future__ import annotations

import pytest

from src.ui.widgets.trades_summary_model import (
    TradesSummaryModel,
    portfolio_mark_keys,
)
from src.utils.helpers import format_option_symbol
from src.ui.widgets.trades_filter_proxy import today_et


def _fill(exec_id, sym, local, sec, side, shares, price, mult, hhmmss,
          **over):
    base = {
        "exec_id": exec_id,
        "con_id": {"MNQ": 793356225, "NVDA": 4815747, "QQQ": 320227571}.get(
            sym, 1),
        "symbol": sym,
        "local_symbol": local,
        "sec_type": sec,
        "side": side,
        "shares": shares,
        "price": price,
        "multiplier": mult,
        "account": "U1",
        "account_id": "U1",
        "time_sort": f"{today_et().isoformat()}T{hhmmss}-04:00",
    }
    base.update(over)
    return base


def _feed_portfolio_mark(model, pos):
    """Mirror FlashTradeView._on_portfolio_updated's price-feed loop."""
    mkt = float(pos.get("market_price", 0.0) or 0.0)
    if 0 < mkt < 1e12:
        for sym in portfolio_mark_keys(pos):
            model.set_last_price(sym, mkt)


class TestPortfolioMarkKeys:
    def test_future_feeds_local_and_root(self):
        keys = portfolio_mark_keys(
            {"symbol": "MNQ", "local_symbol": "MNQU6", "sec_type": "FUT"})
        assert set(keys) == {"MNQU6", "MNQ"}

    def test_stock_local_equals_root_dedupes(self):
        keys = portfolio_mark_keys(
            {"symbol": "NVDA", "local_symbol": "NVDA", "sec_type": "STK"})
        assert keys == ("NVDA",)

    def test_option_feeds_raw_and_formatted_never_root(self):
        occ = "QQQ 260807P00670000"
        keys = portfolio_mark_keys(
            {"symbol": "QQQ", "local_symbol": occ, "sec_type": "OPT"})
        assert set(keys) == {occ, format_option_symbol(occ)}
        assert "QQQ" not in keys  # root = underlying price, never a premium

    def test_no_symbol_yields_nothing(self):
        assert portfolio_mark_keys({"sec_type": "STK"}) == ()


class TestStatusBarMarksFromPortfolio:
    """The 2026-08-12 book: 4 open MNQ (2 @ 29889.75 + 2 @ 29860.00,
    multiplier 2) and 200 NVDA @ 222.58, all bought today."""

    @pytest.fixture
    def model(self):
        m = TradesSummaryModel()
        fills = [
            _fill("e1", "MNQ", "MNQU6", "FUT", "BOT", 2, 29889.75, 2,
                  "15:18:58"),
            _fill("e2", "MNQ", "MNQU6", "FUT", "BOT", 2, 29860.00, 2,
                  "15:28:46"),
            _fill("e3", "NVDA", "NVDA", "STK", "BOT", 200, 222.58, 1,
                  "15:36:19"),
        ]
        for f in fills:
            m.add_fill(f)
        return m

    def test_no_marks_falls_back_to_fill_prices(self, model):
        _r, u, _t, _c = model.pnl_totals_today()
        # Newest-fill fallback: MNQ marks at 29860.00 → -119, NVDA at
        # its own buy price → 0.  The incident's exact toolbar number.
        assert u == pytest.approx(-119.0)

    def test_portfolio_marks_reach_u_pnl(self, model):
        _feed_portfolio_mark(model, {
            "symbol": "MNQ", "local_symbol": "MNQU6", "sec_type": "FUT",
            "market_price": 29860.50})
        _feed_portfolio_mark(model, {
            "symbol": "NVDA", "local_symbol": "NVDA", "sec_type": "STK",
            "market_price": 222.73})
        _r, u, _t, _c = model.pnl_totals_today()
        # MNQ: (29860.5-29889.75)*2*2 + (29860.5-29860)*2*2 = -115
        # NVDA: (222.73-222.58)*200 = +30
        assert u == pytest.approx(-85.0)

    def test_stale_root_tick_cannot_shadow_portfolio_mark(self, model):
        # An L1 tick landed hours ago under the root key, then the
        # stream died.  The portfolio feed now refreshes the root too,
        # so the lookup's root-first order no longer pins the stale
        # price.
        model.set_last_price("MNQ", 29902.50)          # stale bg tick
        _feed_portfolio_mark(model, {
            "symbol": "MNQ", "local_symbol": "MNQU6", "sec_type": "FUT",
            "market_price": 29860.50})
        _r, u, _t, _c = model.pnl_totals_today()
        assert u == pytest.approx(-115.0)

    def test_dbl_max_and_zero_prices_are_dropped(self, model):
        _feed_portfolio_mark(model, {
            "symbol": "NVDA", "local_symbol": "NVDA", "sec_type": "STK",
            "market_price": 1.7976931348623157e+308})
        _feed_portfolio_mark(model, {
            "symbol": "MNQ", "local_symbol": "MNQU6", "sec_type": "FUT",
            "market_price": 0.0})
        _r, u, _t, _c = model.pnl_totals_today()
        assert u == pytest.approx(-119.0)   # both rejected → fallback


class TestOptionMarks:
    def test_option_row_marks_premium_not_underlying(self):
        m = TradesSummaryModel()
        occ = "QQQ 260807P00670000"
        m.add_fill(_fill("o1", "QQQ", occ, "OPT", "BOT", 1, 0.86, 100,
                         "10:00:00"))
        # Underlying's root price in the cache must never mark the option.
        m.set_last_price("QQQ", 670.0)
        _r, u, _t, _c = m.pnl_totals_today()
        assert u == pytest.approx(0.0)      # falls back to own fill price
        _feed_portfolio_mark(m, {
            "symbol": "QQQ", "local_symbol": occ, "sec_type": "OPT",
            "market_price": 0.92})
        _r, u, _t, _c = m.pnl_totals_today()
        assert u == pytest.approx((0.92 - 0.86) * 100)
