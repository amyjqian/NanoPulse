"""
Options Income ticket — selector widgets.

* :class:`SymbolChipBar`  — the ticket's symbol chips on the Book
  Trader's chip grid (position-derived + searched groups, right-click
  removal, QSettings persistence).  Symbols come in via ``select``.
* :class:`RightToggle`    — the Calls / Puts segmented toggle shown left
  of the EXPIRY chips (the order side lives on the rail's Buy / Sell
  toggle).
* :class:`ExpirySelector` — next-four-weeklies chips plus a ``More ▾``
  dropdown for later expirations.

Pure view layer: all state changes surface as Qt signals.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.theme import Theme
from src.ui.widgets.order_panel_parts import SymbolChipBar as ChipGrid
from src.core.options_income import Expiry

log = logging.getLogger(__name__)

ACCENT_BLUE = "#539BF5"          # active-selection blue used app-wide


def _symbol_chip_style(active: bool) -> str:
    """Solid-blue active / grey inactive for the Call/Put toggle and the
    expiry chips — the Book Trader symbol-chip look
    (order_panel_parts._apply_chip_style) one size down."""
    # One step smaller than the symbol chips (11 px, tighter padding):
    # these are selectors above the ladder, not the trade's subject.
    if active:
        return ("QPushButton{background:#539BF5;color:white;border:none;"
                "border-radius:5px;padding:3px 10px;font-weight:bold;"
                "font-size:11px}"
                "QPushButton:hover{background:#6BA8F6}")
    return ("QPushButton{background:#232A3B;color:#A3AFC2;border:none;"
            "border-radius:5px;padding:3px 10px;font-weight:600;"
            "font-size:11px}"
            "QPushButton:hover{background:#334058}")


# ══════════════════════════════════════════════════════════════════════════════
#  SymbolChipBar
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_CHIPS = ["QQQ", "SPY", "NVDA", "TSLA", "NBIS"]
_SETTINGS_KEY = "OptionsTicket/symbolChips"

_POS_CHIP_MAX = 5        # top-N account positions shown as chips
_GRID_COLS = 4           # chips per row
_GRID_ROWS = 3           # rows — the grid never overflows past this
_CHIP_MAX = _GRID_COLS * _GRID_ROWS
_POS_CHIP_MIN_SHARES = 100  # only CC-capable holdings become chips


class SymbolChipBar(QWidget):
    """The ticket's symbol chips (§4.1) — the Book Trader's chip grid
    (``order_panel_parts.SymbolChipBar`` in compact mode: ``_GRID_COLS``
    × ``_GRID_ROWS`` chips one size down from the Book Trader's,
    blue-active / grey, drag-reorder, right-click remove) fed from two
    groups:

    * existing positions across the checked accounts (top
      ``_POS_CHIP_MAX`` STK holdings with ≥ ``_POS_CHIP_MIN_SHARES``
      shares in total, by market value — derived, not persisted), then
    * searched symbols (persisted to QSettings, appended to the right).

    Symbols are added by the host through :meth:`select` (the ticker
    strip's "+" opens the shared symbol-search popup).  The grid holds
    at most ``_CHIP_MAX`` chips: a new symbol on a full grid replaces
    the LAST searched chip rather than becoming an invisible overflow
    entry (same rule as the Book Trader's chip bar).  Chips show the
    bare ticker.

    Signals
    -------
    symbol_selected(str)          — a chip became active
    """

    symbol_selected = Signal(str)

    def __init__(self, get_positions=None, parent=None):
        """*get_positions() -> list[dict]* — STK positions across the
        checked accounts, one entry per symbol (``symbol``, ``position``,
        ``market_value``)."""
        super().__init__(parent)
        self._get_positions = get_positions or (lambda: [])
        self._custom: list[str] = self._load_chips()   # searched, persisted
        self._pos_syms: list[str] = []                 # derived positions
        self._order: list[str] = []                    # display order
        # No chip starts active — the tab opens with the bar unselected
        # until the user picks a chip.
        self._active: str = ""
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self._grid = ChipGrid(self, columns=_GRID_COLS,
                              max_stk_rows=_GRID_ROWS, compact=True)
        self._grid.symbol_selected.connect(lambda s, _sec: self.select(s))
        self._grid.symbol_remove_requested.connect(self._remove_chip)
        self._grid.symbols_reordered.connect(self._on_reordered)
        col.addWidget(self._grid)
        self._rebuild_chips()

    # ── Persistence ─────────────────────────────────────────────────────────

    @staticmethod
    def _load_chips() -> list[str]:
        saved = QSettings("NanoPulse", "Dashboard").value(_SETTINGS_KEY)
        if isinstance(saved, str):
            saved = [s for s in saved.split(",") if s.strip()]
        if isinstance(saved, (list, tuple)) and saved:
            return [str(s).upper() for s in saved]
        return []

    def _save_chips(self) -> None:
        QSettings("NanoPulse", "Dashboard").setValue(
            _SETTINGS_KEY, ",".join(self._custom))

    # ── Chip groups ─────────────────────────────────────────────────────────

    def _position_symbols(self) -> list[str]:
        """Top holdings (across the checked accounts) that can back a
        covered call."""
        try:
            positions = list(self._get_positions() or [])
        except Exception:
            positions = []
        cands = []
        for p in positions:
            try:
                if float(p.get("position") or 0.0) < _POS_CHIP_MIN_SHARES:
                    continue
                sym = str(p.get("symbol") or "").upper()
                if not sym:
                    continue
                cands.append((abs(float(p.get("market_value") or 0.0)), sym))
            except (TypeError, ValueError):
                continue
        cands.sort(reverse=True)
        out: list[str] = []
        for _mv, sym in cands:
            if sym not in out:
                out.append(sym)
            if len(out) >= _POS_CHIP_MAX:
                break
        return out

    def _sync_order(self) -> None:
        """Refresh the display order: position chips keep the left block,
        searched chips sit to their right; user drag-order survives."""
        self._pos_syms = self._position_symbols()
        custom = [s for s in self._custom if s not in self._pos_syms]
        shown = set(self._pos_syms) | set(custom)
        order = [s for s in self._order if s in shown]
        for s in self._pos_syms:
            if s not in order:
                last = max((order.index(x) for x in self._pos_syms
                            if x in order), default=-1)
                order.insert(last + 1, s)
        for s in custom:
            if s not in order:
                order.append(s)
        if len(order) > _CHIP_MAX:
            # A persisted list longer than the grid (an older, bigger
            # layout) is pruned to what fits — the grid would hide the
            # tail anyway, and a hidden chip is a stale one.
            dropped = order[_CHIP_MAX:]
            order = order[:_CHIP_MAX]
            self._custom = [s for s in self._custom if s not in dropped]
            self._save_chips()
        self._order = order

    # ── UI ──────────────────────────────────────────────────────────────────

    def _rebuild_chips(self) -> None:
        self._sync_order()
        self._grid.set_symbols([(sym, "STK") for sym in self._order])
        self._grid.set_active(self._active or None)

    @property
    def _buttons(self) -> dict[str, QPushButton]:
        """symbol → chip button (the grid's), for tests / tooling."""
        return self._grid._buttons     # noqa: SLF001 — same package family

    def refresh_counts(self) -> None:
        """Re-render chips (call after position data or the checked
        accounts change — the position group is re-derived)."""
        self._rebuild_chips()

    # ── Chips API ───────────────────────────────────────────────────────────

    def active_symbol(self) -> str:
        return self._active

    def symbols(self) -> list[str]:
        """Every symbol currently shown as a chip (display order)."""
        return list(self._order)

    def select(self, symbol: str) -> None:
        symbol = symbol.upper()
        self._sync_order()
        if symbol not in self._order and symbol not in self._custom:
            # Searched symbols become persistent chips on the right.
            # A full grid drops its last searched chip to make room —
            # dropping the entry IS the cleanup.
            if len(self._order) >= _CHIP_MAX:
                last = next((s for s in reversed(self._order)
                             if s in self._custom), None)
                if last is not None:
                    self._custom.remove(last)
            self._custom.append(symbol)
            self._save_chips()
        if symbol == self._active:
            self._rebuild_chips()
            return
        self._active = symbol
        self._rebuild_chips()
        self.symbol_selected.emit(symbol)

    def _on_reordered(self, order: list) -> None:
        """Grid drag-reorder → adopt the new order and persist the
        searched chips' relative order (position chips are derived from
        the checked accounts, so their order is session-only)."""
        self._order = [str(s) for s in order if s in self._order]
        custom = set(self._custom)
        self._custom = [s for s in self._order if s in custom]
        self._save_chips()
        self._rebuild_chips()

    def _on_chip_drop(self, src: str, dst: str) -> None:
        """Ladder-style drag reorder: insert *src* at *dst*'s slot."""
        if (src == dst or src not in self._order
                or dst not in self._order):
            return
        order = list(self._order)
        order.remove(src)
        order.insert(order.index(dst), src)
        self._on_reordered(order)

    def _remove_chip(self, symbol: str) -> None:
        """Right-click removes a searched chip outright (ladder-chip
        behavior).  Position chips are derived from the checked accounts
        and stay; the last remaining chip also stays."""
        if symbol not in self._custom or len(self._order) <= 1:
            self._rebuild_chips()      # grid may have dropped it visually
            return
        self._custom.remove(symbol)
        self._save_chips()
        if symbol == self._active:
            # Removing the active chip selects the first remaining one
            # and reloads the ticket.
            self._sync_order()
            self._active = self._order[0] if self._order else ""
            self._rebuild_chips()
            if self._active:
                self.symbol_selected.emit(self._active)
        else:
            self._rebuild_chips()


# ══════════════════════════════════════════════════════════════════════════════
#  StrategyCards
# ══════════════════════════════════════════════════════════════════════════════

class RightToggle(QWidget):
    """Call / Put segmented toggle — sits left of the EXPIRY chips."""

    right_changed = Signal(str)   # "C" | "P"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._right = "C"
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self._btns: dict[str, QPushButton] = {}
        for key, label in (("C", "Call"), ("P", "Put")):
            b = QPushButton(label, self)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=key: self._pick(k))
            self._btns[key] = b
            row.addWidget(b)
        self._restyle()

    def right(self) -> str:
        return self._right

    def _pick(self, key: str) -> None:
        if key == self._right:
            return
        self._right = key
        self._restyle()
        self.right_changed.emit(key)

    def _restyle(self) -> None:
        for k, b in self._btns.items():
            b.setStyleSheet(_symbol_chip_style(k == self._right))


# ══════════════════════════════════════════════════════════════════════════════
#  ExpirySelector
# ══════════════════════════════════════════════════════════════════════════════

class ExpirySelector(QWidget):
    """Four weekly chips + a ``More ▾`` dropdown (§4.3)."""

    expiry_changed = Signal(str)   # YYYYMMDD

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chips: list[Expiry] = []
        self._more: list[Expiry] = []
        self._selected: Expiry | None = None
        self._buttons: list[QPushButton] = []

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._chip_row = QHBoxLayout()
        self._chip_row.setSpacing(6)
        row.addLayout(self._chip_row)
        self._more_btn = QPushButton("More ▾", self)
        self._more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more_btn.clicked.connect(self._show_more_menu)
        row.addWidget(self._more_btn)
        row.addStretch(1)
        self._restyle()

    def reset_selection(self) -> None:
        """Forget the remembered expiry so the next :meth:`set_expiries`
        defaults to the nearest weekly chip (used on symbol change —
        the memory should only span reloads of the *same* symbol)."""
        self._selected = None

    def set_expiries(self, chips: list[Expiry], more: list[Expiry]) -> None:
        self._chips = list(chips)
        self._more = list(more)
        old = self._selected.ymd if self._selected else None
        self._selected = None
        for e in self._chips + self._more:
            if e.ymd == old:
                self._selected = e
                break
        if self._selected is None and self._chips:
            self._selected = self._chips[0]
        self._rebuild()

    def selected(self) -> Expiry | None:
        return self._selected

    def _rebuild(self) -> None:
        while self._chip_row.count():
            item = self._chip_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._buttons.clear()
        for e in self._chips:
            btn = QPushButton(self)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, x=e: self._pick(x))
            self._chip_row.addWidget(btn)
            self._buttons.append(btn)
        self._more_btn.setVisible(bool(self._more))
        self._restyle()

    def _pick(self, e: Expiry) -> None:
        if self._selected is not None and e.ymd == self._selected.ymd:
            return
        self._selected = e
        self._restyle()
        self.expiry_changed.emit(e.ymd)

    def _show_more_menu(self) -> None:
        if not self._more:
            return
        menu = QMenu(self)
        for e in self._more:
            act = QAction(e.long_label, menu)
            act.setCheckable(True)
            act.setChecked(self._selected is not None
                           and e.ymd == self._selected.ymd)
            act.triggered.connect(lambda _c=False, x=e: self._pick(x))
            menu.addAction(act)
        menu.exec(self._more_btn.mapToGlobal(
            self._more_btn.rect().bottomLeft()))

    def _restyle(self) -> None:
        # Solid-blue active / grey inactive — same look as the symbol
        # chips above (and the Book Trader / order-entry chip rows).
        sel = self._selected.ymd if self._selected else None
        for e, btn in zip(self._chips, self._buttons):
            btn.setText(f"{e.chip_label} {e.dte}d")
            btn.setStyleSheet(_symbol_chip_style(e.ymd == sel))
        # When a dropdown expiry is selected, surface it on the More button
        # so the selection stays visible.
        more_sel = (self._selected is not None
                    and self._selected.ymd not in
                    {c.ymd for c in self._chips})
        if more_sel:
            self._more_btn.setText(
                f"{self._selected.chip_label} {self._selected.dte}d ▾")
            self._more_btn.setStyleSheet(_symbol_chip_style(True))
        else:
            self._more_btn.setText("More ▾")
            self._more_btn.setStyleSheet(_symbol_chip_style(False))
