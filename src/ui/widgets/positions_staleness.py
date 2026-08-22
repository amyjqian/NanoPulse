"""
Marking position data stale while its broker connection is down.

A position is a fact about the ACCOUNT, so a dropped connection must not
make the row disappear — an empty Positions tab reads as "you are flat",
which is the most dangerous possible misreading while you are carrying
risk.  But half the columns are derived from a live quote feed, and the
moment the connection drops they freeze at whatever they last were:
disconnect at 10:00, the market moves 200 points, and at 14:00 the panel
still shows 10:00's unrealized P&L in confident green.

So the rule is: keep the row, keep the facts, blank the rest.

* **Facts** — quantity, average cost — come from the position itself and
  stay true until the position changes, which it cannot do unobserved
  for long (see below).  Keep rendering them.
* **Quote-derived** — last price, unrealized, realized, day P&L, market
  value — are only as fresh as the feed.  These render as
  :data:`STALE_TEXT` rather than a frozen figure, because a blank cell
  cannot be mistaken for a current number and a stale one can.

Staleness is keyed on the CONNECTION LABEL, not the account: every
position dict the worker emits carries ``label`` (see
``IBWorkerThread._do_portfolio_refresh``), so a row can be attributed
without maintaining a separate account→connection map that could itself
go stale.

Nothing here clears data.  Recovery is already handled on the other
side: ``_do_portfolio_refresh`` rebuilds from ``ib.positions()`` on every
(re)connect and emits zero-qty entries for cached positions that no
longer appear, so a position closed elsewhere during the outage
correctly collapses to flat once the connection returns.

Fills are deliberately NOT covered by any of this.  A fill is a
historical event that happened regardless of connection state, and
dropping fills would be unrecoverable — IB replays only a limited
window.  The Trades view keeps everything.
"""
from __future__ import annotations

# Rendered in place of a quote-derived value whose connection is down.
# The en-dash matches the placeholder the position models already use for
# "no data yet", so a disconnected cell reads as absent rather than zero.
STALE_TEXT = "–"


def stale_tooltip(labels) -> str:
    """Tooltip for a cell or row blanked by a dropped connection."""
    names = ", ".join(sorted(str(x) for x in labels if x))
    if not names:
        return ""
    conn = "connection" if len(set(labels)) == 1 else "connections"
    return (f"{names} disconnected — quantity and average cost are the "
            f"last known values; live figures are hidden rather than "
            f"shown stale.\nReconnect to refresh this {conn}.")


def partial_total_tooltip(labels) -> str:
    """Tooltip for a TOTAL row that had to leave rows out."""
    names = ", ".join(sorted(str(x) for x in labels if x))
    if not names:
        return ""
    return (f"Partial total — positions on {names} are excluded while "
            f"disconnected, so this sums only connections reporting live.")
