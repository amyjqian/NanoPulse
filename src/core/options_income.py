"""
Options-income domain logic — covered calls (CC) and cash-secured puts (CSP).

Pure functions, no Qt: pricing math (Decimal — money never touches float
until display), collateral eligibility, expiry organisation and strike
selection.  Consumed by the Options Income ticket
(``src/ui/widgets/options_income``) and unit-tested in
``tests/test_options_income.py``.

Conventions
───────────
* Premiums / prices enter as ``float | str | Decimal`` and are normalised
  via :func:`to_dec`; every money-returning function returns ``Decimal``.
* ``spot`` = underlying last price, ``strike`` = option strike,
  ``basis`` = per-share cost basis, ``mult`` = contract multiplier (100).
* Expirations are IB ``YYYYMMDD`` strings.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

MULT = 100  # standard US equity option multiplier

STRATEGY_CC = "CC"
STRATEGY_CSP = "CSP"


# ── Decimal helpers ───────────────────────────────────────────────────────────

def to_dec(value) -> Decimal:
    """Normalise any numeric input to Decimal (via str to avoid float dust)."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(0)


def snap_to_tick(price, tick) -> Decimal:
    """Round *price* to the nearest multiple of *tick* (half-up)."""
    p, t = to_dec(price), to_dec(tick)
    if t <= 0:
        return p.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    steps = (p / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return (steps * t).quantize(t.normalize(), rounding=ROUND_HALF_UP)


# ── Pricing (§5 of the spec) ──────────────────────────────────────────────────

def credit(px, qty: int, mult: int = MULT) -> Decimal:
    """Total premium received: px × mult × qty."""
    return to_dec(px) * mult * int(qty)


def break_even_cc(basis, px) -> Decimal:
    """Covered call break-even: share basis − premium."""
    return to_dec(basis) - to_dec(px)


def break_even_csp(strike, px) -> Decimal:
    """Cash-secured put break-even: strike − premium."""
    return to_dec(strike) - to_dec(px)


def if_called_away(strike, basis, px, qty: int, mult: int = MULT) -> Decimal:
    """CC outcome if assigned: (K − basis) × mult × qty + credit."""
    return ((to_dec(strike) - to_dec(basis)) * mult * int(qty)
            + credit(px, qty, mult))


def if_assigned_cost(strike, px, qty: int, mult: int = MULT) -> Decimal:
    """CSP effective purchase cost if assigned: (K − px) × mult × qty."""
    return (to_dec(strike) - to_dec(px)) * mult * int(qty)


def collateral_csp(strike, qty: int, mult: int = MULT) -> Decimal:
    """Cash / BP reserved by a CSP: K × mult × qty."""
    return to_dec(strike) * mult * int(qty)


def yield_pct(px, ref) -> Decimal:
    """Premium yield in percent: px / ref × 100.

    *ref* is the spot for covered calls, the strike for CSPs.
    Returns 0 when ref is not positive.
    """
    r = to_dec(ref)
    if r <= 0:
        return Decimal(0)
    return (to_dec(px) / r * 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def annualized_pct(yield_percent, dte: int) -> Decimal:
    """Annualise a period yield: yield × 365 / DTE (0 when DTE < 1)."""
    d = max(0, int(dte))
    if d < 1:
        return Decimal(0)
    return (to_dec(yield_percent) * 365 / d).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP)


# ── Eligibility / collateral capacity (§4.2, §4.5) ────────────────────────────

def cc_capacity(shares) -> int:
    """Max covered-call contracts: floor(shares / 100)."""
    try:
        return max(0, int(float(shares) // MULT))
    except (TypeError, ValueError):
        return 0


def csp_capacity(buying_power, strike) -> int:
    """Max cash-secured-put contracts: floor(BP / (strike × 100))."""
    try:
        bp, k = float(buying_power), float(strike)
    except (TypeError, ValueError):
        return 0
    if k <= 0 or bp <= 0:
        return 0
    return int(bp // (k * MULT))


@dataclass(frozen=True)
class StrategyStatus:
    eligible: bool
    badge: str        # "ELIGIBLE" / "NEEDS 100 SH" / "NO BUYING POWER" / …
    detail: str       # collateral line shown on the card
    reason: str = ""  # Sell-button label when ineligible


def cc_status(shares) -> StrategyStatus:
    cap = cc_capacity(shares)
    shs = int(float(shares or 0))
    if cap > 0:
        return StrategyStatus(
            True, "ELIGIBLE",
            f"Collateral: {shs:,} shares held · covers {cap}")
    return StrategyStatus(
        False, "NEEDS 100 SH",
        f"Collateral: {shs:,} shares held · needs 100",
        reason="Not enough shares")


def csp_status(buying_power, *, is_margin: bool = True) -> StrategyStatus:
    bp = float(buying_power or 0)
    if not is_margin:
        return StrategyStatus(
            False, "CASH ACCT",
            f"Collateral: buying power · BP {bp:,.0f}",
            reason="Not enough buying power")
    if bp >= MULT:  # can secure at least a $1 strike
        return StrategyStatus(
            True, "ELIGIBLE",
            f"Collateral: buying power · BP {bp:,.0f}")
    return StrategyStatus(
        False, "NO BUYING POWER",
        f"Collateral: buying power · BP {bp:,.0f}",
        reason="Not enough buying power")


# ── Expiries (§4.3) ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Expiry:
    ymd: str      # "20260814"
    dte: int
    chip_label: str   # "Aug 14" (chips)
    long_label: str   # "Aug 14, 2026 (17d)" (More dropdown)


def _parse_ymd(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def organize_expirations(
    expirations: list[str], today: date, n_chips: int = 4,
) -> tuple[list[Expiry], list[Expiry]]:
    """Split listed *expirations* into ``(chips, more)``.

    Chips are the next *n_chips* listed expirations that fall on the next
    upcoming Fridays (per spec).  Because exchange holidays shift a weekly
    expiry to Thursday, any listed expiry within the chip horizon counts —
    we simply take the first *n_chips* future listed expirations, which in
    practice are the next four weeklies.  Everything later goes to *more*
    (remaining weeklies, monthlies, LEAPS) in ascending order.
    """
    future: list[Expiry] = []
    for s in sorted(set(expirations or [])):
        d = _parse_ymd(s)
        if d is None or d < today:
            continue
        dte = (d - today).days
        future.append(Expiry(
            ymd=s, dte=dte,
            chip_label=d.strftime("%b %d").replace(" 0", " "),
            long_label=f"{d.strftime('%b %d, %Y').replace(' 0', ' ')} "
                       f"({dte}d)",
        ))
    return future[:n_chips], future[n_chips:]


# ── Strikes (§4.4) ────────────────────────────────────────────────────────────

def pick_strikes(strikes: list[float], spot: float, right: str,
                 n: int = 6) -> list[float]:
    """Pick *n* OTM-biased strikes from the listed *strikes*.

    Calls: the *n* lowest strikes at/above spot (ascending — closest
    first).  Puts: the *n* highest strikes at/below spot (descending —
    closest first).  When fewer OTM strikes exist, pad with the nearest
    ITM ones so the table always shows *n* rows if the chain allows.
    """
    ss = sorted(set(float(s) for s in (strikes or []) if float(s) > 0))
    if not ss or spot <= 0:
        return ss[:n]
    if right == "C":
        otm = [s for s in ss if s >= spot]
        itm = [s for s in ss if s < spot]
        out = otm[:n]
        if len(out) < n:                      # pad with nearest ITM below
            out = list(reversed(itm[-(n - len(out)):])) + out
            out.sort()
        return out
    otm = [s for s in ss if s <= spot]
    itm = [s for s in ss if s > spot]
    out = list(reversed(otm))[:n]             # descending, closest first
    if len(out) < n:                          # pad with nearest ITM above,
        pad = itm[:n - len(out)]              # keeping the list descending
        out = list(reversed(pad)) + out
    return out


def moneyness(strike: float, spot: float, right: str) -> str:
    """"OTM" / "ITM" / "ATM" tag for a strike."""
    if spot <= 0:
        return ""
    if strike == spot:
        return "ATM"
    if right == "C":
        return "OTM" if strike > spot else "ITM"
    return "OTM" if strike < spot else "ITM"
