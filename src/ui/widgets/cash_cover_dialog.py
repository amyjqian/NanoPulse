"""Confirmation for covering cash-account buys from sweep holdings.

Shown by ``FlashTradeView`` when a stock buy fans out to selected
accounts and at least one cash account needs more than its available
cash while holding the configured sweep symbols.  One dialog covers the
whole click: a card per such account with its shortfall, the sells that
go out first and — when its holdings cannot cover it all — its reduced
buy size, plus the accounts that will be bought right away.  Cancel
sends nothing to anyone.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from src.core.cash_cover import CoverPlan
from src.ui.theme import Theme

_MONO = "'JetBrains Mono', Menlo, Consolas, monospace"
_SANS = "Inter, 'Helvetica Neue', Arial"
_AMBER = Theme.CLR_FLATTEN
# A card list taller than this scrolls instead of growing the dialog.
_MAX_CARDS_HEIGHT = 560


def _money(v: float) -> str:
    return f"${v:,.0f}"


def cover_summary(plans: list[CoverPlan], aliases: dict[str, str] | None = None,
                  immediate: list[str] | None = None) -> str:
    """The dialog's content as plain text (accessible description)."""
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


def _label(text: str, *, color: str = Theme.TEXT, size: int = 12,
           weight: int = 400, mono: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: {color}; font-size: {size}px; font-weight: {weight};"
        f" font-family: {_MONO if mono else _SANS};"
        " background: transparent; border: none;")
    return lbl


def _tint(hex_color: str, alpha: float) -> str:
    """``#RRGGBB`` → ``rgba(r,g,b,alpha)`` for a translucent fill."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def _pill(text: str, color: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"QLabel {{ color: {color}; background: {_tint(color, 0.14)};"
        f" border: 1px solid {_tint(color, 0.45)}; border-radius: 4px;"
        f" padding: 2px 8px; font-size: 10px; font-weight: 600;"
        f" font-family: {_SANS}; }}")
    return lbl


def _step_badge(n: int, color: str) -> QLabel:
    lbl = QLabel(str(n))
    lbl.setFixedSize(18, 18)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(
        f"color: {Theme.BG_MAIN}; background: {color}; border-radius: 9px;"
        f" font-size: 10px; font-weight: 700; font-family: {_SANS};")
    return lbl


class _AccountCard(QFrame):
    """One cash account: its numbers, then the sell → buy steps."""

    def __init__(self, plan: CoverPlan, name: str, parent=None):
        super().__init__(parent)
        self.setObjectName("coverCard")
        self.setStyleSheet(
            f"#coverCard {{ background: {Theme.BG_INPUT};"
            f" border: 1px solid {Theme.BORDER}; border-radius: 8px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        # ── Header: account + outcome ──────────────────────────────────
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(_label(name, size=13, weight=600))
        head.addWidget(_pill("CASH", Theme.TEXT_DIM))
        head.addStretch(1)
        if plan.shrunk:
            head.addWidget(_pill(
                f"Buy reduced to {plan.buy_qty:,}", _AMBER))
        else:
            head.addWidget(_pill("Full buy", Theme.CLR_BUY))
        lay.addLayout(head)

        # ── Figures: Available · Needs · Short ─────────────────────────
        figs = QGridLayout()
        figs.setHorizontalSpacing(22)
        figs.setVerticalSpacing(1)
        for col, (cap, val, color) in enumerate((
                ("Available", _money(plan.available), Theme.TEXT),
                ("Needs ≈", _money(plan.order_value), Theme.TEXT),
                ("Short", _money(plan.shortfall), Theme.CLR_SELL))):
            figs.addWidget(_label(cap, color=Theme.TEXT_DIM, size=10), 0, col)
            figs.addWidget(_label(val, color=color, size=13, weight=600,
                                  mono=True), 1, col)
        figs.setColumnStretch(3, 1)
        lay.addLayout(figs)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {Theme.BORDER}; border: none;")
        lay.addWidget(sep)

        # ── Steps ──────────────────────────────────────────────────────
        step = 1
        for s in plan.sells:
            lay.addLayout(self._step(
                step, Theme.CLR_SELL,
                f"Sell {s.qty:,} {s.symbol}",
                f"@ MKT  ≈ {_money(s.proceeds)}",
                f"of {int(s.held):,} held"
                + (" — all of it" if s.qty >= int(s.held) else "")))
            step += 1
        if plan.shrunk:
            lay.addLayout(self._step(
                step, _AMBER,
                f"Buy {plan.buy_qty:,} {plan.symbol}",
                f"instead of {plan.requested_qty:,}",
                "sweep holdings can't cover the full order"))
        else:
            lay.addLayout(self._step(
                step, Theme.CLR_BUY,
                f"Buy {plan.buy_qty:,} {plan.symbol}",
                "", "after the sell fills"))

    @staticmethod
    def _step(n: int, color: str, action: str, detail: str,
              note: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(9)
        row.addWidget(_step_badge(n, color))
        row.addWidget(_label(action, size=12, weight=600, mono=True))
        if detail:
            row.addWidget(_label(detail, color=Theme.TEXT, size=12, mono=True))
        row.addStretch(1)
        if note:
            row.addWidget(_label(note, color=Theme.TEXT_DIM, size=11))
        return row


class CashCoverDialog(QDialog):
    def __init__(self, plans: list[CoverPlan],
                 aliases: dict[str, str] | None = None,
                 immediate: list[str] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        aliases = aliases or {}
        name = lambda a: aliases.get(a, "") or a   # noqa: E731
        self.setWindowTitle("Not enough cash")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet(f"QDialog {{ background: {Theme.BG_PANEL}; }}")
        self.setAccessibleDescription(cover_summary(plans, aliases, immediate))

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(0)

        # ── Title ──────────────────────────────────────────────────────
        n = len(plans)
        sym = plans[0].symbol if plans else ""
        qty = plans[0].requested_qty if plans else 0
        root.addWidget(_label(
            "Not enough cash" + (f" in {n} accounts" if n > 1 else ""),
            size=17, weight=600))
        root.addSpacing(4)
        sub = _label(
            f"To buy {qty:,} {sym}, sell sweep holdings first. The buy goes "
            "out once the sells fill.", color=Theme.TEXT_DIM, size=12)
        sub.setWordWrap(True)
        root.addWidget(sub)
        root.addSpacing(16)

        # ── Account cards (scroll when many) ───────────────────────────
        cards = QWidget()
        cards.setStyleSheet("background: transparent;")
        cl = QVBoxLayout(cards)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(10)
        for plan in plans:
            cl.addWidget(_AccountCard(plan, name(plan.account)))
        scroll = QScrollArea()
        scroll.setWidget(cards)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }")
        cl.activate()
        full = cards.sizeHint().height()
        if full <= _MAX_CARDS_HEIGHT:
            scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            cl.setContentsMargins(0, 0, 10, 0)   # gutter for the scrollbar
        scroll.setFixedHeight(min(full, _MAX_CARDS_HEIGHT))
        root.addWidget(scroll)

        # ── Accounts bought straight away ──────────────────────────────
        if immediate:
            root.addSpacing(12)
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(_label("✓", color=Theme.CLR_BUY, size=13,
                                 weight=700))
            now = _label(
                f"Buying {qty:,} {sym} right away in "
                + ", ".join(name(a) for a in immediate),
                color=Theme.TEXT_DIM, size=12)
            now.setWordWrap(True)
            row.addWidget(now, 1)
            root.addLayout(row)

        # ── Buttons ────────────────────────────────────────────────────
        root.addSpacing(18)
        btns = QHBoxLayout()
        btns.setSpacing(10)
        btns.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setMinimumHeight(36)
        cancel.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {Theme.TEXT_DIM};"
            f" border: 1px solid {Theme.BORDER}; border-radius: 6px;"
            f" padding: 0 18px; font-size: 13px; font-family: {_SANS}; }}"
            f"QPushButton:hover {{ color: {Theme.TEXT}; }}")
        cancel.clicked.connect(self.reject)
        ok = QPushButton(
            "Sell && Buy" + (f" in {n} accounts" if n > 1 else ""))
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setMinimumHeight(36)
        ok.setDefault(True)
        ok.setStyleSheet(
            f"QPushButton {{ background: {Theme.CLR_BUY}; color: #fff;"
            " border: none; border-radius: 6px; padding: 0 20px;"
            f" font-size: 13px; font-weight: 600; font-family: {_SANS}; }}"
            "QPushButton:hover { background: #36b04d; }")
        ok.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        root.addLayout(btns)
        self._ok = ok


def confirm_cash_cover(parent: QWidget | None, plans: list[CoverPlan],
                       aliases: dict[str, str] | None = None,
                       immediate: list[str] | None = None) -> bool:
    """Modal ask; True when the user chose Sell & Buy."""
    dlg = CashCoverDialog(plans, aliases, immediate, parent)
    return dlg.exec() == QDialog.DialogCode.Accepted
