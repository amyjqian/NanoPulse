"""Confirmation for covering a cash-account buy from sweep holdings.

Shown by ``FlashTradeView`` when a single cash account's stock buy needs
more than its available cash and the account holds the configured
sweep symbols.  Lists the shortfall, the sells that will go out first,
and — when the holdings cannot cover it all — the reduced buy size.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget,
)

from src.core.cash_cover import CoverPlan


def _money(v: float) -> str:
    return f"${v:,.0f}"


def cover_summary(plan: CoverPlan, display_account: str = "") -> str:
    """The dialog body as plain text (also what the status bar quotes)."""
    acct = display_account or plan.account
    lines = [
        f"{acct} is a cash account with {_money(plan.available)} available.",
        f"Buying {plan.requested_qty:,} {plan.symbol} needs about "
        f"{_money(plan.order_value)} — {_money(plan.shortfall)} short.",
        "",
        "Sell first:",
    ]
    for s in plan.sells:
        lines.append(f"    {s.qty:,} {s.symbol} @ MKT  (≈ {_money(s.proceeds)}, "
                     f"holding {int(s.held):,})")
    lines.append("")
    if plan.shrunk:
        lines.append(f"That still leaves it short, so the buy will be reduced "
                     f"to {plan.buy_qty:,} {plan.symbol}.")
    else:
        lines.append(f"Then buy {plan.buy_qty:,} {plan.symbol} once the sells "
                     "have filled.")
    return "\n".join(lines)


class CashCoverDialog(QDialog):
    def __init__(self, plan: CoverPlan, display_account: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Not enough cash")
        self.setModal(True)
        lay = QVBoxLayout(self)
        body = QLabel(cover_summary(plan, display_account))
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(body)
        buttons = QDialogButtonBox()
        ok = buttons.addButton("Sell && Buy", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        ok.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


def confirm_cash_cover(parent: QWidget | None, plan: CoverPlan,
                       display_account: str = "") -> bool:
    """Modal ask; True when the user chose Sell & Buy."""
    dlg = CashCoverDialog(plan, display_account, parent)
    return dlg.exec() == QDialog.DialogCode.Accepted
