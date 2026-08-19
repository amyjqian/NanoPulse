"""
Shared parts for the SH/% order-size unit toggle
(spec: NanoPulse-Ladder-Percent-Sizing.md).

Used by BOTH size rows — ``order_panel_parts.QtyChipRow`` (Book Trader
ladder) and ``OrderEntryPanel``'s inline row.  This module must import
neither panel so it can sit below both in the import graph.

Styling follows the app's per-widget-stylesheet pattern (not QSS dynamic
properties — this app doesn't style chips from ``styles.qss``).  The SH
branch of :func:`apply_sized_chip_style` carries the two pre-existing
stylesheet strings **verbatim** so share mode stays byte-identical to
the pre-toggle row.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from src.ui.theme import Theme
from src.core.percent_sizing import (
    resolve_percent_qty,
    sell_shares_from_position,
)

UNIT_SH = "SH"
UNIT_PCT = "PCT"

PCT_PRESETS: list[int] = [10, 25, 50, 75, 100]     # spec §3.2
PCT_DEFAULT = 25
PCT_POPUP: list[int] = list(range(1, 101))         # spinner ladder, 1–100
TOGGLE_W = 38                                      # spec §2

_TOOLTIP_SH = "Sizing in shares — click for percent of account value"
_TOOLTIP_PCT = ("Sizing by percent of deployable account value (the "
                "lesser of net liquidation and buying power) — "
                "click for shares")

# Derived from the symbol-chip styles (_apply_chip_style in
# order_entry_panel/order_panel_parts) but numeric chips render in
# JetBrains Mono — numbers you scan use the mono family, words use Inter.
_SH_CHIP_ON = (
    "QPushButton{background:#539BF5;color:white;border:none;"
    "border-radius:6px;padding:4px 12px;font-weight:bold;font-size:12px;"
    "font-family:'JetBrains Mono'}"
    "QPushButton:hover{background:#6BA8F6}")
_SH_CHIP_OFF = (
    "QPushButton{background:#232A3B;color:#A3AFC2;border:none;"
    "border-radius:6px;padding:4px 12px;font-weight:600;font-size:12px;"
    "font-family:'JetBrains Mono'}"
    "QPushButton:hover{background:#334058}")

_PCT_CHIP_ON = (
    f"QPushButton{{background:{Theme.CLR_PCT};color:{Theme.CLR_PCT_ON};"
    "border:none;border-radius:6px;padding:4px 12px;font-weight:bold;"
    "font-size:12px;font-family:'JetBrains Mono'}"
    f"QPushButton:hover{{background:{Theme.CLR_PCT_BORDER}}}")
_PCT_CHIP_OFF = (
    f"QPushButton{{background:{Theme.CLR_PCT_BG};color:{Theme.CLR_PCT_TEXT};"
    "border:1px solid rgba(45,180,164,.35);border-radius:6px;padding:4px 12px;"
    "font-weight:600;font-size:12px;font-family:'JetBrains Mono'}"
    f"QPushButton:hover{{border-color:{Theme.CLR_PCT_BORDER}}}")
_CHIP_DISABLED = (
    "QPushButton{background:#2a2a33;color:#6A7690;border:none;"
    "border-radius:6px;padding:4px 12px;font-weight:600;font-size:12px;"
    "font-family:'JetBrains Mono'}")

_TOGGLE_SH = (
    "QPushButton{background:#3a4250;color:#E6EBF2;"
    "border:1px solid #334058;border-radius:6px;padding:4px 0;"
    "font-weight:bold;font-size:12px}"
    "QPushButton:hover{border-color:#5a6474}")
_TOGGLE_PCT = (
    f"QPushButton{{background:{Theme.CLR_PCT};color:{Theme.CLR_PCT_ON};"
    f"border:1px solid {Theme.CLR_PCT_BORDER};border-radius:6px;"
    "padding:4px 0;font-weight:bold;font-size:12px}"
    f"QPushButton:hover{{background:{Theme.CLR_PCT_BORDER}}}")


def make_unit_toggle(parent=None) -> QPushButton:
    """The 38×28 SH/% cycle button (28 matches today's chip height)."""
    btn = QPushButton(parent)
    btn.setFixedSize(TOGGLE_W, 28)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    apply_unit_toggle_style(btn, UNIT_SH)
    return btn


def apply_unit_toggle_style(btn: QPushButton, unit: str) -> None:
    """Label shows the unit you are IN (spec §3.1); chips recolor too —
    that redundancy is what makes the single-label toggle unambiguous."""
    if unit == UNIT_PCT:
        btn.setText("%")
        btn.setStyleSheet(_TOGGLE_PCT)
        btn.setToolTip(_TOOLTIP_PCT)
    else:
        btn.setText("SH")
        btn.setStyleSheet(_TOGGLE_SH)
        btn.setToolTip(_TOOLTIP_SH)


def apply_sized_chip_style(btn: QPushButton, selected: bool, unit: str,
                           enabled: bool = True) -> None:
    """Chip style per unit.  SH uses the original stylesheets verbatim;
    PCT is the gold pair; a disabled chip (percent resolves to 0 or no
    live price) greys out and stops accepting clicks (spec §4).

    No-op when nothing changed: in percent mode every price tick
    restyles every chip, and each setStyleSheet is a full re-polish."""
    state = (selected, unit, enabled)
    if state == getattr(btn, "_chip_style_state", None):
        return
    btn._chip_style_state = state
    if not enabled:
        btn.setStyleSheet(_CHIP_DISABLED)
        btn.setEnabled(False)
        return
    btn.setEnabled(True)
    if unit == UNIT_PCT:
        btn.setStyleSheet(_PCT_CHIP_ON if selected else _PCT_CHIP_OFF)
    else:
        btn.setStyleSheet(_SH_CHIP_ON if selected else _SH_CHIP_OFF)


class PercentSizingContext:
    """Per-panel percent→shares resolver.  Pure state holder.

    Percent is SIDE-AWARE:
      * BUY  — pct of the account's NetLiquidation at the last price
        (capital deployment; price-gated, BP-clamped, floored);
      * SELL — pct of the account's current position in the active
        symbol (risk reduction; needs no price, ceil'd + capped at the
        holding, never opens a short).

    The host pushes three callables:
      * ``accounts_provider(symbol) -> [(account, nlv, bp, held)]`` —
        the checked accounts with each one's holding of *symbol*
        (FlashTradeView owns that knowledge);
      * ``price_provider() -> float`` — last price of the active symbol,
        0 when none (BUY must never resolve against a stale/zero price
        — spec §4);
      * ``symbol_provider() -> str`` — the panel's active symbol.
    """

    def __init__(self) -> None:
        self._accounts_provider: Callable[[str], list] | None = None
        self._price_provider: Callable[[], float] | None = None
        self._symbol_provider: Callable[[], str] | None = None

    def set_accounts_provider(self, fn) -> None:
        self._accounts_provider = fn

    def set_price_provider(self, fn) -> None:
        self._price_provider = fn

    def set_symbol_provider(self, fn) -> None:
        self._symbol_provider = fn

    def price(self) -> float:
        try:
            return float(self._price_provider() or 0.0) \
                if self._price_provider else 0.0
        except Exception:
            return 0.0

    def symbol(self) -> str:
        try:
            return str(self._symbol_provider() or "") \
                if self._symbol_provider else ""
        except Exception:
            return ""

    def accounts(self) -> list:
        try:
            return list(self._accounts_provider(self.symbol()) or []) \
                if self._accounts_provider else []
        except Exception:
            return []

    def resolve(self, pct: int) -> list[tuple[str, int]]:
        """BUY side: [(account, shares)] from NLV; [] when no accounts
        or no live price."""
        price = self.price()
        if price <= 0:
            return []
        out = []
        for account, nlv, bp, _held in self.accounts():
            out.append((account, resolve_percent_qty(pct, nlv, price, bp)))
        return out

    def resolve_sell(self, pct: int) -> list[tuple[str, int]]:
        """SELL side: [(account, shares)] from each account's position —
        price-free, 0 for flat/short holders."""
        return [(account, sell_shares_from_position(pct, held))
                for account, _nlv, _bp, held in self.accounts()]

    def preview_qty(self, pct: int) -> int:
        """Conservative BUY preview: the SMALLEST positive per-account
        count (the badge / close-tier snapshot must never overstate what
        the smallest checked account will trade); 0 when nothing
        resolves."""
        counts = [n for _a, n in self.resolve(pct) if n > 0]
        return min(counts) if counts else 0

    def preview_total(self, pct: int) -> int:
        """Total BUY shares across ALL resolving accounts."""
        return sum(n for _a, n in self.resolve(pct) if n > 0)

    def sell_preview_min(self, pct: int) -> int:
        """Smallest positive per-account SELL count; 0 when none."""
        counts = [n for _a, n in self.resolve_sell(pct) if n > 0]
        return min(counts) if counts else 0

    def sell_preview_total(self, pct: int) -> int:
        """Total SELL shares across ALL resolving accounts."""
        return sum(n for _a, n in self.resolve_sell(pct) if n > 0)

    def side_qty(self, pct: int, action: str) -> int:
        """Per-side conservative count for click guards."""
        return (self.sell_preview_min(pct) if action == "SELL"
                else self.preview_qty(pct))

    def chip_enabled(self, pct: int) -> bool:
        """A chip stays clickable when EITHER side can resolve — a
        holder without a live price can still percent-SELL."""
        return self.preview_qty(pct) > 0 or self.sell_preview_min(pct) > 0

