"""
Percent-of-account order sizing (spec: NanoPulse-Ladder-Percent-Sizing.md §4).

Pure Decimal math — no Qt.  Used by the SH/% unit toggle on the order-size
rows (Book Trader ladder + Order Entry panel) and by FlashTradeView's
per-account fan-out.

The percent base is the account's **deployable value** — the lesser of
NetLiquidation and buying power (falling back to NLV alone while the BP
tag hasn't arrived).  Never BP alone: 100% of BP on a 4× margin account
would be a 4× leveraged position behind a single click.  And never NLV
alone: when capital is already tied up in positions (BP < NLV), every
percentage would smash into the BP clamp and resolve to the same share
count — the chips must scale what can actually still be deployed.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN


def _dec(v) -> Decimal:
    """Decimal from float/int/str/Decimal; 0 on garbage."""
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def shares_from_percent(pct, account_value, price) -> int:
    """Whole shares for *pct*% of *account_value* at *price*.

    Floors — never rounds up (25% of $1.42M at 182.40 → 1,946, not 1,947).
    Any non-positive input resolves to 0.
    """
    p, av, px = _dec(pct), _dec(account_value), _dec(price)
    if p <= 0 or av <= 0 or px <= 0:
        return 0
    raw = (av * p / Decimal(100)) / px
    return int(raw.to_integral_value(rounding=ROUND_DOWN))


def clamp_to_buying_power(shares: int, price, buying_power) -> int:
    """Cap *shares* at what *buying_power* can carry at *price*.

    ``buying_power <= 0`` returns *shares* unchanged — the BP tag may
    simply not have arrived yet, and clamping against an unknown value
    of 0 would silently zero every order.
    """
    bp, px = _dec(buying_power), _dec(price)
    if bp <= 0 or px <= 0:
        return shares
    cap = int((bp / px).to_integral_value(rounding=ROUND_DOWN))
    return min(shares, cap)


def sell_shares_from_position(pct, position) -> int:
    """Whole shares for a SELL sized as *pct*% of the *position* held.

    Percent-sell reduces an existing long — it must never open a short,
    so flat/short positions resolve to 0.  Rounds UP (``ceil``) and caps
    at the held amount, mirroring ``ib_worker._do_close_pct``: when
    reducing risk, take the larger slice (3 shares × 10% → 1, not 0).
    """
    p = _dec(pct)
    if p <= 0:
        return 0
    if p > 100:
        p = Decimal(100)
    try:
        held = int(_dec(position))
    except (TypeError, ValueError):
        return 0
    if held <= 0:
        return 0
    qty = int((Decimal(held) * p / Decimal(100))
              .to_integral_value(rounding=ROUND_CEILING))
    return min(qty, held)


def resolve_percent_qty(pct, net_liq, price, buying_power=0.0) -> int:
    """Full resolution: pct of the deployable value — ``min(NLV, BP)``
    when buying power is known, NLV alone otherwise — floored at price,
    BP-clamped, >= 0.

    *pct* is clamped into (0, 100] first — the UI validator already
    enforces this, but the payload path must not trust the caller.
    """
    p = _dec(pct)
    if p <= 0:
        return 0
    if p > 100:
        p = Decimal(100)
    nlv, bp = _dec(net_liq), _dec(buying_power)
    base = min(nlv, bp) if bp > 0 else nlv
    shares = shares_from_percent(p, base, price)
    return max(0, clamp_to_buying_power(shares, price, buying_power))
