"""
Options-income domain logic — sell covered calls (CC) / cash-secured
puts (CSP), and buy long calls (BC) / long puts (BP).

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

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

MULT = 100  # standard US equity option multiplier

STRATEGY_CC = "CC"    # sell covered call
STRATEGY_CSP = "CSP"  # sell cash-secured put
STRATEGY_BC = "BC"    # buy call
STRATEGY_BP = "BP"    # buy put

# right / order side per strategy
STRATEGY_RIGHT = {"BC": "C", "CC": "C", "BP": "P", "CSP": "P"}
STRATEGY_ACTION = {"BC": "BUY", "CC": "SELL", "BP": "BUY", "CSP": "SELL"}


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


def submit_order_type(mode: str, action: str) -> str:
    """Payload order type for the rail's order *mode* × *action*.

    ``MID`` works the midpoint (Adaptive) for either side.  ``BID`` /
    ``ASK`` are pegged-to-primary (REL) only on the PASSIVE side of the
    book — REL pegs a BUY to the bid and a SELL to the ask, nothing
    else — so the crossing combinations (BUY at ask, SELL at bid)
    collapse to a marketable limit at the live touch instead:

        mode →   BUY      SELL
        MKT      MKT      MKT
        LMT      LMT      LMT
        MID      MID      MID
        BID      BID      LMT   (crossing)
        ASK      LMT      ASK   (crossing)
    """
    mode = (mode or "LMT").upper()
    action = "BUY" if action == "BUY" else "SELL"
    if mode in ("MKT", "MID", "LMT"):
        return mode
    if mode == "BID":
        return "BID" if action == "BUY" else "LMT"
    if mode == "ASK":
        return "ASK" if action == "SELL" else "LMT"
    return "LMT"


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


def break_even_long_call(strike, px) -> Decimal:
    """Long call break-even: strike + premium."""
    return to_dec(strike) + to_dec(px)


def break_even_long_put(strike, px) -> Decimal:
    """Long put break-even: strike − premium."""
    return to_dec(strike) - to_dec(px)


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


_NAKED_MARGIN_PCT = 0.20   # rough initial-margin estimate per short option


def naked_capacity(buying_power, ref_price, mult: int = MULT) -> int:
    """Contracts a MARGIN account can short against buying power, at a
    rough 20%-of-underlying initial-margin estimate per contract.  Only
    an estimate for the MAX button — the worker's whatIf pre-check is
    the authoritative gate before transmit."""
    try:
        bp, ref = float(buying_power), float(ref_price)
    except (TypeError, ValueError):
        return 0
    if bp <= 0 or ref <= 0:
        return 0
    return int(bp // (_NAKED_MARGIN_PCT * ref * mult))


def cc_status(shares, *, is_margin: bool = False,
              buying_power=0.0) -> StrategyStatus:
    """Sell-call eligibility.  Cash accounts need 100 shares (covered
    only); margin accounts may also sell naked, backed by margin."""
    cap = cc_capacity(shares)
    shs = int(float(shares or 0))
    if cap > 0:
        return StrategyStatus(
            True, "ELIGIBLE",
            f"Collateral: {shs:,} shares held · covers {cap}")
    bp = float(buying_power or 0)
    if is_margin and bp > 0:
        return StrategyStatus(
            True, "MARGIN",
            f"Collateral: margin · BP {bp:,.0f}")
    return StrategyStatus(
        False, "NEEDS 100 SH",
        f"Collateral: {shs:,} shares held · needs 100",
        reason="Not enough shares")


def buy_capacity(buying_power, premium, mult: int = MULT) -> int:
    """Max long contracts: floor(BP / (premium × mult)).

    0 while no premium is known — the cap resolves once a quote lands."""
    try:
        bp, px = float(buying_power), float(premium)
    except (TypeError, ValueError):
        return 0
    if bp <= 0 or px <= 0:
        return 0
    return int(bp // (px * mult))


def buy_status(buying_power) -> StrategyStatus:
    """Long call / long put eligibility: any buying power will do —
    the per-contract cost cap is enforced by :func:`buy_capacity`."""
    bp = float(buying_power or 0)
    if bp > 0:
        return StrategyStatus(
            True, "ELIGIBLE",
            f"Cost: paid from buying power · BP {bp:,.0f}")
    return StrategyStatus(
        False, "NO BUYING POWER",
        f"Cost: paid from buying power · BP {bp:,.0f}",
        reason="Not enough buying power")


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


def upcoming_fridays(today: date, n: int = 4) -> list[str]:
    """The next *n* Friday dates as YYYYMMDD, starting with this week's
    (today itself when it's a Friday).  Placeholder expiries for the
    ticket before any chain has loaded."""
    first = today + timedelta(days=(4 - today.weekday()) % 7)
    return [(first + timedelta(weeks=i)).strftime("%Y%m%d")
            for i in range(n)]


def organize_expirations(
    expirations: list[str], today: date, n_chips: int = 4,
) -> tuple[list[Expiry], list[Expiry]]:
    """Split listed *expirations* into ``(chips, more)``.

    Chips are the next *n_chips* WEEK-ENDING expirations — one per
    calendar week, the last listed expiration of that week.  That's the
    Friday weekly on a normal week and the Thursday on holiday-shifted
    weeks, and it holds even for symbols with Mon–Fri dailies
    (QQQ/SPY), so the chip row always reads like
    ``Jul 31 · Aug 7 · Aug 14 · Aug 21``.  Every other future
    expiration (dailies, later weeklies, monthlies, LEAPS) goes to
    *more* in ascending order.
    """
    future: list[Expiry] = []
    week_last: dict[tuple[int, int], Expiry] = {}
    for s in sorted(set(expirations or [])):
        d = _parse_ymd(s)
        if d is None or d < today:
            continue
        dte = (d - today).days
        e = Expiry(
            ymd=s, dte=dte,
            chip_label=d.strftime("%b %d").replace(" 0", " "),
            long_label=f"{d.strftime('%b %d, %Y').replace(' 0', ' ')} "
                       f"({dte}d)",
        )
        future.append(e)
        iso = d.isocalendar()
        week_last[(iso[0], iso[1])] = e   # ascending → last of week wins

    chips = [week_last[k] for k in sorted(week_last)[:n_chips]]
    chip_ymds = {e.ymd for e in chips}
    more = [e for e in future if e.ymd not in chip_ymds]
    return chips, more


# ── Strikes (§4.4) ────────────────────────────────────────────────────────────

def pick_strikes(strikes: list[float], spot: float, right: str,
                 n: int = 50, n_itm: int = 50) -> list[float]:
    """Pick an ATM-centred ladder from the listed *strikes*, sorted
    **descending** (highest strike first — the ladder reads top-down).

    Takes the *n* nearest OTM strikes plus the *n_itm* nearest ITM
    strikes around *spot* (calls: OTM at/above spot, ITM below; puts
    mirrored).  When one side of the chain runs short, the other side
    fills in so the ladder still shows n + n_itm rows when the chain
    allows.
    """
    ss = sorted(set(float(s) for s in (strikes or []) if float(s) > 0))
    if not ss or spot <= 0:
        return sorted(ss[:n + n_itm], reverse=True)
    if right == "C":
        otm = [s for s in ss if s >= spot]           # nearest first
        itm = [s for s in ss if s < spot][::-1]      # nearest first
    else:
        otm = [s for s in ss if s <= spot][::-1]     # nearest first
        itm = [s for s in ss if s > spot]            # nearest first
    out = (otm[:n + max(0, n_itm - len(itm))]
           + itm[:n_itm + max(0, n - len(otm))])
    return sorted(out, reverse=True)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, vol: float,
             right: str, r: float = 0.04) -> float:
    """Black-Scholes European option price (no dividends)."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or vol <= 0:
        return 0.0
    sq = vol * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + vol * vol / 2) * t_years) / sq
    d2 = d1 - sq
    disc = math.exp(-r * t_years)
    if right == "C":
        return spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    return strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_delta(spot: float, strike: float, dte_days: int, premium: float,
             right: str, r: float = 0.04) -> float:
    """Estimate delta from the option's *premium* (mid) via implied vol.

    Fallback for when IB hasn't streamed a greek computation for a
    strike (common on delayed/paper data): bisect Black-Scholes for the
    implied vol matching *premium*, then return N(d1) (calls) or
    N(d1)−1 (puts).  Returns 0.0 when inputs can't support an estimate.
    """
    t = max(0, int(dte_days)) / 365.0
    if spot <= 0 or strike <= 0 or premium <= 0 or t <= 0:
        return 0.0
    intrinsic = max(0.0, spot - strike) if right == "C" \
        else max(0.0, strike - spot)
    if premium <= intrinsic + 1e-9:
        # No time value left — the option trades at parity.
        return (1.0 if right == "C" else -1.0) if intrinsic > 0 else 0.0

    lo, hi = 1e-3, 5.0
    if premium >= bs_price(spot, strike, t, hi, right, r):
        vol = hi
    else:
        for _ in range(60):
            mid_vol = (lo + hi) / 2
            if bs_price(spot, strike, t, mid_vol, right, r) < premium:
                lo = mid_vol
            else:
                hi = mid_vol
        vol = (lo + hi) / 2

    sq = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + vol * vol / 2) * t) / sq
    delta = _norm_cdf(d1) if right == "C" else _norm_cdf(d1) - 1.0
    return round(delta, 4)


def moneyness(strike: float, spot: float, right: str) -> str:
    """"OTM" / "ITM" / "ATM" tag for a strike."""
    if spot <= 0:
        return ""
    if strike == spot:
        return "ATM"
    if right == "C":
        return "OTM" if strike > spot else "ITM"
    return "OTM" if strike < spot else "ITM"
