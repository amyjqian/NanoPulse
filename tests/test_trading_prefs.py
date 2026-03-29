"""
Tests for the offset / capped-price calculation engine in helpers.py
and the TradingPreferencesManager in trading_prefs.py.
"""
from __future__ import annotations

import pytest
from src.utils.helpers import round_to_tick, calculate_offset, calculate_capped_price


# ═══════════════════════════════════════════════════════════════════════════════
#  round_to_tick
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoundToTick:
    def test_nq_quarter_tick(self):
        assert round_to_tick(20000.10, 0.25) == 20000.00
        assert round_to_tick(20000.13, 0.25) == 20000.25
        assert round_to_tick(20000.37, 0.25) == 20000.25
        assert round_to_tick(20000.38, 0.25) == 20000.50

    def test_stock_penny_tick(self):
        assert round_to_tick(150.003, 0.01) == 150.00
        assert round_to_tick(150.006, 0.01) == 150.01
        assert round_to_tick(150.999, 0.01) == 151.00

    def test_es_quarter_tick(self):
        assert round_to_tick(5000.13, 0.25) == 5000.25

    def test_zero_tick_returns_price(self):
        assert round_to_tick(100.123, 0) == 100.123

    def test_negative_tick_returns_price(self):
        assert round_to_tick(100.123, -1) == 100.123


# ═══════════════════════════════════════════════════════════════════════════════
#  calculate_offset
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalculateOffset:
    def test_amt(self):
        assert calculate_offset(1.50, "amt", 20000.0, 0.25) == 1.50

    def test_percent(self):
        # 0.5% of 20000 = 100
        assert calculate_offset(0.5, "%", 20000.0, 0.25) == 100.0

    def test_ticks(self):
        # 2 ticks × 0.25 tick_size = 0.50
        assert calculate_offset(2, "ticks", 20000.0, 0.25) == 0.50

    def test_cents(self):
        # "cents" treated same as "ticks"
        assert calculate_offset(5, "cents", 150.0, 0.01) == 0.05

    def test_zero_value(self):
        assert calculate_offset(0, "ticks", 100.0, 0.25) == 0.0

    def test_negative_value(self):
        assert calculate_offset(-3, "ticks", 100.0, 0.25) == -0.75


# ═══════════════════════════════════════════════════════════════════════════════
#  calculate_capped_price
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalculateCappedPrice:
    """All tests pass prefs= directly so no disk/singleton dependency."""

    # ── BUY side ──────────────────────────────────────────────────────────────

    def test_buy_ticks_positive(self):
        prefs = {"buy_bid_offset": 2, "buy_bid_unit": "ticks"}
        # 20000 + 2×0.25 = 20000.50
        assert calculate_capped_price("FUT", "BUY", 20000.0, 0.25, prefs=prefs) == 20000.50

    def test_buy_amt(self):
        prefs = {"buy_bid_offset": 1.50, "buy_bid_unit": "amt"}
        # 20000 + 1.50 = 20001.50, rounded to 0.25 → 20001.50
        assert calculate_capped_price("FUT", "BUY", 20000.0, 0.25, prefs=prefs) == 20001.50

    def test_buy_percent(self):
        prefs = {"buy_bid_offset": 0.5, "buy_bid_unit": "%"}
        # 20000 + 0.5%×20000 = 20100.0
        assert calculate_capped_price("FUT", "BUY", 20000.0, 0.25, prefs=prefs) == 20100.0

    def test_buy_negative_offset(self):
        prefs = {"buy_bid_offset": -2, "buy_bid_unit": "ticks"}
        # 20000 + (-2×0.25) = 19999.50
        assert calculate_capped_price("FUT", "BUY", 20000.0, 0.25, prefs=prefs) == 19999.50

    # ── SELL side ─────────────────────────────────────────────────────────────

    def test_sell_ticks_positive(self):
        prefs = {"sell_ask_offset": 2, "sell_ask_unit": "ticks"}
        # 20000 - 2×0.25 = 19999.50
        assert calculate_capped_price("FUT", "SELL", 20000.0, 0.25, prefs=prefs) == 19999.50

    def test_sell_amt(self):
        prefs = {"sell_ask_offset": 0.50, "sell_ask_unit": "amt"}
        # 20000 - 0.50 = 19999.50
        assert calculate_capped_price("FUT", "SELL", 20000.0, 0.25, prefs=prefs) == 19999.50

    def test_sell_percent(self):
        prefs = {"sell_ask_offset": 0.5, "sell_ask_unit": "%"}
        # 20000 - 100 = 19900.0
        assert calculate_capped_price("FUT", "SELL", 20000.0, 0.25, prefs=prefs) == 19900.0

    def test_sell_negative_offset(self):
        prefs = {"sell_ask_offset": -3, "sell_ask_unit": "ticks"}
        # 20000 - (-3×0.25) = 20000.75
        assert calculate_capped_price("FUT", "SELL", 20000.0, 0.25, prefs=prefs) == 20000.75

    # ── Tick rounding ─────────────────────────────────────────────────────────

    def test_rounds_to_nearest_quarter_tick(self):
        prefs = {"buy_bid_offset": 0.30, "buy_bid_unit": "amt"}
        # 20000 + 0.30 = 20000.30 → round to 0.25 → 20000.25
        assert calculate_capped_price("FUT", "BUY", 20000.0, 0.25, prefs=prefs) == 20000.25

    def test_rounds_to_nearest_penny(self):
        prefs = {"buy_bid_offset": 0.123, "buy_bid_unit": "amt"}
        # 150 + 0.123 = 150.123 → round to 0.01 → 150.12
        assert calculate_capped_price("STK", "BUY", 150.0, 0.01, prefs=prefs) == 150.12

    # ── Zero offset (passthrough) ─────────────────────────────────────────────

    def test_zero_offset_buy(self):
        prefs = {"buy_bid_offset": 0, "buy_bid_unit": "ticks"}
        assert calculate_capped_price("FUT", "BUY", 20000.0, 0.25, prefs=prefs) == 20000.0

    def test_zero_offset_sell(self):
        prefs = {"sell_ask_offset": 0, "sell_ask_unit": "ticks"}
        assert calculate_capped_price("FUT", "SELL", 20000.0, 0.25, prefs=prefs) == 20000.0

    # ── Missing keys default to 0 ────────────────────────────────────────────

    def test_missing_offset_key(self):
        prefs = {}
        assert calculate_capped_price("FUT", "BUY", 20000.0, 0.25, prefs=prefs) == 20000.0

    # ── Stock with penny tick ─────────────────────────────────────────────────

    def test_stock_buy_cents(self):
        prefs = {"buy_bid_offset": 5, "buy_bid_unit": "ticks"}
        # 150 + 5×0.01 = 150.05
        assert calculate_capped_price("STK", "BUY", 150.0, 0.01, prefs=prefs) == 150.05

    def test_stock_sell_percent(self):
        prefs = {"sell_ask_offset": 1.0, "sell_ask_unit": "%"}
        # 200 - 1%×200 = 198.0
        assert calculate_capped_price("STK", "SELL", 200.0, 0.01, prefs=prefs) == 198.0

    # ── Limit Cap Offset (MKT capping) ────────────────────────────────────────

    def test_limit_cap_buy_ticks(self):
        prefs = {"limit_cap": 5, "limit_cap_unit": "ticks"}
        # 150 + 5×0.01 = 150.05
        assert calculate_capped_price(
            "STK", "BUY", 150.0, 0.01, prefs=prefs, offset_key="limit_cap"
        ) == 150.05

    def test_limit_cap_sell_ticks(self):
        prefs = {"limit_cap": 5, "limit_cap_unit": "ticks"}
        # 150 - 5×0.01 = 149.95
        assert calculate_capped_price(
            "STK", "SELL", 150.0, 0.01, prefs=prefs, offset_key="limit_cap"
        ) == 149.95

    def test_limit_cap_buy_percent(self):
        prefs = {"limit_cap": 1.0, "limit_cap_unit": "%"}
        # 20000 + 1%×20000 = 20200.0
        assert calculate_capped_price(
            "FUT", "BUY", 20000.0, 0.25, prefs=prefs, offset_key="limit_cap"
        ) == 20200.0

    def test_limit_cap_sell_amt(self):
        prefs = {"limit_cap": 2.50, "limit_cap_unit": "amt"}
        # 20000 - 2.50 = 19997.50
        assert calculate_capped_price(
            "FUT", "SELL", 20000.0, 0.25, prefs=prefs, offset_key="limit_cap"
        ) == 19997.50

    def test_limit_cap_zero_stays_mkt(self):
        """Zero limit_cap should return the base price unchanged."""
        prefs = {"limit_cap": 0, "limit_cap_unit": "ticks"}
        assert calculate_capped_price(
            "STK", "BUY", 150.0, 0.01, prefs=prefs, offset_key="limit_cap"
        ) == 150.0

    def test_limit_cap_rounds_to_tick(self):
        prefs = {"limit_cap": 1, "limit_cap_unit": "ticks"}
        # 20000 + 1×0.25 = 20000.25
        assert calculate_capped_price(
            "FUT", "BUY", 20000.0, 0.25, prefs=prefs, offset_key="limit_cap"
        ) == 20000.25


# ═══════════════════════════════════════════════════════════════════════════════
#  TradingPreferencesManager
# ═══════════════════════════════════════════════════════════════════════════════

class TestTradingPreferencesManager:
    def test_update_and_read(self):
        from src.core.trading_prefs import TradingPreferencesManager
        mgr = TradingPreferencesManager()
        mgr.update(
            futures={"quantity": 5, "buy_bid_offset": 2, "buy_bid_unit": "ticks"},
            stocks={"quantity": 200, "sell_ask_offset": 1.5, "sell_ask_unit": "amt"},
        )
        fut = mgr.futures
        assert fut["quantity"] == 5
        assert fut["buy_bid_offset"] == 2
        # defaults should be merged
        assert fut["limit_cap"] == 0

        stk = mgr.stocks
        assert stk["quantity"] == 200
        assert stk["sell_ask_offset"] == 1.5

    def test_get_by_symbol_type(self):
        from src.core.trading_prefs import TradingPreferencesManager
        mgr = TradingPreferencesManager()
        mgr.update(
            futures={"quantity": 3},
            stocks={"quantity": 300},
        )
        assert mgr.get("FUT")["quantity"] == 3
        assert mgr.get("STK")["quantity"] == 300
        # Unknown type falls back to futures
        assert mgr.get("OPT")["quantity"] == 3

    def test_snapshot_isolation(self):
        """Mutating the returned dict must NOT affect the manager's state."""
        from src.core.trading_prefs import TradingPreferencesManager
        mgr = TradingPreferencesManager()
        mgr.update(futures={"quantity": 1})
        fut = mgr.futures
        fut["quantity"] = 9999
        assert mgr.futures["quantity"] == 1  # unchanged



