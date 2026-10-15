"""Cover a cash-account buy by selling cash-management holdings.

A cash account (no margin, and every registered plan) can only buy with
the cash it has.  When a stock buy needs more than ``AvailableFunds``
and the account parks cash in the configured sweep symbols (SGOV, BIL,
…), the app offers to sell enough of those first and then send the buy
once the sells fill — instead of letting IB reject the order.

This module is the Qt-free planner: it sizes the sells and, when the
holdings cannot cover the shortfall, shrinks the buy to what the cash
plus the full sale proceeds affords.  ``FlashTradeView`` owns the
confirmation dialog and the sell → fill → buy sequencing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# IB reserves roughly this much over the order value on a market buy in a
# cash account; a check that ignored it would clear a buy IB then rejects.
MARKET_BUY_CUSHION = 1.05

# Sweep sells are sized like the Accounts panel's "Sweep out": rounded up
# to a multiple of 5 so the deficit is covered, capped at the shares held.
_LOT = 5


@dataclass
class CoverSell:
    symbol: str
    qty: int
    price: float          # sizing price (last) — the sell goes out MKT
    held: float

    @property
    def proceeds(self) -> float:
        return self.qty * self.price


@dataclass
class CoverPlan:
    account: str
    symbol: str                       # the stock being bought
    requested_qty: int
    unit_price: float                 # per-share cost used for the check
    available: float                  # AvailableFunds before any sell
    sells: list[CoverSell] = field(default_factory=list)
    buy_qty: int = 0                  # what will actually be sent

    @property
    def order_value(self) -> float:
        return self.requested_qty * self.unit_price

    @property
    def shortfall(self) -> float:
        return max(0.0, self.order_value - self.available)

    @property
    def proceeds(self) -> float:
        return sum(s.proceeds for s in self.sells)

    @property
    def needed(self) -> bool:
        """True when the buy does not fit the available cash."""
        return self.shortfall > 0

    @property
    def shrunk(self) -> bool:
        return self.buy_qty < self.requested_qty


def buy_unit_price(order_type: str, limit_price: float | None,
                   ask: float, last: float) -> float:
    """Per-share cost to check against the cash.

    A limit order costs at most its limit.  Anything that may fill at
    market (MKT, pegs, adaptive) is checked at the ask — or the last
    when there is no ask — plus IB's own cushion.  0 when no price is
    known, in which case no check can be made.
    """
    if order_type == "LMT" and limit_price and limit_price > 0:
        return float(limit_price)
    base = ask if ask and ask > 0 else (last if last and last > 0 else 0.0)
    return base * MARKET_BUY_CUSHION if base > 0 else 0.0


def plan_cash_cover(account: str, symbol: str, qty: int, unit_price: float,
                    available: float,
                    holdings: list[tuple[str, float, float]]) -> CoverPlan:
    """Size the sells that cover a buy of *qty* × *unit_price* against
    *available* cash.

    *holdings* is ``[(sweep_symbol, shares_held, last_price), …]`` in the
    user's configured order; symbols are sold in that order, each only
    as far as still needed.  When everything held is not enough, every
    position is sold in full and ``buy_qty`` shrinks to what the cash
    plus the proceeds affords (0 when nothing fits).
    """
    plan = CoverPlan(account=account, symbol=symbol, requested_qty=int(qty),
                     unit_price=float(unit_price), available=float(available),
                     buy_qty=int(qty))
    if plan.unit_price <= 0 or plan.requested_qty <= 0 or not plan.needed:
        return plan
    remaining = plan.shortfall
    for sym, held, price in holdings:
        if remaining <= 0:
            break
        held = float(held or 0.0)
        price = float(price or 0.0)
        if held <= 0 or price <= 0:
            continue
        want = int(math.ceil(remaining / price / _LOT)) * _LOT
        sell_qty = want if held >= want else int(held)
        if sell_qty <= 0:
            continue
        plan.sells.append(CoverSell(symbol=sym, qty=sell_qty, price=price,
                                    held=held))
        remaining -= sell_qty * price
    if remaining > 0:
        # Holdings cannot cover it: everything is already sold in full
        # above; shrink the buy to what cash + proceeds affords.
        plan.buy_qty = int((plan.available + plan.proceeds) // plan.unit_price)
        if plan.buy_qty < 0:
            plan.buy_qty = 0
    return plan
