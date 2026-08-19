"""Reusable widget building blocks shared between OrderEntryPanel and
BookTraderView.

Three self-contained widgets:

* ``TickerStrip``     — compact row (symbol, last, bid, ask, change, change%,
                         "+"-add-symbol button)
* ``SymbolChipBar``   — grid of symbol chips (STK rows on top, FUT rows
                         underneath), with left-click select and right-click
                         remove
* ``QtyChipRow``      — row of qty presets + styled integer input

Each widget owns its internal state (labels, chip buttons, active selection)
and emits Qt signals for the host.  They do **not** persist configuration,
subscribe to market data, or drive tier sections; those concerns live in the
host panel so the widgets stay reusable.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDrag, QFont, QMouseEvent
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui.theme import Theme
from src.utils.helpers import format_option_symbol_two_line
from src.ui.widgets.size_unit_parts import (
    PCT_DEFAULT,
    PCT_POPUP,
    PCT_PRESETS,
    UNIT_PCT,
    UNIT_SH,
    PercentSizingContext,
    apply_sized_chip_style,
    apply_unit_toggle_style,
    make_unit_toggle,
)


# ══════════════════════════════════════════════════════════════════════════════
#  TickerStrip
# ══════════════════════════════════════════════════════════════════════════════

# Symbols whose ticker-strip %change column is hidden — tiny daily % swings
# relative to the price magnitude make the percentage visually noisy on the
# CME index futures.  Absolute change is still shown.  Host can override via
# constructor kwarg.
DEFAULT_NO_PCT_CHANGE: frozenset[str] = frozenset({"NQ", "MNQ", "ES", "MES"})


class TickerStrip(QWidget):
    """Compact single-row ticker strip.

    Layout (left → right):
      [symbol]  [last]   [bid(green)]  [ask(red)]   [change]  [change%]   [+]

    Host calls ``set_symbol`` when the active symbol changes and
    ``update_price`` on every background price tick.  The "+" button
    emits ``add_symbol_clicked`` so the host can show its own
    symbol-search popup (kept on the host because it needs access to
    ``ReferenceData`` / persistence).
    """

    add_symbol_clicked = Signal()

    def __init__(
        self,
        parent=None,
        *,
        no_pct_change_symbols: Iterable[str] = DEFAULT_NO_PCT_CHANGE,
    ) -> None:
        super().__init__(parent)
        self._no_pct: set[str] = {s.upper() for s in no_pct_change_symbols}
        self._symbol: str = ""
        self._last: float = 0.0
        self._bid: float = 0.0
        self._ask: float = 0.0
        self._close: float = 0.0
        self._build_ui()

    def _build_ui(self) -> None:
        top_row = QHBoxLayout(self)
        top_row.setSpacing(6)
        top_row.setContentsMargins(0, 0, 0, 0)

        _sym_font = QFont("JetBrains Mono, Menlo, Consolas, monospace", 12)
        _sym_font.setWeight(QFont.Weight.Medium)
        _price_font = QFont("JetBrains Mono, Menlo, Consolas, monospace", 12)
        _price_font.setWeight(QFont.Weight.Bold)
        _ticker_font = QFont("JetBrains Mono, Menlo, Consolas, monospace", 11)
        _ticker_font.setWeight(QFont.Weight.Bold)

        self._lbl_symbol = QLabel("")
        self._lbl_symbol.setFont(_sym_font)
        self._lbl_symbol.setStyleSheet(
            f"color: {Theme.TEXT}; border: none;")

        self._lbl_price = QLabel("")
        self._lbl_price.setFont(_price_font)
        self._lbl_price.setStyleSheet(
            f"color: {Theme.TEXT}; border: none;")

        self._lbl_bid = QLabel("")
        self._lbl_bid.setFont(_ticker_font)
        self._lbl_bid.setStyleSheet(
            f"color: {Theme.CLR_BUY}; border: none;")

        self._lbl_ask = QLabel("")
        self._lbl_ask.setFont(_ticker_font)
        self._lbl_ask.setStyleSheet(
            f"color: {Theme.CLR_SELL}; border: none;")

        _dim_ss = f"color: {Theme.TEXT_DIM}; border: none;"
        self._lbl_change = QLabel("")
        self._lbl_change.setFont(_ticker_font)
        self._lbl_change.setStyleSheet(_dim_ss)

        self._lbl_change_pct = QLabel("")
        self._lbl_change_pct.setFont(_ticker_font)
        self._lbl_change_pct.setStyleSheet(_dim_ss)

        self._btn_add = QPushButton("+")
        self._btn_add.setFixedSize(22, 22)
        self._btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_add.setToolTip("Add symbol")
        self._btn_add.setStyleSheet(
            f"QPushButton{{background:{Theme.BG_INPUT};color:{Theme.TEXT};"
            f"border:1px solid {Theme.BORDER};border-radius:4px;"
            f"font-size:14px;font-weight:bold;padding:0}}"
            f"QPushButton:hover{{border-color:{Theme.ACCENT}}}")
        self._btn_add.clicked.connect(self.add_symbol_clicked)

        top_row.addWidget(self._lbl_symbol)
        top_row.addWidget(self._lbl_price)
        top_row.addSpacing(6)
        top_row.addWidget(self._lbl_bid)
        top_row.addWidget(self._lbl_ask)
        top_row.addSpacing(6)
        top_row.addWidget(self._lbl_change)
        top_row.addWidget(self._lbl_change_pct)
        top_row.addStretch(1)
        top_row.addWidget(self._btn_add)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_symbol(self, sym: str | None) -> None:
        """Update the symbol label; clears the price/bid/ask until the next
        ``update_price`` call.  Pass empty string / None to blank the row."""
        new_sym = (sym or "").strip()
        changed = new_sym != self._symbol
        self._symbol = new_sym
        self._lbl_symbol.setText(self._symbol)
        if changed:
            # Reset cached state so ``update_price``'s "preserve last valid"
            # logic can't carry the previous symbol's bid/ask into the new one.
            self._last = self._bid = self._ask = self._close = 0.0
            self._lbl_price.setText("")
            self._lbl_bid.setText("")
            self._lbl_ask.setText("")
        self._refresh_change()

    def update_price(self, last: float, bid: float, ask: float,
                     close: float = 0.0) -> None:
        """Push a fresh price tick.

        Positive values update; zero / None leaves the prior value in place
        (matches the existing OrderEntryPanel semantics where partial ticks
        don't blank the display).  Close is the prior-session close used
        for the day-change display; 0 leaves the cached value.
        """
        if last and last > 0:
            self._last = float(last)
            self._lbl_price.setText(f"{last:,.2f}")
        if bid and bid > 0:
            self._bid = float(bid)
            self._lbl_bid.setText(f"{bid:,.2f}")
        elif bid == 0 and self._bid == 0:
            self._lbl_bid.setText("")
        if ask and ask > 0:
            self._ask = float(ask)
            self._lbl_ask.setText(f"{ask:,.2f}")
        elif ask == 0 and self._ask == 0:
            self._lbl_ask.setText("")
        if close and close > 0:
            self._close = float(close)
        self._refresh_change()

    def clear(self) -> None:
        """Blank every label and reset cached prices."""
        self._symbol = ""
        self._last = self._bid = self._ask = self._close = 0.0
        self._lbl_symbol.setText("")
        self._lbl_price.setText("")
        self._lbl_bid.setText("")
        self._lbl_ask.setText("")
        self._lbl_change.setText("")
        self._lbl_change_pct.setText("")

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def last_price(self) -> float:
        return self._last

    # ── Internal ──────────────────────────────────────────────────────────────

    def _refresh_change(self) -> None:
        sym = self._symbol.upper()
        close = self._close
        last = self._last
        # Gate the change badges on the price label being populated.
        # If last has been reset (symbol switch, worker disconnect) but
        # a stale ``_last`` slipped through, we don't want the user to
        # see a change number floating over an empty price/bid/ask row.
        if (not sym or close <= 0 or last <= 0
                or not self._lbl_price.text()):
            self._lbl_change.setText("")
            self._lbl_change_pct.setText("")
            return
        change = last - close
        pct = (change / close) * 100.0 if close else 0.0
        sign = "+" if change > 0 else ""
        self._lbl_change.setText(f"{sign}{change:,.2f}")
        if sym in self._no_pct:
            self._lbl_change_pct.setText("")
        else:
            self._lbl_change_pct.setText(f"{sign}{pct:,.2f}%")
        if change > 0:
            colour = Theme.CLR_BUY
        elif change < 0:
            colour = Theme.CLR_SELL
        else:
            colour = Theme.TEXT_DIM
        # Restyle only when the colour flips — setStyleSheet forces a
        # full re-polish and this runs on every tick of the strip.
        if colour != getattr(self, "_change_colour", None):
            self._change_colour = colour
            ss = f"color: {colour}; border: none;"
            self._lbl_change.setStyleSheet(ss)
            self._lbl_change_pct.setStyleSheet(ss)


# ══════════════════════════════════════════════════════════════════════════════
#  SymbolChipBar
# ══════════════════════════════════════════════════════════════════════════════

# Primary-exchange codes that count as Toronto Stock Exchange.  IB
# reports Toronto as "TSE"; "TSX" kept as a defensive alias.
_TORONTO_EXCHANGES = frozenset({"TSE", "TSX"})


def attach_exchange_badge(btn: QPushButton, symbol: str) -> None:
    """Overlay a tiny superscript letter at the chip's top-right corner
    marking the stock's primary listing exchange — currently just "T"
    for Toronto (TSE) listings.  No-op for anything else.

    Uses a layout on the button itself so the letter tracks the corner
    through resizes without a custom paintEvent.
    """
    try:
        from src.core import instruments
        if instruments.get_primary_exchange(symbol) not in _TORONTO_EXCHANGES:
            return
    except Exception:
        return
    badge = QLabel("T", btn)
    badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    badge.setStyleSheet(
        "background:transparent;color:rgba(255,255,255,0.8);"
        "font-size:8px;font-weight:bold;border:none;padding:0")
    lay = QHBoxLayout(btn)
    lay.setContentsMargins(0, 1, 3, 0)
    lay.setSpacing(0)
    lay.addStretch(1)
    lay.addWidget(
        badge, 0,
        Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)


def _apply_chip_style(btn: QPushButton, selected: bool) -> None:
    """Apply blue-selected or grey-unselected style to a symbol chip.

    The single chip style for both the Order Entry and Book Trader
    panels (order_entry_panel imports this).

    Chips whose ``chip_small`` property is set (option chips showing a
    compact two-line "QQQ Aug7 ⏎ 670 P" label) get a smaller font and
    tighter padding so both lines fit the standard chip height.
    """
    if bool(btn.property("chip_small")):
        font, pad = "9px", "1px 4px"
    else:
        font, pad = "12px", "4px 12px"
    if bool(btn.property("chip_expired")):
        # Dead pinned future — dimmed with a dashed border so the user
        # notices and right-click-removes it.  Still clickable.
        btn.setStyleSheet(
            "QPushButton{background:#1B2130;color:#5A6478;"
            "border:1px dashed #334058;"
            f"border-radius:6px;padding:{pad};font-weight:500;"
            f"font-size:{font}}}"
            "QPushButton:hover{background:#232A3B}")
        return
    if selected:
        btn.setStyleSheet(
            "QPushButton{background:#539BF5;color:white;border:none;"
            f"border-radius:6px;padding:{pad};font-weight:600;font-size:{font}}}"
            "QPushButton:hover{background:#6BA8F6}")
    else:
        btn.setStyleSheet(
            "QPushButton{background:#232A3B;color:#A3AFC2;border:none;"
            f"border-radius:6px;padding:{pad};font-weight:500;font-size:{font}}}"
            "QPushButton:hover{background:#334058}")


CHIP_MIME_TYPE = "application/x-nanopulse-symbol"


class DraggableChip(QPushButton):
    """Symbol chip that can be dragged to another chip in the same group.

    The drag payload carries ``"{symbol}|{sec_type}"``; drop targets only
    accept a payload whose sec-type matches their own.  A user-supplied
    ``on_drop`` callback runs when a valid drop lands — the host uses it
    to reorder its backing list and rebuild the grid.
    """

    _DRAG_PX = 6   # pixels of movement before a drag is initiated

    def __init__(
        self,
        sym: str,
        sec_type: str,
        on_drop,
        parent=None,
        *,
        label: str | None = None,
    ) -> None:
        # *label* is display-only (e.g. a compact option label); the chip's
        # identity for drag payloads / dedupe / callbacks stays *sym*.
        super().__init__(label if label is not None else sym, parent)
        self._sym = sym
        self._sec_type = sec_type
        self._on_drop = on_drop
        self._press_pos = None
        self.setAcceptDrops(True)

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if (not (e.buttons() & Qt.MouseButton.LeftButton)
                or self._press_pos is None):
            super().mouseMoveEvent(e)
            return
        if ((e.position().toPoint() - self._press_pos).manhattanLength()
                < self._DRAG_PX):
            super().mouseMoveEvent(e)
            return

        mime = QMimeData()
        mime.setData(
            CHIP_MIME_TYPE,
            f"{self._sym}|{self._sec_type}".encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pm = self.grab()
        drag.setPixmap(pm)
        drag.setHotSpot(e.position().toPoint())
        # Clearing _press_pos suppresses a late-arriving click when the
        # cursor returns to this chip without a drop.
        self._press_pos = None
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, e) -> None:  # noqa: N802
        md = e.mimeData()
        if not md.hasFormat(CHIP_MIME_TYPE):
            e.ignore()
            return
        try:
            payload = bytes(md.data(CHIP_MIME_TYPE)).decode("utf-8")
            src_sym, src_sec = payload.split("|", 1)
        except (UnicodeDecodeError, ValueError):
            e.ignore()
            return
        # Same-group only, and ignore drops back on the source chip itself.
        if src_sec == self._sec_type and src_sym != self._sym:
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e) -> None:  # noqa: N802
        if e.mimeData().hasFormat(CHIP_MIME_TYPE):
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:  # noqa: N802
        md = e.mimeData()
        if not md.hasFormat(CHIP_MIME_TYPE):
            e.ignore()
            return
        try:
            payload = bytes(md.data(CHIP_MIME_TYPE)).decode("utf-8")
            src_sym, src_sec = payload.split("|", 1)
        except (UnicodeDecodeError, ValueError):
            e.ignore()
            return
        if src_sec != self._sec_type or src_sym == self._sym:
            e.ignore()
            return
        e.acceptProposedAction()
        if self._on_drop is not None:
            self._on_drop(src_sym, self._sym)


class SymbolChipBar(QWidget):
    """Grid of symbol chips, arranged STK rows on top, FUT rows underneath.

    Host calls ``set_symbols`` after loading the persisted symbol list; the
    widget rebuilds the chip grid.  Left-click a chip → ``symbol_selected``;
    right-click → ``symbol_remove_requested``.  Drag a chip onto another
    chip in the same sec-type group → ``symbols_reordered`` with the new
    symbol order.  The widget does NOT persist the list or trigger
    subscriptions; that's the host's job.
    """

    symbol_selected = Signal(str, str)       # (symbol, sec_type)
    symbol_remove_requested = Signal(str)    # symbol
    symbols_reordered = Signal(list)         # list[str] — new symbol order

    _CHIP_HEIGHT = 28

    def __init__(
        self,
        parent=None,
        *,
        columns: int = 5,
        max_rows_per_group: int = 2,
        max_stk_rows: int = 3,
    ) -> None:
        super().__init__(parent)
        self._columns = max(1, columns)
        self._max_rows_per_group = max(1, max_rows_per_group)
        # The STK group gets an extra row (3 × columns chips) — stock
        # lists grow faster than futures roots.  FUT keeps
        # ``max_rows_per_group``.
        self._max_stk_rows = max(1, max_stk_rows)
        self._entries: list[tuple[str, str]] = []  # (sym, sec_type)
        self._buttons: dict[str, QPushButton] = {}
        self._active: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._grid = QGridLayout()
        self._grid.setSpacing(4)
        self._grid.setContentsMargins(0, 0, 0, 0)
        inner = QWidget()
        inner.setLayout(self._grid)
        outer.addWidget(inner)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_symbols(self, entries: Sequence[tuple[str, str]]) -> None:
        """Rebuild the chip grid from *entries* — a sequence of
        ``(symbol, sec_type)`` tuples.  Sec-type ``"FUT"`` goes to the FUT
        row group; anything else (STK, OPT, unknown) goes to the STK group.
        Preserves the incoming order within each group.
        """
        self._entries = list(entries)
        self._rebuild_grid()

    def set_active(self, sym: str | None) -> None:
        """Highlight the chip for *sym*; pass None to clear the highlight."""
        self._active = sym or None
        for s, btn in self._buttons.items():
            _apply_chip_style(btn, s == self._active)

    def remove(self, sym: str) -> None:
        """Remove *sym* from the grid (host-driven, called after the host
        has updated its persisted symbol list)."""
        self._entries = [(s, sec) for (s, sec) in self._entries if s != sym]
        if self._active == sym:
            self._active = None
        self._rebuild_grid()

    @property
    def active_symbol(self) -> str | None:
        return self._active

    # ── Internal ──────────────────────────────────────────────────────────────

    def _rebuild_grid(self) -> None:
        # Clear grid
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget() if item else None
            if w is not None:
                w.deleteLater()
        self._buttons.clear()

        stk: list[tuple[str, str]] = []
        fut: list[tuple[str, str]] = []
        for sym, sec in self._entries:
            (fut if sec == "FUT" else stk).append((sym, sec))

        stk = stk[:self._columns * self._max_stk_rows]
        fut = fut[:self._columns * self._max_rows_per_group]

        row = 0
        for i, (sym, sec) in enumerate(stk):
            r = row + i // self._columns
            c = i % self._columns
            self._grid.addWidget(self._make_chip(sym, sec), r, c)
        if stk:
            row += (len(stk) - 1) // self._columns + 1

        for i, (sym, sec) in enumerate(fut):
            r = row + i // self._columns
            c = i % self._columns
            self._grid.addWidget(self._make_chip(sym, sec), r, c)

        # Apply active highlight after chips are built.
        self.set_active(self._active)

    def _make_chip(self, sym: str, sec_type: str) -> QPushButton:
        # OCC option symbols render a compact display label in a smaller
        # font — root + expiry over strike + right, the same split the
        # Positions/Trades/Orders tables use.
        # format_option_symbol_two_line passes every other symbol through
        # unchanged, so STK/FUT chips are unaffected.
        disp = format_option_symbol_two_line(sym)
        is_opt = disp != sym
        btn = DraggableChip(sym, sec_type, self._on_drop,
                            label=disp if is_opt else None)
        btn.setProperty("chip_small", is_opt)
        try:
            from src.core import instruments
            if instruments.is_expired_fut(sym):
                btn.setProperty("chip_expired", True)
                btn.setToolTip("Expired contract — right-click to remove")
        except Exception:
            pass
        btn.setFixedHeight(self._CHIP_HEIGHT)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        _apply_chip_style(btn, sym == self._active)
        attach_exchange_badge(btn, sym)
        btn.clicked.connect(lambda _checked, s=sym: self._on_click(s))
        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(
            lambda _pos, s=sym: self.symbol_remove_requested.emit(s))
        self._buttons[sym] = btn
        return btn

    def _on_click(self, sym: str) -> None:
        sec = next(
            (sec for (s, sec) in self._entries if s == sym), "")
        self.set_active(sym)
        self.symbol_selected.emit(sym, sec)

    def _on_drop(self, src_sym: str, dst_sym: str) -> None:
        """The dragged chip takes the drop target's position (same
        semantics as the account-group pills): dragging forward lands
        *after* the target, dragging backward lands *before* it — so a
        chip can reach any slot in its group, in any direction.
        Same-group enforcement is already handled by DraggableChip —
        we trust the callback payload here."""
        entries = list(self._entries)
        src_entry = next((e for e in entries if e[0] == src_sym), None)
        if src_entry is None:
            return
        try:
            # Target's index BEFORE removal: inserting there after the
            # pop lands after the target on forward drags (everything
            # shifted left by one) and before it on backward drags.
            dst_idx = next(i for i, (s, _) in enumerate(entries)
                           if s == dst_sym)
        except StopIteration:
            return
        entries.remove(src_entry)
        entries.insert(dst_idx, src_entry)
        self._entries = entries
        self._rebuild_grid()
        self.symbols_reordered.emit([s for (s, _) in entries])


# ══════════════════════════════════════════════════════════════════════════════
#  QtyChipRow
# ══════════════════════════════════════════════════════════════════════════════

class QtyChipRow(QWidget):
    """Horizontal row of quantity preset chips + custom integer input.

    Host calls ``set_presets`` on active-symbol change (FUT and STK use
    different preset sets); ``qty_changed`` fires whenever the active qty
    changes (either a preset click or a valid custom value).  The
    integer-input widget comes from ``order_entry_panel._QtyInput`` —
    reused directly so the wheel/keyboard ladder-stepping behaviour is
    identical across panels.

    With ``allow_percent=True`` (stocks only) the row grows an SH/%
    unit toggle (spec: NanoPulse-Ladder-Percent-Sizing.md).  Percent
    mode recolors the row gold and sizes as percent of account net
    liquidation, resolved through :class:`PercentSizingContext`.
    ``qty_changed`` still always carries **resolved shares** — in
    percent mode that is the conservative preview count — so every
    existing consumer (hover badge, tier sections) keeps pure share
    semantics.  The unit is never persisted: every ``set_presets``
    (launch / symbol switch) resets to shares.
    """

    qty_changed = Signal(int)        # resolved shares, both units
    # Per-side % readouts ("B 1,946", "S 150"); empty strings in share
    # mode or when a side doesn't resolve.
    preview_changed = Signal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._active_qty: int = 0
        self._default: int = 0
        self._presets: list[int] = []
        self._share_popup: list[int] = []
        self._buttons: list[QPushButton] = []

        self._unit: str = UNIT_SH
        self._allow_percent = False
        self._active_pct: int = PCT_DEFAULT
        self._pct_ctx = PercentSizingContext()
        # Per-unit memory (spec §3.2/3.3): selected chip and custom text
        # survive a toggle round-trip independently per unit.
        self._sel_by_unit: dict[str, int | None] = {}
        self._custom_by_unit: dict[str, str] = {}

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        # _QtyInput lazily imported to avoid a hard cycle at module load.
        from src.ui.widgets.order_entry_panel import _QtyInput  # noqa: WPS433
        self._qty_input_max = _QtyInput._MAX_DEFAULT
        self._custom = _QtyInput()
        self._custom.textChanged.connect(self._on_custom_text)
        self._custom.returnPressed.connect(self._on_custom_enter)
        self._custom.focusChanged.connect(self._on_custom_focus)
        self._btn_unit = make_unit_toggle()
        self._btn_unit.clicked.connect(self._on_unit_toggled)
        self._btn_unit.hide()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_presets(
        self,
        presets: Sequence[int],
        default: int,
        popup_vals: Sequence[int] | None = None,
        *,
        allow_percent: bool = False,
    ) -> None:
        """Rebuild the preset chips with *presets*; *default* is the qty
        selected initially.  *popup_vals* seeds the _QtyInput's
        wheel/keyboard stepping ladder.  *allow_percent* shows the SH/%
        toggle (host passes True only for stock symbols)."""
        # Clear current widgets from the layout.  The custom input AND
        # the unit toggle are long-lived — deleting the toggle here
        # would leave a dangling C++ object after the first symbol
        # switch.
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget() if item else None
            if w is not None and w is not self._custom \
                    and w is not self._btn_unit:
                w.deleteLater()
        self._buttons.clear()

        self._presets = list(presets)
        self._default = int(default)
        self._active_qty = self._default
        self._share_popup = list(popup_vals) if popup_vals is not None else []
        # % mode SURVIVES a stock→stock symbol switch (the pct snaps
        # back to the default); a non-percent symbol (FUT/OPT) forces
        # shares.  App launch starts in shares (initial _unit).
        stay_pct = bool(allow_percent) and self._unit == UNIT_PCT
        self._allow_percent = bool(allow_percent)
        self._unit = UNIT_PCT if stay_pct else UNIT_SH
        self._active_pct = PCT_DEFAULT
        self._sel_by_unit = {UNIT_SH: self._default, UNIT_PCT: PCT_DEFAULT}
        self._custom_by_unit = {UNIT_SH: "", UNIT_PCT: ""}

        if stay_pct:
            self._custom.set_presets(list(PCT_POPUP))
        elif popup_vals is not None:
            self._custom.set_presets(list(popup_vals))
        self._custom.set_max_value(
            100 if stay_pct else self._qty_input_max)
        self._custom.set_accent(Theme.CLR_PCT if stay_pct else None)
        self._custom.blockSignals(True)
        self._custom.clear()
        self._custom.blockSignals(False)

        if self._allow_percent:
            apply_unit_toggle_style(self._btn_unit, self._unit)
            self._btn_unit.show()
            self._layout.addWidget(self._btn_unit)
        else:
            self._btn_unit.hide()
        self._layout.addWidget(self._custom)
        self._build_chip_buttons()
        if stay_pct:
            # Refresh readouts/chip enables against the new symbol's
            # accounts + (not-yet-ticked) price immediately.
            self._emit_state()
        else:
            # Landed in share mode: re-emit so the header shows the
            # new symbol's SH total (default qty × accounts) instead
            # of the previous symbol's stale % readout.
            self._emit_preview()

    def set_percent_context(self, accounts_provider) -> None:
        """*accounts_provider(symbol) -> [(account, nlv, bp, held)]*."""
        self._pct_ctx.set_accounts_provider(accounts_provider)

    def set_price_provider(self, price_provider) -> None:
        """*price_provider() -> float* — last price of the active symbol."""
        self._pct_ctx.set_price_provider(price_provider)

    def set_symbol_provider(self, symbol_provider) -> None:
        """*symbol_provider() -> str* — the host's active symbol (the
        SELL side resolves against each account's holding of it)."""
        self._pct_ctx.set_symbol_provider(symbol_provider)

    def percent_side_qty(self, action: str) -> int:
        """Per-side conservative resolved count in % mode (BUY → NLV
        basis, SELL → position basis); 0 in share mode or unresolved."""
        if self._unit != UNIT_PCT:
            return 0
        return self._pct_ctx.side_qty(self._active_pct, action)

    def account_count(self) -> int:
        """Number of checked accounts the host's provider reports (the
        fan-out places one order per account, so share mode's true
        total = qty × this)."""
        return len(self._pct_ctx.accounts())

    def percent_side_total(self, action: str) -> int:
        """Per-side TOTAL resolved shares across all checked accounts
        in % mode; 0 in share mode or unresolved."""
        if self._unit != UNIT_PCT:
            return 0
        return (self._pct_ctx.sell_preview_total(self._active_pct)
                if action == "SELL"
                else self._pct_ctx.preview_total(self._active_pct))

    def refresh_percent_preview(self) -> None:
        """Re-resolve the B/S readout against fresh price/account data.
        Host calls this on price ticks, account selection changes and
        summary updates.  Share mode only re-emits the preview (its
        total tracks the checked-account count) — no restyle, no
        qty_changed."""
        if self._unit != UNIT_PCT:
            self._emit_preview()
            return
        self._restyle(self._current_chip())
        self._emit_state()

    def current_unit(self) -> str:
        return self._unit

    def current_percent(self) -> int | None:
        """The active percent in % mode, None in share mode."""
        return self._active_pct if self._unit == UNIT_PCT else None

    def set_qty(self, qty: int) -> None:
        """Programmatically set the active qty (no signal emitted).  Used
        by the host to sync from an external source (e.g. keyboard shortcut
        or a position-row click) without triggering re-entrant order logic.
        A preset value lights its chip; any other value lands in the
        custom Qty input so the number is visible.  Share mode only —
        percent mode owns its own state."""
        if self._unit == UNIT_PCT:
            return
        qty = int(qty)
        if qty <= 0:
            return
        self._active_qty = qty
        if qty in self._presets:
            # Mirror _on_preset minus the signal: chip lights, custom clears.
            self._custom.blockSignals(True)
            self._custom.clear()
            self._custom.blockSignals(False)
            self._custom_by_unit[self._unit] = ""
            self._sel_by_unit[self._unit] = qty
            self._restyle(qty)
        else:
            # Non-preset qty → show it in the custom Qty input.
            self._custom.blockSignals(True)
            self._custom.setText(str(qty))
            self._custom.blockSignals(False)
            self._custom_by_unit[self._unit] = str(qty)
            self._restyle(None)  # no chip is "current" while custom is active
        # No qty_changed (re-entrancy guard) — but the B/S readout must
        # track the new qty; preview_changed carries no order logic.
        self._emit_preview()

    def current_qty(self) -> int:
        """Resolved shares.  Percent mode resolves live via the context
        (conservative smallest-account preview; 0 when unresolvable)."""
        if self._unit == UNIT_PCT:
            return self._pct_ctx.preview_qty(self._active_pct)
        return self._active_qty

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_chip_buttons(self) -> None:
        """(Re)build the preset chips for the current unit, keeping the
        toggle at the head of the row and the custom input at the tail."""
        for btn in self._buttons:
            self._layout.removeWidget(btn)
            btn.deleteLater()
        self._buttons.clear()

        values = (list(PCT_PRESETS) if self._unit == UNIT_PCT
                  else self._presets)
        sel = self._current_chip()
        insert_at = self._layout.indexOf(self._custom)
        for v in values:
            # Plain numbers in both units — the gold row + gold toggle
            # carry the "this is percent" signal, not a % suffix.
            btn = QPushButton(str(v))
            btn.setProperty("qty_value", v)
            btn.setFixedHeight(28)
            btn.setMinimumWidth(36)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(
                lambda _checked, q=v: self._on_preset(q))
            self._buttons.append(btn)
            self._layout.insertWidget(insert_at, btn)
            insert_at += 1
        self._restyle(sel)

    def _current_chip(self) -> int | None:
        """The chip that should show selected: None while a custom value
        is active in the current unit."""
        if self._custom_by_unit.get(self._unit):
            return None
        return self._sel_by_unit.get(self._unit)

    def _emit_state(self) -> None:
        self.qty_changed.emit(self.current_qty())
        self._emit_preview()

    def _emit_preview(self) -> None:
        """Push the header B/S readout.  % mode: per-side resolved
        totals across the checked accounts.  Share mode: the TOTAL a
        click would transmit — active qty × checked-account count (the
        fan-out places one order per account), same number both sides.
        Empty strings (labels hidden) when nothing is resolvable."""
        if self._unit == UNIT_PCT:
            buy = self._pct_ctx.preview_total(self._active_pct)
            sell = self._pct_ctx.sell_preview_total(self._active_pct)
            self.preview_changed.emit(
                f"B {buy:,}" if buy > 0 else "",
                f"S {sell:,}" if sell > 0 else "")
        else:
            total = self.current_qty() * len(self._pct_ctx.accounts())
            text = f"{total:,}" if total > 0 else ""
            self.preview_changed.emit(
                f"B {text}" if text else "",
                f"S {text}" if text else "")

    def _on_unit_toggled(self) -> None:
        # Snapshot the outgoing unit's custom text, flip, restore the
        # incoming unit's remembered state (spec §3.3 — carrying one
        # number across the toggle would turn 750 shares into 750%).
        self._custom_by_unit[self._unit] = self._custom.text().strip()
        self._unit = UNIT_PCT if self._unit == UNIT_SH else UNIT_SH
        pct_mode = self._unit == UNIT_PCT
        apply_unit_toggle_style(self._btn_unit, self._unit)

        self._custom.set_max_value(100 if pct_mode else self._qty_input_max)
        self._custom.set_presets(
            list(PCT_POPUP) if pct_mode else list(self._share_popup))
        self._custom.set_accent(Theme.CLR_PCT if pct_mode else None)
        remembered = self._custom_by_unit.get(self._unit, "")
        self._custom.blockSignals(True)
        if remembered:
            self._custom.setText(remembered)
        else:
            self._custom.clear()
        self._custom.blockSignals(False)

        if pct_mode:
            self._active_pct = (int(remembered) if remembered
                                else self._sel_by_unit.get(UNIT_PCT)
                                or PCT_DEFAULT)
            self._active_pct = min(100, max(1, self._active_pct))
        else:
            self._active_qty = (int(remembered) if remembered
                                else self._sel_by_unit.get(UNIT_SH)
                                or self._default)

        self._build_chip_buttons()
        self._emit_state()

    def _on_preset(self, value: int) -> None:
        self._custom.blockSignals(True)
        self._custom.clear()
        self._custom.blockSignals(False)
        self._custom_by_unit[self._unit] = ""
        self._sel_by_unit[self._unit] = value
        if self._unit == UNIT_PCT:
            self._active_pct = value
        else:
            self._active_qty = value
        self._restyle(value)
        self._emit_state()

    def _on_custom_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        try:
            qty = int(text)
        except ValueError:
            return
        if qty <= 0:
            return
        self._custom_by_unit[self._unit] = text
        if self._unit == UNIT_PCT:
            self._active_pct = min(qty, 100)
        else:
            self._active_qty = qty
        self._restyle(None)  # no preset is "current" when custom is active
        self._emit_state()

    def _on_custom_focus(self, focused: bool) -> None:
        """Entering the custom field drops the chip highlight (the field
        is now the active input).  On the way out: a typed value that
        matches a preset is adopted BY the preset (chip lights up, field
        clears), and an empty field restores the previous highlight."""
        if focused:
            self._restyle(None)
            return
        self._adopt_matching_preset()
        if not self._custom.text().strip():
            self._restyle(self._current_chip())

    def _adopt_matching_preset(self) -> None:
        """Typed value == an existing chip → normalize onto the chip
        (same size either way; one selected element, not two)."""
        text = self._custom.text().strip()
        if not text:
            return
        try:
            v = int(text)
        except ValueError:
            return
        values = (list(PCT_PRESETS) if self._unit == UNIT_PCT
                  else self._presets)
        if v not in values:
            return
        self._custom.clear()
        self._custom_by_unit[self._unit] = ""
        self._sel_by_unit[self._unit] = v

    def _on_custom_enter(self) -> None:
        text = self._custom.text().strip()
        try:
            if int(text) > 0:
                return
        except ValueError:
            pass
        # Invalid on Enter → reset to the current unit's default.
        self._custom.blockSignals(True)
        self._custom.clear()
        self._custom.blockSignals(False)
        self._custom_by_unit[self._unit] = ""
        if self._unit == UNIT_PCT:
            self._active_pct = PCT_DEFAULT
            self._sel_by_unit[UNIT_PCT] = PCT_DEFAULT
            self._restyle(PCT_DEFAULT)
        else:
            self._active_qty = self._default
            self._sel_by_unit[UNIT_SH] = self._default
            self._restyle(self._default)
        self._emit_state()

    def _restyle(self, active: int | None) -> None:
        pct_mode = self._unit == UNIT_PCT
        for btn in self._buttons:
            v = btn.property("qty_value")
            enabled = True
            if pct_mode:
                # A chip stays clickable when EITHER side resolves —
                # buy needs NLV + live price, sell only needs a held
                # position (spec §4).
                enabled = self._pct_ctx.chip_enabled(int(v))
            apply_sized_chip_style(
                btn, active is not None and v == active,
                self._unit, enabled=enabled)
