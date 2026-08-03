"""
Math utilities and helpers for tick sizes, PnL calculations, and position sizing.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_MONTH_ABBR = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# OCC option localSymbol: root (padded with spaces by IB), YYMMDD expiry,
# C/P right, strike × 1000 as 8 digits — e.g. "QQQ   260807P00670000".
_OCC_OPTION_RE = re.compile(
    r"^([A-Z][A-Z0-9.]*)\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def format_option_symbol(symbol: str) -> str:
    """Pretty-print an OCC option localSymbol for display.

    ``"QQQ 260807P00670000"`` → ``"QQQ Aug7 670 P"``.  Strings that
    don't match the OCC pattern (stocks, futures, already-formatted
    text) are returned unchanged, so this is safe to call on every
    symbol at display time.  Display-only — flatten/close flows match
    positions on the raw localSymbol, so never feed the result back
    into order routing.
    """
    m = _OCC_OPTION_RE.match((symbol or "").strip())
    if not m:
        return symbol
    root, _yy, mm, dd, right, strike_raw = m.groups()
    month = int(mm)
    if not 1 <= month <= 12:
        return symbol
    strike = int(strike_raw) / 1000.0
    return f"{root} {_MONTH_ABBR[month]}{int(dd)} {strike:g} {right}"


def round_to_tick(price: float, tick_size: float = 0.01) -> float:
    """Round price to nearest tick size."""
    if tick_size <= 0:
        return price
    return round(round(price / tick_size) * tick_size, 10)


# ── Offset / capped-price engine ─────────────────────────────────────────────


def calculate_offset(
    value: float,
    unit: str,
    base_price: float,
    tick_size: float = 0.01,
) -> float:
    """Convert a user-specified offset into a dollar amount.

    Parameters
    ----------
    value      : The raw number the user typed (e.g. ``2``, ``0.5``).
    unit       : ``"amt"`` → dollar amount, ``"%"`` → percentage of *base_price*,
                 ``"ticks"`` (or ``"cents"``) → multiples of *tick_size*.
    base_price : The reference price (bid, ask, mid, …).
    tick_size  : Minimum price increment for the instrument (e.g. 0.25 for NQ).

    Returns
    -------
    float – the offset expressed as a dollar amount (always positive or zero).
    """
    if unit == "amt":
        return value
    if unit == "%":
        return base_price * (value / 100.0)
    # "ticks", "cents", or any other tick-based label
    return value * tick_size


def calculate_capped_price(
    symbol_type: str,
    side: str,
    base_price: float,
    tick_size: float = 0.01,
    *,
    prefs: dict[str, Any] | None = None,
    offset_key: str = "side",
) -> float:
    """Return a tick-aligned limit price with the user's offset applied.

    Parameters
    ----------
    symbol_type : ``"FUT"`` or ``"STK"`` — selects which preference section
                  to read (Futures vs Stocks).
    side        : ``"BUY"`` or ``"SELL"``.
    base_price  : The reference price to offset from (bid for BUY, ask for
                  SELL, mid for @Mid, etc.).
    tick_size   : Minimum price increment for the instrument.
    prefs       : If supplied, use this dict instead of reading from
                  :data:`trading_prefs` (useful for unit-testing).
    offset_key  : Which offset fields to read:

                  * ``"side"`` *(default)* — reads ``buy_bid_offset`` /
                    ``buy_bid_unit`` for BUY, ``sell_ask_offset`` /
                    ``sell_ask_unit`` for SELL.
                  * ``"limit_cap"`` — reads ``limit_cap`` / ``limit_cap_unit``
                    (used when a MKT order is capped).

    Returns
    -------
    float – final limit price, rounded to the nearest *tick_size*.

    Examples
    --------
    >>> calculate_capped_price("FUT", "BUY", 20000.00, 0.25,
    ...     prefs={"buy_bid_offset": 2, "buy_bid_unit": "ticks"})
    20000.5
    >>> calculate_capped_price("STK", "BUY", 150.00, 0.01,
    ...     prefs={"limit_cap": 5, "limit_cap_unit": "ticks"},
    ...     offset_key="limit_cap")
    150.05
    """
    # ── 1. Resolve preferences ────────────────────────────────────────────
    if prefs is None:
        from src.core.trading_prefs import trading_prefs
        prefs = trading_prefs.get(symbol_type)

    # ── 2. Pick the correct offset fields ─────────────────────────────────
    if offset_key == "limit_cap":
        raw_value = float(prefs.get("limit_cap", 0))
        unit = str(prefs.get("limit_cap_unit", "ticks"))
    elif side == "BUY":
        raw_value = float(prefs.get("buy_bid_offset", 0))
        unit = str(prefs.get("buy_bid_unit", "ticks"))
    else:
        raw_value = float(prefs.get("sell_ask_offset", 0))
        unit = str(prefs.get("sell_ask_unit", "ticks"))

    # ── 3. Convert to dollar offset ──────────────────────────────────────
    offset = calculate_offset(raw_value, unit, base_price, tick_size)

    # ── 4. Apply direction ───────────────────────────────────────────────
    #   BUY  → add offset  (willing to pay MORE  than base)
    #   SELL → subtract offset (willing to sell for LESS than base)
    if side == "BUY":
        raw_price = base_price + offset
    else:
        raw_price = base_price - offset

    # ── 5. Round to nearest tick ─────────────────────────────────────────
    capped = round_to_tick(raw_price, tick_size)

    log.debug(
        "calculate_capped_price(%s, %s, base=%.4f, tick=%.4f, key=%s) "
        "→ offset=%.4f (%s %s), raw=%.4f, capped=%.4f",
        symbol_type, side, base_price, tick_size, offset_key,
        offset, raw_value, unit, raw_price, capped,
    )
    return capped


def calculate_pnl(entry_price: float, exit_price: float, quantity: int, multiplier: int = 1) -> float:
    """Calculate PnL for a trade."""
    return (exit_price - entry_price) * quantity * multiplier


def calculate_position_size(account_balance: float, risk_percent: float, stop_loss_ticks: int) -> int:
    """Calculate position size based on account balance and risk parameters."""
    risk_amount = account_balance * (risk_percent / 100)
    tick_value = 5  # Default ES/NQ tick value in dollars
    position_size = risk_amount / (stop_loss_ticks * tick_value)
    return int(position_size)


# ── Tick-size lookup ──────────────────────────────────────────────────────────

# Common futures symbol → tick size.  Keyed by the *root* symbol that IB uses.
_FUT_TICK_SIZES: dict[str, float] = {
    "ES":  0.25,   # E-mini S&P 500
    "MES": 0.25,   # Micro E-mini S&P 500
    "NQ":  0.25,   # E-mini Nasdaq-100
    "MNQ": 0.25,   # Micro E-mini Nasdaq-100
    "YM":  1.00,   # E-mini Dow
    "MYM": 1.00,   # Micro E-mini Dow
    "RTY": 0.10,   # E-mini Russell 2000
    "M2K": 0.10,   # Micro E-mini Russell 2000
    "CL":  0.01,   # Crude Oil
    "MCL": 0.01,   # Micro Crude Oil
    "GC":  0.10,   # Gold
    "MGC": 0.10,   # Micro Gold
    "SI":  0.005,  # Silver
    "SIL": 0.005,  # Micro Silver (1000 oz)
    "HG":  0.0005,  # Copper
    "NG":  0.001,  # Natural Gas
    "ZB":  0.03125,  # 30-Year US Treasury Bond
    "ZN":  0.015625,  # 10-Year US Treasury Note
    "ZF":  0.0078125,  # 5-Year US Treasury Note
    "ZC":  0.25,   # Corn
    "ZS":  0.25,   # Soybeans
    "ZW":  0.25,   # Wheat
    "6E":  0.00005,  # Euro FX
    "6J":  0.0000005,  # Japanese Yen
    "6B":  0.0001,  # British Pound
}


def tick_size_for_contract(
    sec_type: str,
    symbol: str = "",
    local_symbol: str = "",
) -> float:
    """Return the minimum tick size for a contract.

    Parameters
    ----------
    sec_type     : ``"STK"``, ``"FUT"``, ``"OPT"``, etc.
    symbol       : Base IB symbol (e.g. ``"NQ"``, ``"AAPL"``).
    local_symbol : IB local symbol (e.g. ``"NQM6"``).

    Returns
    -------
    float – tick size. Falls back to 0.01 for unknown instruments.
    """
    if sec_type == "STK":
        return 0.01   # US equities: penny tick

    if sec_type == "FUT":
        # Try direct symbol match first
        root = symbol.upper()
        if root in _FUT_TICK_SIZES:
            return _FUT_TICK_SIZES[root]

        # Try stripping the month/year suffix from local_symbol
        # e.g. "NQM6" → try "NQ", "NQM"
        ls = (local_symbol or "").upper()
        for length in range(2, len(ls)):
            prefix = ls[:length]
            if prefix in _FUT_TICK_SIZES:
                return _FUT_TICK_SIZES[prefix]

        log.warning("tick_size_for_contract: unknown FUT symbol '%s' / '%s' – defaulting to 0.01",
                    symbol, local_symbol)
        return 0.01

    # OPT and everything else
    return 0.01
