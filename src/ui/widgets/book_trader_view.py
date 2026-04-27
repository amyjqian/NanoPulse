"""Book Trader (Depth-of-Market) panel.

Sits in the middle column of FlashTradeView and offers click-to-trade
against a live price ladder.  Layout top→bottom:

    ┌ TickerStrip (sym • last • bid • ask • chg • chg%)  [+] ┐
    ├ SymbolChipBar (grid of saved symbols)                  ┤
    ├ QtyChipRow (1 / 2 / 3 / 5 / [Qty])                     ┤
    ├ Toolbar (Recenter)                                     ┤
    ├ DOMLadderView (bid-size | price | ask-size)  ← click   ┤
    ├ Reverse / Flatten / Cancel                             ┤
    ├ Take Profit  (_TierSection)                            ┤
    ├ Stop Loss    (_TierSection)                            ┤
    └ Trail Stop   (_TierSection)                            ┘

Shared widgets live in ``order_panel_parts.py``; the price ladder lives
in ``dom_ladder.py``.  ``_TierSection`` is imported from
``order_entry_panel.py`` as-is.

Data in:
  * ``feed_price(symbol, last, bid, ask, close)`` — fed by each worker's
    ``bg_price_updated`` signal
  * ``feed_depth(symbol, bids, asks)`` — fed by each worker's new
    ``depth_updated`` signal (100 ms throttle)

Signals out (mirror OrderEntryPanel so FlashTradeView's existing
handlers route Book Trader clicks to the same worker methods):

  * ``order_requested(dict)``        — click on a ladder row (LMT)
  * ``cancel_all_requested(str)``    — Cancel button
  * ``flatten_requested(str)``       — Flatten button
  * ``reverse_requested(str)``       — Reverse button
  * ``close_tier_requested(dict)``   — TP / SL header-button close
  * ``active_symbol_changed(str)``
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core import config_manager
from src.ui.theme import Theme
from src.ui.widgets.dom_ladder import DOMLadderModel, DOMLadderView
from src.ui.widgets.order_entry_panel import (
    _make_btn,
    _sec_type_for,
    _TierSection,
)
from src.ui.widgets.order_panel_parts import (
    QtyChipRow,
    SymbolChipBar,
    TickerStrip,
)

log = logging.getLogger(__name__)

# Lock-button icons live alongside other UI assets in src/ui/icons/.
# Resolved relative to this file so the lookup works regardless of the
# process's current working directory.
_ICON_DIR = Path(__file__).resolve().parent.parent / "icons"
_LOCK_CLOSED_ICON = str(_ICON_DIR / "lock-closed.svg")
_LOCK_OPEN_ICON = str(_ICON_DIR / "lock-open.svg")

# Tick-aggregation dropdown presets, by security type.  The contract's
# native tick is preselected when it appears in the list; if it doesn't
# (e.g. an exotic 0.0001 product), we prepend it so the user always sees
# their real tick as one of the options.
_TICK_PRESETS: dict[str, list[float]] = {
    "FUT": [0.25, 0.5, 0.75, 1.0, 5.0, 10.0],
    "STK": [0.01, 0.05, 0.10, 0.5, 1.0, 5.0, 10.0],
    "OPT": [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 1.00, 5.00],
}


# Qty presets (same rule as OrderEntryPanel: different sets for FUT vs. STK).
_FUT_QTYS = [1, 2, 3, 5]
_STK_QTYS = [10, 25, 50, 100, 500]
_FUT_DEFAULT = 1
_STK_DEFAULT = 100
_FUT_QTY_POPUP: list[int] = list(range(1, 101))
_STK_QTY_POPUP: list[int] = (
    list(range(1, 11))
    + list(range(20, 110, 10))
    + list(range(200, 1100, 100))
    + list(range(1100, 10100, 100))
    + list(range(11000, 51000, 1000))
)


def _presets_for_sec(sec: str) -> tuple[list[int], int, list[int]]:
    """(presets, default, popup_ladder) for an asset-class string."""
    if sec == "FUT":
        return _FUT_QTYS, _FUT_DEFAULT, _FUT_QTY_POPUP
    return _STK_QTYS, _STK_DEFAULT, _STK_QTY_POPUP


# Known-futures per-symbol tick sizes — used as a fallback when a saved
# contract row was created before the Tick column existed (so it lacks
# ``tick_size`` / ``min_tick`` / ``minTick`` fields).  Without this
# table the FUT branch would always return the 0.25 default — wrong
# for the metals (GC/MGC = 0.1) and energies (CL/MCL = 0.01).  Mirror
# of the table in ``order_entry_panel._tick_size_for_symbol`` so both
# panels agree on the default tick.
_FUT_TICK: dict[str, float] = {
    "MNQ": 0.25, "NQ":  0.25,
    "MES": 0.25, "ES":  0.25,
    "M2K": 0.1,  "RTY": 0.1,
    "MYM": 1.0,  "YM":  1.0,
    "MGC": 0.1,  "GC":  0.1,
    "MCL": 0.01, "CL":  0.01,
}


def _tick_size_for_contract(cached: dict | None) -> float:
    """Minimum tick size for *cached* contract row, with sensible defaults.

    Lookup order:
      1. ``tick_size`` / ``min_tick`` / ``minTick`` field on the cached
         row (populated by ContractsView since the Tick column landed).
      2. Per-symbol :data:`_FUT_TICK` table — covers contracts saved
         before the Tick column existed, and ensures GC / MGC default
         to 0.1 and CL / MCL to 0.01 instead of the generic 0.25 FUT
         fallback.
      3. Coarse sec-type fallback: FUT → 0.25, everything else → 0.01.
    """
    if cached:
        for key in ("tick_size", "min_tick", "minTick"):
            v = cached.get(key)
            if v:
                try:
                    f = float(v)
                    if f > 0:
                        return f
                except (TypeError, ValueError):
                    pass
        # Per-symbol lookup table — match on root symbol first, then
        # local_symbol (e.g. ``GCM6``) which begins with the root.
        sym = str(cached.get("symbol", "") or "").upper().strip()
        if sym and sym in _FUT_TICK:
            return _FUT_TICK[sym]
        local = str(cached.get("local_symbol", "") or "").upper().strip()
        for root, t in _FUT_TICK.items():
            if local.startswith(root):
                return t
        sec = cached.get("sec_type", "")
        if sec == "FUT":
            return 0.25
    return 0.01


# ══════════════════════════════════════════════════════════════════════════════
#  BookTraderView
# ══════════════════════════════════════════════════════════════════════════════

class BookTraderView(QWidget):
    """Depth-of-Market panel — click a ladder row to submit a LMT."""

    order_requested = Signal(dict)
    cancel_all_requested = Signal(str)
    cancel_order_requested = Signal(str)   # order_id
    flatten_requested = Signal(str)
    reverse_requested = Signal(str)
    close_tier_requested = Signal(dict)
    active_symbol_changed = Signal(str)

    def __init__(
        self,
        active_threads: dict | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # Share MainWindow's dict by reference so entries added later
        # (when the user clicks Connect) are visible here too.
        # IMPORTANT: ``active_threads or {}`` would create a NEW dict when
        # an empty dict is passed, breaking the reference and leaving
        # this panel with no workers forever — use ``is not None``.
        self._active_threads: dict = (
            active_threads if active_threads is not None else {})

        # State
        self._symbols: list[str] = config_manager.load_book_trader_symbols()
        # Restore last-active symbol; fall back to first saved symbol.
        _saved_active = config_manager.load_book_trader_active_symbol()
        if _saved_active and _saved_active in self._symbols:
            self._active_symbol: str | None = _saved_active
        else:
            self._active_symbol = self._symbols[0] if self._symbols else None
        self._active_sec_type: str = (
            _sec_type_for(self._active_symbol) if self._active_symbol else "")
        self._active_qty: int = (
            _FUT_DEFAULT if self._active_sec_type == "FUT" else _STK_DEFAULT)
        # In-memory cache of TP / SL / TS selections per symbol —
        # mirrors the OrderEntryPanel cache.  Lives only for the
        # session; not persisted to JSON, so a fresh launch always
        # starts each symbol unarmed.
        self._tier_state_by_symbol: dict[str, dict] = {}
        self._last_price: float = 0.0
        # L1 fallback state — set False when TWS rejects the depth
        # subscription (no L2 entitlement, code 10090/10167).  While
        # False, we render a single-level "book" synthesised from
        # bg_price_updated + bg_sizes_updated, mirroring what TWS does
        # for stocks without the L2 booster.  Reset to True on every
        # symbol switch so a new symbol gets a fresh depth attempt.
        self._l2_ok: bool = True
        # Flipped True on the first non-empty ``feed_depth`` — used to
        # drive L1 warmup rendering.  While False (no L2 has been seen
        # yet for the active symbol) we synthesise the ladder from L1
        # data so the user sees bid/ask immediately.  Covers the case
        # where TWS silently accepts the depth sub but sends no ticks
        # (common on paper accounts / no-entitlement stocks), which
        # never triggers ``market_data_failed``.
        self._l2_seen: bool = False
        self._last_bid: float = 0.0
        self._last_ask: float = 0.0
        self._last_bid_size: float = 0.0
        self._last_ask_size: float = 0.0
        self._last_trade_size: float = 0.0
        self._bg_worker = None   # single worker for L2 subscription
        # Live open orders across every connected worker, keyed by
        # order_id.  Used to render order-badges on the ladder for the
        # currently active symbol.
        self._live_orders: dict[str, dict] = {}
        # Workers whose ``order_status_updated`` signal is already
        # connected to our slot.  Tracked so we don't double-connect on
        # repeat ``_wire_order_status`` calls.
        self._order_status_wired: set = set()

        # Ladder model/view (built first so _reload_tier_sections can call it).
        # ``rows_each_side`` defaults to 20 → 41-row ladder; the view scrolls
        # vertically when the panel isn't tall enough to show all rows.
        tick = _tick_size_for_contract(self._active_contract_cached())
        self._model = DOMLadderModel(tick_size=tick)

        self._setup_ui()
        self._wire_internals()

        # Push the initial symbol into the children.
        if self._active_symbol:
            self._ticker.set_symbol(self._active_symbol)
            self._chip_bar.set_active(self._active_symbol)
        self._reload_tier_sections()

        # Subscribe depth on the first connected worker, if any.
        self._ensure_bg_worker()
        if self._bg_worker is not None and self._active_symbol:
            self._bg_worker.subscribe_depth(self._active_symbol)

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setObjectName("BookTraderView")
        self.setStyleSheet(f"""
            QWidget#BookTraderView {{
                background: {Theme.BG_PANEL};
                border: 1px solid {Theme.BORDER};
                border-radius: 4px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)

        # ── Ticker strip ──────────────────────────────────────────────────────
        self._ticker = TickerStrip()
        root.addWidget(self._ticker)

        # ── Symbol chip bar ───────────────────────────────────────────────────
        self._chip_bar = SymbolChipBar(columns=5, max_rows_per_group=2)
        self._chip_bar.set_symbols(
            [(s, _sec_type_for(s)) for s in self._symbols])
        root.addWidget(self._chip_bar)

        root.addSpacing(12)

        # ── Qty chip row ──────────────────────────────────────────────────────
        self._qty_row = QtyChipRow()
        presets, default, popup = _presets_for_sec(self._active_sec_type)
        self._qty_row.set_presets(presets, default, popup)
        self._active_qty = default
        root.addWidget(self._qty_row)

        # ── Pre-build TP / SL / Trail tier sections so the ARMED strip
        # below can subscribe to their ``changed`` signals.  The widgets
        # are added to the root layout later, at their normal position
        # near the bottom of the panel.
        self._pt_section = _TierSection(
            "Take Profit", [], label_color=Theme.CLR_BUY,
            short_title="TP")
        self._sl_section = _TierSection(
            "Stop Loss", [], label_color=Theme.CLR_SELL,
            short_title="SL")
        self._tr_section = _TierSection(
            "Trailing", [], label_color=Theme.CLR_FLATTEN,
            short_title="TS")

        root.addSpacing(8)

        # ── Ladder header row: "BID SZ" | tick combo | "ASK SZ" ──────────────
        # Persistent in-widget header (not a Qt horizontal header) so the
        # tick-aggregation dropdown can sit centred between the two size
        # columns, matching the Book Trader mockup.  Double-clicking the
        # "BID SZ" label recentres the ladder on the last price (poor-
        # man's replacement for an explicit Recenter button).
        from PySide6.QtWidgets import QComboBox
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(0)

        _hdr_ss = (
            f"color: {Theme.TEXT_DIM}; border: none; "
            f"font-size: 10px; font-family: 'Helvetica Neue', Arial; "
            f"font-weight: bold;"
        )
        self._lbl_bid_hdr = QLabel("BID<br>SZ")
        self._lbl_bid_hdr.setTextFormat(Qt.TextFormat.RichText)
        self._lbl_bid_hdr.setStyleSheet(_hdr_ss)
        self._lbl_bid_hdr.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._lbl_bid_hdr.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lbl_bid_hdr.setToolTip("Double-click to recentre ladder")
        self._lbl_bid_hdr.mouseDoubleClickEvent = (  # type: ignore[method-assign]
            lambda _ev: self._on_recenter_clicked())

        self._cmb_tick = QComboBox()
        self._cmb_tick.setFixedHeight(24)
        self._cmb_tick.setMaximumWidth(86)
        self._cmb_tick.setStyleSheet(
            f"QComboBox{{background:{Theme.BG_INPUT};color:{Theme.TEXT};"
            f"border:1px solid {Theme.BORDER};border-radius:3px;"
            f"padding:1px 6px;font-size:11px}}"
            f"QComboBox:hover{{border-color:{Theme.ACCENT}}}"
            f"QComboBox QAbstractItemView{{background:{Theme.BG_INPUT};"
            f"color:{Theme.TEXT};selection-background-color:#3a3f50}}")
        self._cmb_tick.currentTextChanged.connect(self._on_tick_changed)

        # Lock button — when locked (default) the ladder auto-scrolls so
        # the last-price row stays centred in the viewport on every
        # tick.  Unlock to scroll the ladder manually without the view
        # yanking back to the centre.  Closed-padlock SVG when locked,
        # open-padlock SVG when unlocked (see _on_lock_toggled).
        self._icon_lock_closed = QIcon(_LOCK_CLOSED_ICON)
        self._icon_lock_open = QIcon(_LOCK_OPEN_ICON)
        self._btn_lock = QPushButton()
        self._btn_lock.setIcon(self._icon_lock_closed)
        self._btn_lock.setIconSize(QSize(16, 16))
        self._btn_lock.setCheckable(True)
        self._btn_lock.setChecked(True)
        self._btn_lock.setFixedSize(28, 24)
        self._btn_lock.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_lock.setToolTip(
            "Lock ladder to last price — auto-scroll keeps the last "
            "traded price centred.  Click to release.")
        self._btn_lock.setStyleSheet(
            f"QPushButton{{background:{Theme.BG_INPUT};color:{Theme.TEXT};"
            f"border:1px solid {Theme.BORDER};border-radius:3px;"
            f"padding:0}}"
            f"QPushButton:hover{{border-color:{Theme.ACCENT}}}"
            f"QPushButton:checked{{background:#3a3f50;"
            f"border-color:{Theme.ACCENT}}}")
        self._btn_lock.toggled.connect(self._on_lock_toggled)

        self._lbl_ask_hdr = QLabel("ASK<br>SZ")
        self._lbl_ask_hdr.setTextFormat(Qt.TextFormat.RichText)
        self._lbl_ask_hdr.setStyleSheet(_hdr_ss)
        self._lbl_ask_hdr.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)

        # Centre cell groups the tick-aggregation combo and the lock
        # button side-by-side under a single stretch slot; outer cells
        # hold the BID SZ / ASK SZ labels.
        centre_box = QHBoxLayout()
        centre_box.setContentsMargins(0, 0, 0, 0)
        centre_box.setSpacing(4)
        centre_box.addWidget(self._btn_lock)
        centre_box.addWidget(self._cmb_tick)
        hdr.addWidget(self._lbl_bid_hdr, stretch=1)
        hdr.addLayout(centre_box, stretch=1)
        hdr.addWidget(self._lbl_ask_hdr, stretch=1)
        root.addLayout(hdr)

        # ── DOM ladder ────────────────────────────────────────────────────────
        self._ladder = DOMLadderView(model=self._model)
        root.addWidget(self._ladder, stretch=1)
        # Hover badge content — ``Buy {qty}`` / ``Sell {qty}`` plus a
        # second line listing armed TP / SL / TS amounts so the trader
        # sees exactly what a click would submit.
        self._ladder.set_qty_provider(lambda: self._active_qty)
        self._ladder.set_armed_summary_provider(self._format_attached_summary)
        # Populate tick combo for the initial contract.
        self._populate_tick_combo()

        # ── Reverse / Flatten / Cancel ────────────────────────────────────────
        pm_row = QHBoxLayout()
        pm_row.setContentsMargins(0, 0, 0, 0)
        pm_row.setSpacing(4)
        self._btn_reverse = _make_btn("Reverse", Theme.CLR_REVERSE, 30)
        self._btn_flatten = _make_btn("Flatten", Theme.CLR_FLATTEN, 30)
        self._btn_cancel = _make_btn("Cancel", Theme.CLR_CANCEL, 30)
        pm_row.addWidget(self._btn_reverse)
        pm_row.addWidget(self._btn_flatten)
        pm_row.addWidget(self._btn_cancel)
        root.addLayout(pm_row)

        # ── TP / SL / Trail tier sections ─────────────────────────────────────
        # The widgets themselves were created earlier (so the ARMED
        # strip above could connect to their ``changed`` signal); here
        # we just lay them out at the bottom of the root column.
        root.addWidget(self._pt_section)
        root.addWidget(self._sl_section)
        root.addWidget(self._tr_section)

    def _wire_internals(self) -> None:
        # Ticker strip "+" button → our searchable add-symbol popup.
        self._ticker.add_symbol_clicked.connect(self._show_add_symbol_popup)

        # Symbol chip clicks / removes / drag-reorder.
        self._chip_bar.symbol_selected.connect(self._on_symbol_selected)
        self._chip_bar.symbol_remove_requested.connect(self._on_symbol_remove)
        self._chip_bar.symbols_reordered.connect(self._on_symbols_reordered)

        # Qty changes → sync tier sections.
        self._qty_row.qty_changed.connect(self._on_qty_changed)

        # Ladder click → LMT order.
        self._ladder.ladder_clicked.connect(self._on_ladder_clicked)

        # Action buttons.
        self._btn_reverse.clicked.connect(
            lambda: self._active_symbol and self.reverse_requested.emit(
                self._active_symbol))
        self._btn_flatten.clicked.connect(
            lambda: self._active_symbol and self.flatten_requested.emit(
                self._active_symbol))
        self._btn_cancel.clicked.connect(
            lambda: self._active_symbol and self.cancel_all_requested.emit(
                self._active_symbol))

        # Tier-section header-button submit → close-tier request.
        self._pt_section.submit_requested.connect(
            lambda: self._emit_close_tier("tp"))
        self._sl_section.submit_requested.connect(
            lambda: self._emit_close_tier("sl"))

    # ── Public data-in methods (called from FlashTradeView) ──────────────────

    def feed_price(
        self,
        symbol: str,
        last: float,
        bid: float,
        ask: float,
        close: float = 0.0,
    ) -> None:
        """Receive a background price tick.  Gated on active symbol.

        The first positive last-price after an active-symbol switch
        recentres the ladder on it (warm-start the view with data
        instead of a blank grid).
        """
        if not self._active_symbol or symbol.upper() != self._active_symbol.upper():
            return
        if self._last_price <= 0:
            log.info("BookTrader: first price tick for %s — last=%s bid=%s "
                     "ask=%s close=%s", symbol, last, bid, ask, close)
        first_tick = self._last_price <= 0 and last > 0
        if last and last > 0:
            self._last_price = float(last)
            # Sync tier-section "market price" so the TP/SL Dist input
            # ladder renders tick steps from the real market.
            tick = self._model.tick_size
            for section in (self._pt_section, self._sl_section,
                            self._tr_section):
                section.set_market_price(float(last), tick)
        if bid and bid > 0:
            self._last_bid = float(bid)
        if ask and ask > 0:
            self._last_ask = float(ask)
        self._ticker.update_price(last, bid, ask, close)
        if first_tick:
            self._model.recenter(float(last))
        if last and last > 0:
            self._model.feed_last(float(last))
            # Auto-scroll: when locked, keep the last-price row centred
            # in the viewport as the market moves.  When unlocked this
            # is a no-op — user's scroll position is preserved.
            self._ladder.sync_to_last()
        # While in L1-fallback mode (no L2 entitlement) OR before the
        # first real L2 snapshot has arrived, reconstruct the ladder
        # from L1 bid/ask so the user sees quotes immediately.  Once
        # ``_l2_seen`` flips True the real depth takes over.
        if not self._l2_ok or not self._l2_seen:
            self._render_l1_depth()

    def feed_depth(
        self,
        symbol: str,
        bids: list,
        asks: list,
    ) -> None:
        """Receive a depth snapshot.  Gated on active symbol."""
        if not self._active_symbol or symbol.upper() != self._active_symbol.upper():
            return
        has_data = bool(bids or asks)
        # Empty snapshots before the first real L2 frame would wipe the
        # L1 warmup render (``_l2_seen`` is still False, user would see
        # the bid/ask flash on then disappear).  Ignore them — they
        # happen when the worker marks the depth ticker dirty on any
        # tick touch, not just real L2 updates.
        if not has_data and not self._l2_seen:
            return
        if not self._model._bid and not self._model._ask and has_data:
            log.info("BookTrader: first depth snapshot for %s — "
                     "%d bid levels, %d ask levels",
                     symbol, len(bids or []), len(asks or []))
        if has_data:
            # Real L2 data has arrived — stop the L1 warmup render so
            # the ladder shows true depth going forward.
            self._l2_seen = True
        self._model.feed_depth(bids or [], asks or [])

    def feed_sizes(
        self,
        symbol: str,
        bid_size: float,
        ask_size: float,
        last_size: float,
    ) -> None:
        """Receive a size-tick.  Caches the three sizes, feeds the
        last-trade stripe on every positive ``last_size``, and when the
        active symbol has no L2 entitlement synthesises a single-level
        depth snapshot from L1 bid/ask + these sizes (the TWS "no-L2"
        look)."""
        if not self._active_symbol or symbol.upper() != self._active_symbol.upper():
            return
        if bid_size > 0:
            self._last_bid_size = float(bid_size)
        if ask_size > 0:
            self._last_ask_size = float(ask_size)
        if last_size > 0 and self._last_price > 0:
            self._last_trade_size = float(last_size)
            self._model.feed_last_trade(self._last_price, float(last_size))
        if not self._l2_ok or not self._l2_seen:
            self._render_l1_depth()

    def on_market_data_failed(self, symbol: str) -> None:
        """TWS rejected a market-data request for *symbol*.  When the
        rejection matches the active symbol we flip into L1-fallback
        rendering and draw what we can from bg_price_updated +
        bg_sizes_updated alone."""
        if self._active_symbol and symbol.upper() == self._active_symbol.upper():
            if self._l2_ok:
                log.info("BookTrader: L2 unavailable for %s — falling "
                         "back to L1 ladder", symbol)
            self._l2_ok = False
            self._render_l1_depth()

    def _render_l1_depth(self) -> None:
        """Synthesise a single-level depth snapshot from cached L1
        bid/ask + sizes.  Called whenever we're in L1-fallback mode and
        new price or size data arrives.  Empty sides are fine — the
        model paints nothing for a zero-row book."""
        bids: list[tuple[float, float]] = []
        asks: list[tuple[float, float]] = []
        if self._last_bid > 0 and self._last_bid_size > 0:
            bids.append((self._last_bid, self._last_bid_size))
        if self._last_ask > 0 and self._last_ask_size > 0:
            asks.append((self._last_ask, self._last_ask_size))
        self._model.feed_depth(bids, asks)

    def _on_order_status(self, data: dict) -> None:
        """Track every worker's order status updates so we can render
        badges on the ladder for open orders at the active symbol.

        Only LMT, STP and STP LMT orders show a badge — TRAIL uses an
        offset (no absolute price on the ladder grid) and MKT fills
        instantly.  Orders in terminal states (Filled / Cancelled /
        Inactive / ApiCancelled) are dropped from ``_live_orders``.
        """
        if not isinstance(data, dict):
            return
        order_id = data.get("order_id", "")
        if not order_id:
            return
        status = data.get("status", "")
        terminal = status in (
            "Filled", "Completed", "Cancelled", "ApiCancelled", "Inactive")
        if terminal:
            self._live_orders.pop(order_id, None)
        else:
            self._live_orders[order_id] = dict(data)
        self._refresh_ladder_orders()

    def _refresh_ladder_orders(self) -> None:
        """Project ``_live_orders`` into a list the model can render."""
        if not self._active_symbol:
            self._model.set_orders([])
            return
        active_u = self._active_symbol.upper()
        # Pre-compute the set of acceptable symbol strings for a match
        # — the worker emits either the bare symbol (STK) or the local
        # symbol (FUT, e.g. "MNQM6").  We accept either.
        accepted: set[str] = {active_u}
        from src.core.reference_data import get_instance as _rd
        for c in _rd().contracts_by_symbol.get(active_u, []):
            local = (c.get("local_symbol") or "").upper()
            if local:
                accepted.add(local)

        markers: list[dict] = []
        for od in self._live_orders.values():
            sym = (od.get("symbol") or "").upper()
            if sym not in accepted:
                continue
            otype = (od.get("order_type") or "").upper()
            if otype == "LMT":
                price_str = od.get("limit_price")
            elif otype in ("STP", "STP LMT"):
                price_str = od.get("stop_price")
            else:
                continue   # MKT / TRAIL / etc. have no ladder price
            try:
                price = float(price_str)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            action = (od.get("action") or "").upper()
            if action not in ("BUY", "SELL"):
                continue
            try:
                qty = int(od.get("quantity") or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                continue
            markers.append({
                "action": action,
                "qty": qty,
                "price": price,
                "type": otype,
            })
        self._model.set_orders(markers)

    # ── Internal slots ────────────────────────────────────────────────────────

    def _on_symbol_selected(self, sym: str, sec_type: str) -> None:
        """User clicked a chip — switch the active symbol."""
        if not sym:
            return
        if sym == self._active_symbol:
            # Clicking the already-active chip — the symbol isn't
            # changing, but refresh subscriptions anyway in case this
            # is the first chip click after workers came online
            # (covers the cold-start window where _ensure_bg_worker
            # initially found no workers).
            self.refresh_subscriptions()
            return
        # Snapshot the outgoing symbol's TP / SL / TS state before we
        # blow it away by reloading tier sections for the new symbol.
        self._capture_tier_state(self._active_symbol)
        old = self._active_symbol
        self._active_symbol = sym
        self._active_sec_type = sec_type or _sec_type_for(sym)
        config_manager.save_book_trader_active_symbol(sym)
        self._ticker.set_symbol(sym)
        self._chip_bar.set_active(sym)
        presets, default, popup = _presets_for_sec(self._active_sec_type)
        self._qty_row.set_presets(presets, default, popup)
        self._active_qty = default
        # New symbol → reset last price and ladder centre; real data
        # will arrive on the next tick.
        self._last_price = 0.0
        # New symbol gets a fresh L2 attempt — if it turns out not to
        # have depth entitlement, ``on_market_data_failed`` will flip
        # this back to False and the ladder falls back to L1 rendering.
        self._l2_ok = True
        self._l2_seen = False
        self._last_bid = 0.0
        self._last_ask = 0.0
        self._last_bid_size = 0.0
        self._last_ask_size = 0.0
        self._last_trade_size = 0.0
        self._model.set_tick_size(_tick_size_for_contract(
            self._active_contract_cached()))
        self._model.recenter(0.0)
        self._populate_tick_combo()
        self._reload_tier_sections()
        # Restore (or reset) the new symbol's previously-stored arming.
        self._restore_tier_state(sym)

        # Swap depth subscription on the bg worker.  L1 is owned by
        # OrderEntryPanel (which subscribes every saved chip-bar
        # symbol); Book Trader only takes over the L2 stream for the
        # active symbol.
        self._ensure_bg_worker()
        if self._bg_worker is not None:
            if old:
                self._bg_worker.unsubscribe_depth(old)
            self._bg_worker.subscribe_depth(sym)

        # Different symbol → different subset of live orders is
        # relevant.  Re-project the ladder markers.
        self._refresh_ladder_orders()

        self.active_symbol_changed.emit(sym)

    def _on_symbols_reordered(self, new_order: list[str]) -> None:
        """Chip bar finished a drag-reorder — sync our cached list and
        persist.  Same-group enforcement is handled by the chip widget."""
        # Preserve any symbols not present in the reordered list (defensive;
        # the chip bar should always emit the full list).
        self._symbols = list(new_order) + [
            s for s in self._symbols if s not in new_order]
        config_manager.save_book_trader_symbols(self._symbols)

    def _on_symbol_remove(self, sym: str) -> None:
        """Right-click on a chip — remove it from the bar (and the
        persisted config)."""
        if sym not in self._symbols:
            return
        self._symbols.remove(sym)
        config_manager.save_book_trader_symbols(self._symbols)
        self._chip_bar.set_symbols(
            [(s, _sec_type_for(s)) for s in self._symbols])
        if self._active_symbol == sym:
            # Pick a new active if available.
            new_active = self._symbols[0] if self._symbols else None
            if new_active is not None:
                self._on_symbol_selected(
                    new_active, _sec_type_for(new_active))
            else:
                self._active_symbol = None
                config_manager.save_book_trader_active_symbol("")
                self._ticker.clear()

    def _on_qty_changed(self, qty: int) -> None:
        self._active_qty = int(qty)
        for section in (self._pt_section, self._sl_section,
                        self._tr_section):
            section.set_qty(self._active_qty)

    def _format_attached_summary(self) -> str:
        """Return a one-line summary of the currently-armed brackets,
        e.g. ``"TP $40, SL $80, TS 50t"``.  Empty string when no tier
        is armed.  Reused by :class:`DOMLadderView`'s hover badge so
        the trader sees which brackets a ladder click would attach.
        """
        # Reuse the helpers from OrderEntryPanel — they already do the
        # tier-section → dollar-amount math the action buttons rely on.
        from src.ui.widgets.order_entry_panel import OrderEntryPanel as _OEP
        parts: list[str] = []
        tp = _OEP._tier_amount(self._pt_section)
        if tp is not None:
            parts.append(f"TP ${tp:,}")
        sl = _OEP._tier_amount(self._sl_section)
        if sl is not None:
            parts.append(f"SL ${sl:,}")
        tr_text = _OEP._trail_text(self._tr_section)
        if tr_text is not None:
            parts.append(f"TS {tr_text}")
        return ", ".join(parts)

    def _on_recenter_clicked(self) -> None:
        if self._last_price > 0:
            self._model.recenter(self._last_price)

    def _on_lock_toggled(self, locked: bool) -> None:
        """Lock / unlock the ladder's 'keep last price centred' tracker.

        The button icon flips between closed-padlock (locked) and
        open-padlock (unlocked) so the affordance matches the state.
        """
        self._btn_lock.setIcon(
            self._icon_lock_closed if locked else self._icon_lock_open)
        self._ladder.set_locked(locked)

    def _populate_tick_combo(self) -> None:
        """Fill the tick-aggregation combo with sec-type-appropriate
        aggregation steps from :data:`_TICK_PRESETS`.  The contract's
        native tick is preselected; if it isn't already a preset, it's
        prepended so the user always sees the real tick on the list.
        """
        tick = _tick_size_for_contract(self._active_contract_cached())
        sec = (self._active_sec_type
               or _sec_type_for(self._active_symbol or ""))
        presets = list(_TICK_PRESETS.get(sec, _TICK_PRESETS["STK"]))
        if tick > 0 and not any(abs(p - tick) < 1e-9 for p in presets):
            presets = [tick] + presets
        self._cmb_tick.blockSignals(True)
        self._cmb_tick.clear()
        for t in presets:
            self._cmb_tick.addItem(f"{t:g}")
        self._cmb_tick.setCurrentText(f"{tick:g}")
        self._cmb_tick.blockSignals(False)

    def _on_tick_changed(self, text: str) -> None:
        try:
            t = float(text)
        except ValueError:
            return
        if t <= 0 or t == self._model.tick_size:
            return
        self._model.set_tick_size(t)
        # A tick change wipes depth state — force a re-subscribe so the
        # next emit rebuilds sizes against the new grid.
        self._ensure_bg_worker()
        if self._bg_worker is not None and self._active_symbol:
            self._bg_worker.unsubscribe_depth(self._active_symbol)
            self._bg_worker.subscribe_depth(self._active_symbol)

    def _on_ladder_clicked(self, action: str, price: float) -> None:
        """Click on a bid-size or ask-size cell.

        Behaviour mirrors pro DOM ladders:
        * Click on a price where the user has no open same-side LMT →
          place a new LMT at that price.
        * Click on a price where the user already has a same-side LMT →
          cancel that resting order (toggle off).
        * Click on a different price while a same-side LMT is resting
          → cancel the old order and place a new one at the click
          (move the order).

        Only plain LMT orders are considered — STP / STP LMT protective
        orders are left alone so a casual click doesn't drop a stop.
        """
        if not self._active_symbol or price <= 0:
            return
        tick = self._model.tick_size
        tol = (tick / 2) if tick > 0 else 0.01

        # Match the active symbol against both bare and local symbols
        # (futures' order blotter uses the local symbol, e.g. "MNQM6").
        accepted: set[str] = {self._active_symbol.upper()}
        try:
            from src.core.reference_data import get_instance as _rd
            for c in _rd().contracts_by_symbol.get(
                    self._active_symbol.upper(), []):
                local = (c.get("local_symbol") or "").upper()
                if local:
                    accepted.add(local)
        except Exception:
            pass

        # Collect resting same-side LMTs for this symbol — keeping
        # (order_id, price) so we can match against the click.
        same_side_lmt: list[tuple[str, float]] = []
        for oid, od in self._live_orders.items():
            if (od.get("symbol") or "").upper() not in accepted:
                continue
            if (od.get("action") or "").upper() != action:
                continue
            if (od.get("order_type") or "").upper() != "LMT":
                continue
            try:
                op = float(od.get("limit_price") or 0.0)
            except (TypeError, ValueError):
                continue
            if op <= 0:
                continue
            same_side_lmt.append((oid, op))

        # Click on existing order's price → cancel-only (toggle off).
        # Collect every matching order before returning — multi-account
        # fan-out leaves one resting LMT per account at the same price,
        # and the user expects a single click to cancel the whole batch
        # (otherwise the toggle takes N clicks to clear N accounts).
        matches = [oid for oid, op in same_side_lmt
                   if abs(op - price) < tol]
        if matches:
            log.info("BookTrader: click on resting %s LMT %s @ %.4f "
                     "— cancelling %d order(s)", action,
                     self._active_symbol, price, len(matches))
            for oid in matches:
                self.cancel_order_requested.emit(oid)
            return

        # Click on different price while a same-side LMT rests → cancel
        # the old order(s) before placing the new one.  Cancels fire
        # first; TWS handles the two operations independently.
        for oid, op in same_side_lmt:
            log.info("BookTrader: move %s LMT for %s from %.4f to %.4f "
                     "— cancelling old order", action,
                     self._active_symbol, op, price)
            self.cancel_order_requested.emit(oid)

        # Build payload in the same shape OrderEntryPanel emits for a
        # BID_LMT / ASK_LMT order so FlashTradeView's existing
        # `_on_order_requested` fan-out handles TP/SL/Trail children
        # and multi-account dispatch without any changes.
        payload: dict = {
            "action": action,                 # "BUY" or "SELL"
            "type": "BID_LMT" if action == "BUY" else "ASK_LMT",
            "symbol": self._active_symbol,
            "qty": int(self._qty_row.current_qty()),
            "sec_type": self._active_sec_type,
            "last_price": float(self._last_price),
            "limit_price": float(price),
            "profit_take": self._pt_section.get_value()
                if hasattr(self, "_pt_section") else None,
            "stop_loss": self._sl_section.get_value()
                if hasattr(self, "_sl_section") else None,
            "trailing": self._tr_section.get_value()
                if hasattr(self, "_tr_section") else None,
        }
        log.info("BookTrader: ladder click → %s %s @ %.4f",
                 action, self._active_symbol, price)
        self.order_requested.emit(payload)

    def _emit_close_tier(self, side: str) -> None:
        section = self._pt_section if side == "tp" else self._sl_section
        tier = section.get_value()
        if tier is None or not self._active_symbol:
            return
        raw_val = float(tier.get("value", 0.0))
        unit = tier.get("unit", "price")
        if unit == "ticks":
            tick = self._model.tick_size
            offset_pts = raw_val * (tick if tick > 0 else 0.01)
        else:
            offset_pts = raw_val
        if offset_pts <= 0:
            return
        self.close_tier_requested.emit({
            "side": side,
            "symbol": self._active_symbol,
            "qty": int(self._qty_row.current_qty()),
            "offset_pts": offset_pts,
            "last_price": float(self._last_price),
        })

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _active_contract_cached(self) -> dict | None:
        if not self._active_symbol:
            return None
        try:
            from src.core.reference_data import get_instance as _rd
            return _rd().get_contract(self._active_symbol)
        except Exception:
            return None

    def _capture_tier_state(self, sym: str | None) -> None:
        if not sym or not hasattr(self, "_pt_section"):
            return
        self._tier_state_by_symbol[sym.upper()] = {
            "pt": self._pt_section.capture_state(),
            "sl": self._sl_section.capture_state(),
            "tr": self._tr_section.capture_state(),
        }

    def _restore_tier_state(self, sym: str | None) -> None:
        if not hasattr(self, "_pt_section"):
            return
        snapshot = self._tier_state_by_symbol.get(
            sym.upper()) if sym else None
        if snapshot:
            self._pt_section.restore_state(snapshot.get("pt"))
            self._sl_section.restore_state(snapshot.get("sl"))
            self._tr_section.restore_state(snapshot.get("tr"))
        else:
            self._pt_section.reset()
            self._sl_section.reset()
            self._tr_section.reset()

    def _reload_tier_sections(self) -> None:
        """Reload TP / SL / Trail chips from ReferenceData and sync
        qty + multiplier + market price into each section."""
        if not self._active_symbol or not hasattr(self, "_pt_section"):
            return
        from src.core.reference_data import get_instance as _rd
        rd = _rd()
        sym = self._active_symbol

        def _tiers(ot: str) -> list[dict]:
            raw = rd.get_tiers_for_symbol(sym, ot)
            if not raw:
                return []
            # Label chips with the offset value (matches OrderEntryPanel).
            out: list[dict] = []
            for t in raw:
                off = t.get("offset") or t.get("value", 0.0)
                try:
                    off = float(off)
                except (TypeError, ValueError):
                    off = 0.0
                lbl = f"{off:g}" if off > 0 else t.get("label", "")
                out.append({**t, "label": lbl})
            return out

        self._pt_section.reload_tiers(_tiers("take_profit"))
        self._sl_section.reload_tiers(_tiers("stop_loss"))
        self._tr_section.reload_tiers(_tiers("trailing"))

        for section in (self._pt_section, self._sl_section,
                        self._tr_section):
            section.set_qty(self._active_qty)

        mult = rd.get_multiplier(sym) or 1.0
        for section in (self._pt_section, self._sl_section,
                        self._tr_section):
            section.set_multiplier(mult)

        tick = self._model.tick_size
        for section in (self._pt_section, self._sl_section,
                        self._tr_section):
            section.set_market_price(self._last_price, tick)

    def refresh_subscriptions(self) -> None:
        """Re-check worker availability, wire signals, and (re)subscribe
        depth for the active symbol.

        Called from ``FlashTradeView._wire_all_workers`` once worker
        threads finish their initial IB connection.  Without this hook
        a panel built before workers connect would sit with
        ``_bg_worker = None`` forever — no market-data signals wired,
        no depth subscription → the ladder and ticker stay blank.

        Note: Book Trader does NOT subscribe L1 (``bg_price_updated``)
        — OrderEntryPanel already subscribes every saved symbol for L1
        ticks, and Book Trader just listens to the same signal.  Book
        Trader owns only the L2 (depth) subscription for the currently
        active symbol, which is the one data stream OrderEntry does
        not need.
        """
        self._ensure_bg_worker()
        # Wire order_status_updated on every connected worker so we can
        # render order badges on the ladder.  An order submitted by
        # OrderEntry on worker A needs to appear on BookTrader too, so
        # we fan out across all workers rather than just the bg one.
        self._wire_order_status_on_all_workers()
        if self._bg_worker is None or not self._active_symbol:
            return
        # Worker.subscribe_depth is idempotent (skips already-subscribed
        # symbols) so calling it on every refresh guarantees re-
        # subscription after a reconnect.
        self._bg_worker.subscribe_depth(self._active_symbol)

    def _wire_order_status_on_all_workers(self) -> None:
        """Connect ``order_status_updated`` for every running worker we
        haven't wired yet.  Idempotent — re-running a worker already in
        ``_order_status_wired`` is a no-op."""
        for _lbl, worker in self._active_threads.items():
            if worker in self._order_status_wired:
                continue
            try:
                if worker.isRunning():
                    worker.order_status_updated.connect(
                        self._on_order_status)
                    self._order_status_wired.add(worker)
            except Exception:
                continue

    def _ensure_bg_worker(self) -> None:
        """Pick a connected worker for L2 depth subscriptions (first match
        wins) and wire its ``bg_price_updated`` / ``depth_updated`` signals
        to the view's feed methods.

        Depth is single-worker — TWS caps concurrent L2 streams per
        account.  L1 (``bg_price_updated``) isn't strictly single-worker
        but using the same worker keeps the streams aligned with the
        depth stream.
        """
        current = self._bg_worker
        current_ok = (
            current is not None
            and getattr(current, "isRunning", lambda: False)()
            and getattr(current, "is_ib_connected", False)
        )
        if current_ok:
            return
        # Old worker is gone / disconnected — disconnect its signals and
        # pick a fresh one.
        if current is not None:
            try:
                current.bg_price_updated.disconnect(self.feed_price)
            except (RuntimeError, TypeError):
                pass
            try:
                current.depth_updated.disconnect(self.feed_depth)
            except (RuntimeError, TypeError):
                pass
            try:
                current.bg_sizes_updated.disconnect(self.feed_sizes)
            except (RuntimeError, TypeError, AttributeError):
                pass
            try:
                current.market_data_failed.disconnect(
                    self.on_market_data_failed)
            except (RuntimeError, TypeError):
                pass
            try:
                current.connection_status_changed.disconnect(
                    self._on_bg_worker_status)
            except (RuntimeError, TypeError):
                pass
        self._bg_worker = None
        candidates = list(self._active_threads.items())
        log.info(
            "BookTrader: _ensure_bg_worker scanning %d thread(s) "
            "(active_symbol=%s)",
            len(candidates), self._active_symbol)
        for lbl, worker in candidates:
            try:
                running = worker.isRunning()
                connected = worker.is_ib_connected
                log.info(
                    "BookTrader:   thread %s running=%s connected=%s",
                    lbl, running, connected)
                if running and connected:
                    self._bg_worker = worker
                    worker.bg_price_updated.connect(self.feed_price)
                    worker.depth_updated.connect(self.feed_depth)
                    worker.bg_sizes_updated.connect(self.feed_sizes)
                    worker.market_data_failed.connect(
                        self.on_market_data_failed)
                    worker.connection_status_changed.connect(
                        self._on_bg_worker_status)
                    log.info(
                        "BookTrader: wired signals on thread %s "
                        "(active_symbol=%s)",
                        lbl, self._active_symbol)
                    return
            except Exception as exc:
                log.warning(
                    "BookTrader: thread %s check failed: %s", lbl, exc)
                continue
        log.warning(
            "BookTrader: no connected worker available — market data "
            "will not flow until a worker comes online")

    def _on_bg_worker_status(self, label: str, is_connected: bool) -> None:
        # On (re)connect the worker wipes ``_depth_tickers`` (ib_worker.py
        # reconnect path), so our L2 stream is dead even though the Qt
        # signal wiring survived.  Re-subscribe the active symbol — the
        # worker's ``subscribe_depth`` is idempotent and thread-safe.
        if not is_connected or not self._active_symbol:
            return
        if self._bg_worker is None:
            return
        log.info(
            "BookTrader: worker %s reconnected — re-subscribing depth "
            "for %s", label, self._active_symbol)
        self._bg_worker.subscribe_depth(self._active_symbol)

    # ── Add-symbol popup (searchable) ────────────────────────────────────────

    def _show_add_symbol_popup(self) -> None:
        """Small searchable popup listing saved contracts the user hasn't
        added to the chip bar yet.  Same UX as OrderEntryPanel's "+"
        button popup; not shared because it's light and self-contained.
        """
        from src.core.reference_data import get_instance as _rd
        added = {s.upper() for s in self._symbols}
        seen: set[str] = set()
        entries: list[tuple[str, str]] = []
        for c in sorted(_rd().contracts, key=lambda x: x.get("symbol", "")):
            sym = c.get("symbol", "")
            if not sym or sym.upper() in added:
                continue
            sec = c.get("sec_type", "")
            local = c.get("local_symbol", "")
            exch = c.get("exchange", "")
            key = f"{sym}_{local}_{sec}"
            if key in seen:
                continue
            seen.add(key)
            detail = local if local and local != sym else exch
            label = (f"{sym}  –  {detail}  ({sec})"
                     if detail else f"{sym}  ({sec})")
            entries.append((label, sym))

        dlg = QDialog(self)
        dlg.setWindowFlags(Qt.WindowType.Popup)
        dlg.setFixedWidth(260)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        search = QLineEdit()
        search.setPlaceholderText("Type to search…")
        search.setStyleSheet(
            f"QLineEdit{{background:{Theme.BG_INPUT};color:{Theme.TEXT};"
            f"border:1px solid {Theme.BORDER};border-radius:3px;"
            f"padding:4px 6px;font-size:12px}}"
            f"QLineEdit:focus{{border-color:{Theme.ACCENT}}}")
        lay.addWidget(search)

        lst = QListWidget()
        lst.setStyleSheet(
            f"QListWidget{{background:{Theme.BG_INPUT};color:{Theme.TEXT};"
            f"border:1px solid {Theme.BORDER};border-radius:3px;"
            f"padding:2px;outline:none}}"
            f"QListWidget::item{{padding:4px 6px;border-radius:2px}}"
            f"QListWidget::item:selected{{background:#3a3f50}}")
        lst.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay.addWidget(lst)

        if not entries:
            item = QListWidgetItem(
                "(no more symbols — add via Contracts tab)")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            lst.addItem(item)
        else:
            for label, sym in entries:
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, sym)
                lst.addItem(item)
            lst.setCurrentRow(0)

        def _apply_filter(text: str) -> None:
            q = text.strip().lower()
            first_visible = -1
            for i in range(lst.count()):
                it = lst.item(i)
                if it.data(Qt.ItemDataRole.UserRole) is None:
                    continue
                visible = (not q) or (q in it.text().lower())
                it.setHidden(not visible)
                if visible and first_visible < 0:
                    first_visible = i
            if first_visible >= 0:
                lst.setCurrentRow(first_visible)

        def _commit_current() -> None:
            it = lst.currentItem()
            if it is None or it.isHidden():
                return
            sym = it.data(Qt.ItemDataRole.UserRole)
            if not sym:
                return
            dlg.close()
            self._add_symbol(sym)

        def _step(delta: int) -> None:
            n = lst.count()
            if n == 0:
                return
            row = lst.currentRow()
            for _ in range(n):
                row = (row + delta) % n
                it = lst.item(row)
                if it is not None and not it.isHidden() \
                        and it.data(Qt.ItemDataRole.UserRole):
                    lst.setCurrentRow(row)
                    return

        search.textChanged.connect(_apply_filter)
        search.returnPressed.connect(_commit_current)
        lst.itemDoubleClicked.connect(lambda _it: _commit_current())

        def _key(event) -> None:
            if event.key() == Qt.Key.Key_Down:
                _step(+1)
                event.accept()
                return
            if event.key() == Qt.Key.Key_Up:
                _step(-1)
                event.accept()
                return
            QLineEdit.keyPressEvent(search, event)
        search.keyPressEvent = _key  # type: ignore[method-assign]

        # Anchor below the ticker's "+" button.
        btn = self._ticker._btn_add      # noqa: SLF001 — private-ish, single-caller
        pos = btn.mapToGlobal(btn.rect().bottomLeft())
        dlg.move(pos)
        dlg.show()
        search.setFocus()

    def _add_symbol(self, sym: str) -> None:
        sym = (sym or "").strip().upper()
        if not sym or sym in {s.upper() for s in self._symbols}:
            return
        self._symbols.append(sym)
        config_manager.save_book_trader_symbols(self._symbols)
        self._chip_bar.set_symbols(
            [(s, _sec_type_for(s)) for s in self._symbols])
        # Auto-activate the newly added symbol.
        self._on_symbol_selected(sym, _sec_type_for(sym))

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            if self._bg_worker is not None and self._active_symbol:
                self._bg_worker.unsubscribe_depth(self._active_symbol)
        except Exception:
            pass
        super().closeEvent(event)
