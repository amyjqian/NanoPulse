"""
Hiding a disconnected connection's accounts.

When a broker connection drops, every account it serves stops reporting.
Its positions and fills are still in memory, but nothing about them is
being refreshed — quotes, P&L and quantities all freeze at whatever they
last were, indistinguishable on screen from live figures.

The rule this module supports: **while a connection is down, its
accounts are not shown at all** — not in Positions, not in Positions
Summary, not in Trades, not in Trades Summary.  Everything left on
screen is being actively refreshed by a live connection.

Nothing is deleted.  Both position caches and the fill store keep their
entries; the views filter on the connection label that every position
dict and every fill dict already carries (see
``IBWorkerThread._do_portfolio_refresh`` and ``_flatten_fill``).  A
reconnect therefore restores the rows immediately, and the fill history
survives — which matters, because IB replays only a limited window and a
discarded fill is gone for good.

**The known cost of this rule.**  A hidden position still exists at the
broker.  A trader glancing at a short Positions tab can read it as "I am
flat" when the missing accounts are in fact carrying risk, and a Trades
P&L that silently drops half the fills reads as a complete day.  That is
why every view that hides something must SAY so — see
``hidden_accounts_notice`` — rather than quietly rendering a shorter
list.
"""
from __future__ import annotations


def hidden_accounts_notice(labels, n_accounts: int = 0) -> str:
    """One-line banner text for views currently hiding accounts.

    Deliberately states the count as well as the connection: "Q-ib
    disconnected" alone reads as a connectivity note, while "2 accounts
    hidden" is what tells the trader the numbers in front of them are
    not the whole book.
    """
    names = ", ".join(sorted(str(x) for x in labels if x))
    if not names:
        return ""
    if n_accounts > 0:
        who = f"{n_accounts} account{'s' if n_accounts != 1 else ''}"
    else:
        who = "its accounts"
    return f"{names} disconnected — {who} hidden until it reconnects"


def hidden_accounts_tooltip(labels, accounts=()) -> str:
    """Longer explanation for the banner's tooltip."""
    names = ", ".join(sorted(str(x) for x in labels if x))
    if not names:
        return ""
    lines = [
        f"{names} is disconnected, so the accounts it serves are hidden "
        f"from Positions and Trades.",
        "",
        "Those positions still exist at the broker — they are hidden "
        "because nothing is refreshing them, not because they are flat.",
        "Nothing is discarded; reconnecting restores every row and fill.",
    ]
    listed = sorted(str(a) for a in accounts if a)
    if listed:
        lines += ["", "Hidden accounts: " + ", ".join(listed)]
    return "\n".join(lines)
