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

        _sym_font = QFont("Helvetica Neue, Arial", 12)
        _sym_font.setWeight(QFont.Weight.Bold)
        _price_font = QFont("Menlo, Consolas, monospace", 12)
        _price_font.setWeight(QFont.Weight.Bold)
        _ticker_font = QFont("Menlo, Consolas, monospace", 11)
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
        from src.core.reference_data import get_instance as _rd
        if _rd().get_primary_exchange(symbol) not in _TORONTO_EXCHANGES:
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

    Duplicated verbatim from order_entry_panel._apply_chip_style so both
    panels render chips identically.  Kept here to avoid a reverse import.
    """
    if selected:
        btn.setStyleSheet(
            "QPushButton{background:#0071e3;color:white;border:none;"
            "border-radius:6px;padding:4px 12px;font-weight:bold;font-size:12px}"
            "QPushButton:hover{background:#0077ed}")
    else:
        btn.setStyleSheet(
            "QPushButton{background:#3a3a4a;color:#ccc;border:none;"
            "border-radius:6px;padding:4px 12px;font-weight:600;font-size:12px}"
            "QPushButton:hover{background:#4a4a5a}")


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
    ) -> None:
        super().__init__(sym, parent)
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
    ) -> None:
        super().__init__(parent)
        self._columns = max(1, columns)
        self._max_rows_per_group = max(1, max_rows_per_group)
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

        cap = self._columns * self._max_rows_per_group
        stk = stk[:cap]
        fut = fut[:cap]

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
        btn = DraggableChip(sym, sec_type, self._on_drop)
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
        """Move *src_sym* immediately before *dst_sym* in the entry list
        and emit ``symbols_reordered`` with the new order.  Same-group
        enforcement is already handled by DraggableChip — we trust the
        callback payload here."""
        entries = list(self._entries)
        src_entry = next((e for e in entries if e[0] == src_sym), None)
        if src_entry is None:
            return
        entries.remove(src_entry)
        try:
            dst_idx = next(i for i, (s, _) in enumerate(entries) if s == dst_sym)
        except StopIteration:
            return
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
    reused directly so the popup-ladder behaviour is identical across
    panels.
    """

    qty_changed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._active_qty: int = 0
        self._default: int = 0
        self._presets: list[int] = []
        self._buttons: list[QPushButton] = []

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        # _QtyInput lazily imported to avoid a hard cycle at module load.
        from src.ui.widgets.order_entry_panel import _QtyInput  # noqa: WPS433
        self._custom = _QtyInput()
        self._custom.textChanged.connect(self._on_custom_text)
        self._custom.returnPressed.connect(self._on_custom_enter)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_presets(
        self,
        presets: Sequence[int],
        default: int,
        popup_vals: Sequence[int] | None = None,
    ) -> None:
        """Rebuild the preset chips with *presets*; *default* is the qty
        selected initially.  *popup_vals* seeds the _QtyInput's arrow-
        dropdown ladder."""
        # Clear current widgets from the layout
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget() if item else None
            if w is not None and w is not self._custom:
                w.deleteLater()
        self._buttons.clear()

        self._presets = list(presets)
        self._default = int(default)
        self._active_qty = self._default

        if popup_vals is not None:
            self._custom.set_presets(list(popup_vals))

        for v in self._presets:
            btn = QPushButton(str(v))
            btn.setFixedHeight(28)
            btn.setMinimumWidth(36)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            _apply_chip_style(btn, v == self._default)
            btn.clicked.connect(
                lambda _checked, q=v: self._on_preset(q))
            self._buttons.append(btn)
            self._layout.addWidget(btn)

        self._custom.blockSignals(True)
        self._custom.clear()
        self._custom.blockSignals(False)
        self._layout.addWidget(self._custom)

    def set_qty(self, qty: int) -> None:
        """Programmatically set the active qty (no signal emitted).  Used
        by the host to sync from an external source (e.g. keyboard shortcut)
        without triggering re-entrant order logic."""
        qty = int(qty)
        if qty <= 0:
            return
        self._active_qty = qty
        self._restyle(qty)

    def current_qty(self) -> int:
        return self._active_qty

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_preset(self, qty: int) -> None:
        self._active_qty = qty
        self._custom.blockSignals(True)
        self._custom.clear()
        self._custom.blockSignals(False)
        self._restyle(qty)
        self.qty_changed.emit(qty)

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
        self._active_qty = qty
        self._restyle(None)  # no preset is "current" when custom is active
        self.qty_changed.emit(qty)

    def _on_custom_enter(self) -> None:
        text = self._custom.text().strip()
        try:
            if int(text) > 0:
                return
        except ValueError:
            pass
        # Invalid on Enter → reset to default.
        self._custom.blockSignals(True)
        self._custom.clear()
        self._custom.blockSignals(False)
        self._active_qty = self._default
        self._restyle(self._default)
        self.qty_changed.emit(self._default)

    def _restyle(self, active: int | None) -> None:
        for btn in self._buttons:
            try:
                v = int(btn.text())
            except ValueError:
                v = None
            _apply_chip_style(btn, active is not None and v == active)
