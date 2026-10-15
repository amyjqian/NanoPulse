"""Confirmation for covering cash-account buys from sweep holdings.

Shown by ``FlashTradeView`` when a stock buy fans out to selected
accounts and at least one cash account needs more than its available
cash while holding the configured sweep symbols.  One dialog covers the
whole click: each such account with its shortfall, the sells that go
out first and — when its holdings cannot cover it all — its reduced buy
size, plus the accounts that will be bought right away.  Cancel sends
nothing to anyone.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget,
)

from src.core.cash_cover import CoverPlan


def _money(v: float) -> str:
    return f"${v:,.0f}"


def cover_summary(plans: list[CoverPlan], aliases: dict[str, str] | None = None,
                  immediate: list[str] | None = None) -> str:
    """The dialog body as plain text."""
    aliases = aliases or {}
    name = lambda a: aliases.get(a, "") or a   # noqa: E731
    lines: list[str] = []
    for plan in plans:
        if lines:
            lines.append("")
        lines += [
            f"{name(plan.account)} is a cash account with "
            f"{_money(plan.available)} available.",
            f"Buying {plan.requested_qty:,} {plan.symbol} needs about "
            f"{_money(plan.order_value)} — {_money(plan.shortfall)} short.",
            "Sell first:",
        ]
        for s in plan.sells:
            lines.append(f"    {s.qty:,} {s.symbol} @ MKT  "
                         f"(≈ {_money(s.proceeds)}, holding {int(s.held):,})")
        if plan.shrunk:
            lines.append(f"That still leaves it short, so its buy will be "
                         f"reduced to {plan.buy_qty:,} {plan.symbol}.")
        else:
            lines.append(f"Then buy {plan.buy_qty:,} {plan.symbol} once the "
                         "sells have filled.")
    if immediate:
        lines.append("")
        lines.append("Bought right away: " + ", ".join(name(a)
                                                       for a in immediate))
    return "\n".join(lines)


class CashCoverDialog(QDialog):
    def __init__(self, plans: list[CoverPlan],
                 aliases: dict[str, str] | None = None,
                 immediate: list[str] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Not enough cash")
        self.setModal(True)
        lay = QVBoxLayout(self)
        body = QLabel(cover_summary(plans, aliases, immediate))
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(body)
        buttons = QDialogButtonBox()
        ok = buttons.addButton("Sell && Buy", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        ok.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


def confirm_cash_cover(parent: QWidget | None, plans: list[CoverPlan],
                       aliases: dict[str, str] | None = None,
                       immediate: list[str] | None = None) -> bool:
    """Modal ask; True when the user chose Sell & Buy."""
    dlg = CashCoverDialog(plans, aliases, immediate, parent)
    return dlg.exec() == QDialog.DialogCode.Accepted
