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
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.core import config_manager
from src.ui.theme import Theme
from src.utils.helpers import format_option_symbol
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


# Qty presets (same rule as OrderEntryPanel: different sets for FUT vs. STK;
# option contracts trade in small lots like futures).
_FUT_QTYS = [1, 2, 3, 5]
_STK_QTYS = [10, 25, 50, 100, 500]
_OPT_QTYS = [1, 2, 3, 5]
_FUT_DEFAULT = 1
_STK_DEFAULT = 100
_OPT_DEFAULT = 1
_FUT_QTY_POPUP: list[int] = list(range(1, 101))
_OPT_QTY_POPUP: list[int] = list(range(1, 101))
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
    if sec == "OPT":
        return _OPT_QTYS, _OPT_DEFAULT, _OPT_QTY_POPUP
    return _STK_QTYS, _STK_DEFAULT, _STK_QTY_POPUP


def _chip_key_for_position(pos: dict) -> tuple[str, str]:
    """Chip key + sec_type for a portfolio position dict.

    STK and FUT chips are keyed by the root symbol (FUT chips are roots
    like "MNQ", never local symbols like "MNQM6").  OPT chips are keyed
    by the raw OCC local symbol — an option must map to its own chip,
    never to the root's STK chip.  Returns ("", sec) when the position
    carries no usable symbol.
    """
    sec = (pos.get("sec_type") or "").upper()
    if sec == "OPT":
        key = (pos.get("local_symbol") or "").strip().upper()
    else:
        key = (pos.get("symbol") or "").strip().upper()
    return key, sec


def _position_like_from_order(order: dict) -> dict | None:
    """Translate an order-blotter dict into a ``focus_position`` payload.

    The blotter's ``symbol`` is a display symbol — a bare root ("AAPL"),
    a futures localSymbol ("MNQU6"), or a raw OCC option symbol — so it
    is first matched against the saved-contracts cache (localSymbol
    match wins over root match, mirroring ``instruments.get_contract``).
    Unsaved symbols fall back to the order dict itself: OCC-shaped ones
    become OPT payloads (``con_id``/``sec_type`` travel on the order
    dict, so ``_ensure_position_contract_saved`` can still register
    them), anything else is treated as a bare root symbol.  Returns
    ``None`` when the order carries no symbol.
    """
    raw = (order.get("symbol") or "").strip().upper()
    if not raw:
        return None
    try:
        qty = abs(int(float(order.get("quantity") or 0)))
    except (TypeError, ValueError):
        qty = 0

    from src.core import instruments
    root_match: dict | None = None
    local_match: dict | None = None
    for c in instruments.all_contracts():
        if (c.get("local_symbol") or "").strip().upper() == raw:
            local_match = c
            break
        if root_match is None and (
                (c.get("symbol") or "").strip().upper() == raw):
            root_match = c
    row = local_match or root_match
    if row:
        return {
            "symbol":       row.get("symbol") or raw,
            "local_symbol": row.get("local_symbol") or "",
            "sec_type":     (row.get("sec_type") or "").upper(),
            # The order's own con_id backfills a saved row missing its
            # conid (older builds saved OPT rows without one) so
            # _ensure_position_contract_saved can heal it.
            "con_id":       row.get("conid") or order.get("con_id") or "",
            "exchange":     row.get("exchange") or "",
            "currency":     row.get("currency") or "USD",
            "multiplier":   row.get("multiplier") or "",
            "position":     qty,
        }

    # Not in the contracts universe.  OCC-shaped symbols are options
    # (format_option_symbol only rewrites OCC strings); anything else is
    # treated as a bare root symbol and resolved best-effort at chip time.
    is_opt = format_option_symbol(raw) != raw
    return {
        "symbol":       raw,
        "local_symbol": raw if is_opt else "",
        "sec_type":     "OPT" if is_opt else (order.get("sec_type") or "").upper(),
        "con_id":       order.get("con_id") or "",
        "exchange":     order.get("exchange") or "",
        "currency":     order.get("currency") or "USD",
        "multiplier":   order.get("multiplier") or "",
        "position":     qty,
    }


def _contract_meta_from_position(pos: dict) -> dict | None:
    """Contract metadata for an OPT position's chip, or ``None``.

    Option contracts arrive via the position feed, not the symbol
    search, so the chip's metadata is built from the position dict
    itself (conid, exchange, multiplier).  Without a conid the entry
    could never subscribe market data — return ``None`` so the chip
    stays bare and a later click that DOES carry a conid completes it.
    """
    key, sec = _chip_key_for_position(pos)
    if sec != "OPT" or not key:
        return None
    con_id = str(pos.get("con_id") or "")
    if not con_id:
        log.warning("BookTrader: no conid for OPT %s — contract metadata "
                    "not stored, ladder will not stream", key)
        return None
    return {
        "conid": con_id,
        "sec_type": "OPT",
        "exchange": pos.get("exchange") or "SMART",
        "currency": pos.get("currency") or "USD",
        "local_symbol": pos.get("local_symbol") or "",
        "multiplier": str(pos.get("multiplier") or "100"),
    }


def _ensure_position_contract_saved(pos: dict) -> dict | None:
    """Resolve (and heal) an OPT position's chip metadata.

    Returns the metadata dict for the chip key, or ``None`` when the
    position carries no conid.  Existing chip / hot-button references
    are healed in place via ``upsert_instrument``.  Idempotent: no-op
    when the key already resolves with a conid.
    """
    from src.core import instruments
    key, sec = _chip_key_for_position(pos)
    if sec != "OPT" or not key:
        return None
    existing_entry = instruments.get_contract(key)
    if existing_entry is not None and str(existing_entry.get("conid") or ""):
        return None  # already resolvable — market data can stream
    meta = _contract_meta_from_position(pos)
    if meta is None:
        return None
    if config_manager.upsert_instrument(key, meta):   # heal existing refs
        log.info("BookTrader: stored OPT metadata %s (conid %s) from "
                 "position", key, meta["conid"])
    return meta


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
         entry (min_tick, captured from TWS contract details).
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
    # A saved futures contract was swapped for a different expiration via
    # the search popup — FlashTradeView re-points both panels' market
    # data at the new contract.
    contract_replaced = Signal(str)

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
        # Last-known (last, bid, ask, close) per symbol, updated on every
        # bg tick BEFORE the active-symbol gate in ``feed_price``.  On a
        # symbol switch the cached tick is replayed so the ladder warm-
        # starts instantly — the worker skips already-subscribed symbols
        # (no fresh snapshot on re-select), and a thinly-traded contract
        # (e.g. an option) may not print for minutes, which used to
        # leave the ladder blank until the next natural tick.
        self._bg_price_cache: dict[str, tuple[float, float, float, float]] = {}
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

        # Symbol-chip bar → qty-chip row gap.  Tightened from 12 px to
        # 7 px (3/5) to match the OrderEntry panel and tighten the
        # visual cluster.
        root.addSpacing(7)

        # ── Qty chip row ──────────────────────────────────────────────────────
        # The SH/% percent-of-account toggle is stocks-only: futures
        # size in contracts against margin (notional percent math is
        # meaningless there) and options carry multiplier semantics —
        # both keep today's plain share/contract row (spec §4).
        self._qty_row = QtyChipRow()
        presets, default, popup = _presets_for_sec(self._active_sec_type)
        self._qty_row.set_presets(
            presets, default, popup,
            allow_percent=(self._active_sec_type == "STK"))
        self._qty_row.set_price_provider(lambda: self._last_price)
        self._qty_row.set_symbol_provider(lambda: self._active_symbol or "")
        self._qty_row.preview_changed.connect(self._on_qty_preview_changed)
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
            "Trailing", [], label_color=Theme.CLR_CLOSE,
            short_title="TS", label_fg="#000000")

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
            f"font-size: 10px; font-family: Inter, 'Helvetica Neue', Arial; "
            f"font-weight: bold;"
        )
        self._lbl_bid_hdr = QLabel("Bid Size")
        self._lbl_bid_hdr.setStyleSheet(_hdr_ss)
        self._lbl_bid_hdr.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._lbl_bid_hdr.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lbl_bid_hdr.setToolTip("Double-click to recentre ladder")
        self._lbl_bid_hdr.mouseDoubleClickEvent = (  # type: ignore[method-assign]
            lambda _ev: self._on_recenter_clicked())

        self._cmb_tick = QComboBox()
        self._cmb_tick.setFixedHeight(24)
        self._cmb_tick.setMaximumWidth(86)
        # combobox-popup:0 forces Qt's list-view popup instead of the
        # native macOS menu — the native one ignores the dark styling
        # and flashes a white panel.  The popup palette matches the
        # qty-input dropdown ladder (#1B2130 / #539BF5 highlight).
        self._cmb_tick.setStyleSheet(
            f"QComboBox{{background:{Theme.BG_INPUT};color:{Theme.TEXT};"
            f"border:1px solid {Theme.BORDER};border-radius:3px;"
            f"padding:1px 6px;font-size:11px;font-family:'JetBrains Mono';"
            f"combobox-popup:0}}"
            f"QComboBox:hover{{border-color:{Theme.ACCENT}}}"
            "QComboBox::drop-down{border:none;width:16px}"
            "QComboBox QAbstractItemView{background:#1B2130;"
            f"color:{Theme.TEXT};border:1px solid #334058;"
            "border-radius:4px;outline:none;padding:2px;font-size:12px}"
            "QComboBox QAbstractItemView::item{padding:3px 10px;"
            "border:none;min-height:20px}"
            "QComboBox QAbstractItemView::item:hover,"
            "QComboBox QAbstractItemView::item:selected"
            "{background:#539BF5;color:white;border-radius:3px}")
        # The list-view popup defaults to the combo's own (narrow)
        # width, eliding the longest entries ("0.01" → "…") — widen it
        # and never elide.
        self._cmb_tick.view().setTextElideMode(Qt.TextElideMode.ElideNone)
        self._cmb_tick.view().setMinimumWidth(90)
        self._cmb_tick.currentTextChanged.connect(self._on_tick_changed)

        # Lock button — when locked the ladder auto-scrolls so the
        # last-price row stays centred in the viewport on every tick.
        # Unlock to scroll the ladder manually without the view yanking
        # back to the centre.  Closed-padlock SVG when locked, open-
        # padlock SVG when unlocked (see _on_lock_toggled).
        # Initial state: persisted user choice (defaults to UNLOCKED on
        # first launch).  setChecked() runs before .toggled is wired so
        # the load doesn't fire the save callback.
        self._icon_lock_closed = QIcon(_LOCK_CLOSED_ICON)
        self._icon_lock_open = QIcon(_LOCK_OPEN_ICON)
        try:
            self._initial_lock_state: bool = (
                config_manager.load_book_trader_lock_state())
        except Exception:
            log.debug(
                "BookTrader: could not read persisted lock state, "
                "defaulting to unlocked", exc_info=True)
            self._initial_lock_state = False
        self._btn_lock = QPushButton()
        self._btn_lock.setIcon(
            self._icon_lock_closed if self._initial_lock_state
            else self._icon_lock_open)
        self._btn_lock.setIconSize(QSize(16, 16))
        self._btn_lock.setCheckable(True)
        self._btn_lock.setChecked(self._initial_lock_state)
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
            f"QPushButton:checked{{background:#334058;"
            f"border-color:{Theme.ACCENT}}}")
        self._btn_lock.toggled.connect(self._on_lock_toggled)

        self._lbl_ask_hdr = QLabel("Ask Size")
        self._lbl_ask_hdr.setStyleSheet(_hdr_ss)
        self._lbl_ask_hdr.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Centre cell groups the tick-aggregation combo and the lock
        # button side-by-side under a single stretch slot; outer cells
        # hold the BID SZ / ASK SZ labels.
        # Percent-mode safety readouts (spec §5): each side's resolved
        # share count must be on screen BEFORE any rung click.  Hidden
        # in share mode so the header stays pixel-identical to today.
        # Header = 5 slots: Bid Size · B:… · [lock+interval] · S:… ·
        # Ask Size, with the interval group staying dead-centre (three
        # equal-stretch thirds).
        _pct_ss = (
            "color: {clr}; border: none; font-size: 11px; "
            "font-family: 'JetBrains Mono'; font-weight: bold;")
        self._lbl_pct_buy = QLabel("")
        self._lbl_pct_buy.setStyleSheet(_pct_ss.format(clr=Theme.ACCENT))
        self._lbl_pct_buy.hide()
        self._lbl_pct_sell = QLabel("")
        self._lbl_pct_sell.setStyleSheet(_pct_ss.format(clr="#F85149"))
        self._lbl_pct_sell.hide()

        left_box = QHBoxLayout()
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.setSpacing(4)
        left_box.addWidget(self._lbl_bid_hdr)
        left_box.addStretch(1)
        left_box.addWidget(self._lbl_pct_buy)

        centre_box = QHBoxLayout()
        centre_box.setContentsMargins(0, 0, 0, 0)
        centre_box.setSpacing(4)
        centre_box.addStretch(1)
        centre_box.addWidget(self._btn_lock)
        centre_box.addWidget(self._cmb_tick)
        centre_box.addStretch(1)

        right_box = QHBoxLayout()
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.setSpacing(4)
        right_box.addWidget(self._lbl_pct_sell)
        right_box.addStretch(1)
        right_box.addWidget(self._lbl_ask_hdr)

        hdr.addLayout(left_box, stretch=1)
        hdr.addLayout(centre_box, stretch=1)
        hdr.addLayout(right_box, stretch=1)
        root.addLayout(hdr)

        # ── DOM ladder ────────────────────────────────────────────────────────
        self._ladder = DOMLadderView(model=self._model)
        # Sync the ladder's internal lock flag with the persisted button
        # state.  DOMLadderView defaults its _locked to True; if the user
        # last left the lock OFF, push that through immediately so the
        # initial behaviour matches the displayed icon without waiting
        # for the user to click the button.
        self._ladder.set_locked(self._initial_lock_state)
        root.addWidget(self._ladder, stretch=1)
        # Hover badge content — ``Buy {qty}`` / ``Sell {qty}`` plus a
        # second line listing armed TP / SL / TS amounts so the trader
        # sees exactly what a click would submit.
        self._ladder.set_qty_provider(self._ladder_badge_qty)
        self._ladder.set_armed_summary_provider(self._format_attached_summary)
        # Populate tick combo for the initial contract.
        self._populate_tick_combo()

        # ── Reverse / Flatten / Cancel ────────────────────────────────────────
        pm_row = QHBoxLayout()
        pm_row.setContentsMargins(0, 0, 0, 0)
        pm_row.setSpacing(4)
        self._btn_reverse = _make_btn("Reverse", Theme.CLR_REVERSE, 30)
        self._btn_flatten = _make_btn("Flatten", Theme.CLR_FLATTEN, 30,
                                      fg="#1c1206")
        self._btn_cancel = _make_btn("Cancel", Theme.CLR_CANCEL, 30)
        pm_row.addWidget(self._btn_reverse)
        pm_row.addWidget(self._btn_flatten)
        pm_row.addWidget(self._btn_cancel)
        root.addLayout(pm_row)

        # ── TP / SL / Trail tier sections ─────────────────────────────────────
        # The widgets themselves were created earlier (so the ARMED
        # strip above could connect to their ``changed`` signal); here
        # we just lay them out at the bottom of the root column.
        # Wrap in a sub-VBox with tighter spacing (root uses 8 px; we
        # halve it to 4 px) so TP / SL / Trail read as a connected
        # cluster without affecting the surrounding gaps.
        tier_box = QVBoxLayout()
        tier_box.setContentsMargins(0, 0, 0, 0)
        tier_box.setSpacing(4)
        tier_box.addWidget(self._pt_section)
        tier_box.addWidget(self._sl_section)
        tier_box.addWidget(self._tr_section)
        root.addLayout(tier_box)

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
        self._tr_section.submit_requested.connect(
            lambda: self._emit_close_tier("tr"))

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
        # Cache every symbol's tick (merging per-field so a bid-only
        # tick doesn't erase a known last) BEFORE the active-symbol
        # gate — the cache warm-starts the ladder on symbol switches.
        u = symbol.upper()
        prev = self._bg_price_cache.get(u, (0.0, 0.0, 0.0, 0.0))
        self._bg_price_cache[u] = (
            float(last) if last and last > 0 else prev[0],
            float(bid) if bid and bid > 0 else prev[1],
            float(ask) if ask and ask > 0 else prev[2],
            float(close) if close and close > 0 else prev[3],
        )
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
        # Percent sizing re-resolves on every tick (no-op in SH mode;
        # bg ticks are already throttled upstream).
        self._qty_row.refresh_percent_preview()

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
        # orderId → permId migration: drop the stale entry the new one
        # replaces so the old badge doesn't linger on the ladder.
        superseded = data.get("supersedes")
        if superseded:
            self._live_orders.pop(superseded, None)
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
        from src.core import instruments
        c = instruments.get_contract(active_u)
        if c:
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
        # allow_percent resets the row to SH mode too (never persisted).
        self._qty_row.set_presets(
            presets, default, popup,
            allow_percent=(self._active_sec_type == "STK"))
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

        # Swap depth subscription on the bg worker.  L1 is normally
        # owned by OrderEntryPanel — it subscribes every saved chip-bar
        # symbol — but a symbol that lives only on Book Trader's chip
        # bar (e.g. the user added MCL here without adding it to Order
        # Entry) won't get an L1 tick from that path, so the ladder
        # would sit at 0-centred prices forever.  Subscribe L1 for the
        # active symbol from BookTrader too — ``subscribe_background_symbols``
        # is idempotent on the worker side, so this is a no-op when
        # OrderEntry already has it.
        self._ensure_bg_worker()
        if self._bg_worker is not None:
            if old:
                self._bg_worker.unsubscribe_depth(old)
            self._bg_worker.subscribe_depth(sym)
            l1_params = self._build_l1_params_for(sym)
            if l1_params is not None:
                self._bg_worker.subscribe_background_symbols([l1_params])

        # Different symbol → different subset of live orders is
        # relevant.  Re-project the ladder markers.
        self._refresh_ladder_orders()

        # Warm-start from the last-known tick.  The worker skips
        # already-subscribed symbols (no fresh snapshot on re-select),
        # so a thinly-traded contract would otherwise leave the ladder
        # blank until its next natural tick.
        cached = self._bg_price_cache.get(sym.upper())
        if cached and any(v > 0 for v in cached[:3]):
            self.feed_price(sym, *cached)

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
        # Dropping the chip entry drops its contract metadata with it —
        # no external cleanup needed.
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

    def refresh_symbol_contract(self, sym: str) -> None:
        """Re-point *sym*'s L1/L2 streams at its (possibly replaced)
        saved contract.  Both subscription paths are symbol-keyed on the
        worker, so an unsubscribe → subscribe cycle is required for the
        new conId to take effect."""
        sym = (sym or "").upper()
        if sym not in {s.upper() for s in self._symbols}:
            return
        self._ensure_bg_worker()
        if self._bg_worker is None:
            return
        self._bg_worker.unsubscribe_background_symbol(sym)
        l1_params = self._build_l1_params_for(sym)
        if l1_params is not None:
            self._bg_worker.subscribe_background_symbols([l1_params])
        if (self._active_symbol or "").upper() == sym:
            self._bg_worker.unsubscribe_depth(self._active_symbol)
            self._bg_worker.subscribe_depth(self._active_symbol)

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
        Persists the choice so the next app launch restores it.
        """
        self._btn_lock.setIcon(
            self._icon_lock_closed if locked else self._icon_lock_open)
        self._ladder.set_locked(locked)
        try:
            config_manager.save_book_trader_lock_state(locked)
        except Exception:
            log.warning(
                "BookTrader: could not persist lock state %s",
                locked, exc_info=True)

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

    def _on_ladder_clicked(self, action: str, price: float,
                           add_only: bool = False) -> None:
        """Click on a bid-size or ask-size cell.

        Behaviour mirrors pro DOM ladders:
        * Click on a price where the user has no open same-side LMT →
          place a new LMT at that price.
        * Click on a price where the user already has a same-side LMT →
          cancel that resting order (toggle off).
        * Click on a different price while a same-side LMT is resting
          → cancel the old order and place a new one at the click
          (move the order).
        * Shift+left-click or right-click (*add_only*) → STACK a new
          LMT at the rung without touching any resting order — for
          laddering multiple orders at different prices.

        Only plain LMT orders are considered — STP / STP LMT protective
        orders are left alone so a casual click doesn't drop a stop.
        """
        if not self._active_symbol or price <= 0:
            return
        if not add_only:
            tick = self._model.tick_size
            tol = (tick / 2) if tick > 0 else 0.01

            # Match the active symbol against both bare and local symbols
            # (futures' order blotter uses the local symbol, e.g. "MNQM6").
            accepted: set[str] = {self._active_symbol.upper()}
            try:
                from src.core import instruments
                c = instruments.get_contract(self._active_symbol)
                if c:
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
        pct = self._qty_row.current_percent()
        if pct is not None:
            # Percent mode is SIDE-AWARE: BUY resolves against each
            # account's net liq, SELL against each account's position in
            # this symbol (never opens a short).  qty carries the
            # conservative per-side preview; the real per-account counts
            # are resolved in FlashTradeView's fan-out.
            side_qty = self._qty_row.percent_side_qty(action)
            if side_qty <= 0:
                log.warning("BookTrader: %s %s%% resolves to 0 shares "
                            "(no %s) — ladder click ignored",
                            action, pct,
                            "position held" if action == "SELL"
                            else "price/account data")
                return
            payload["qty"] = side_qty
            payload["qty_pct"] = int(pct)
        log.info("BookTrader: ladder click → %s %s @ %.4f",
                 action, self._active_symbol, price)
        self.order_requested.emit(payload)

    def _emit_close_tier(self, side: str) -> None:
        section = {"tp": self._pt_section, "sl": self._sl_section,
                   "tr": self._tr_section}[side]
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
        # Close-tier (like flatten / reverse / cancel) must never
        # inherit percent semantics — it consumes the resolved-share
        # snapshot, matching the on-screen readout.
        qty = int(self._qty_row.current_qty())
        if qty <= 0:
            log.warning("BookTrader: close-tier skipped — qty unresolved")
            return
        self.close_tier_requested.emit({
            "side": side,
            "symbol": self._active_symbol,
            "qty": qty,
            "offset_pts": offset_pts,
            "last_price": float(self._last_price),
        })

    # ── Percent sizing (SH/% toggle) ─────────────────────────────────────────

    def set_percent_context(self, accounts_provider) -> None:
        """Host (FlashTradeView) pushes the checked-accounts provider:
        ``fn() -> [(account, net_liq, buying_power)]``."""
        self._qty_row.set_percent_context(accounts_provider)

    def refresh_percent_preview(self) -> None:
        """Re-resolve the % readout (account selection / summary change)."""
        self._qty_row.refresh_percent_preview()

    def _ladder_badge_qty(self, side: str) -> int:
        """Hover-badge count for *side* ("BUY"/"SELL") — always the
        TOTAL a click would transmit.  % mode: per-side resolved total
        across the checked accounts.  Share mode: the active qty × the
        number of checked accounts (the fan-out places one order per
        account)."""
        if self._qty_row.current_percent() is not None:
            return self._qty_row.percent_side_total(side)
        return self._active_qty * max(1, self._qty_row.account_count())

    def _on_qty_preview_changed(self, buy_text: str,
                                sell_text: str) -> None:
        self._lbl_pct_buy.setText(buy_text)
        self._lbl_pct_buy.setVisible(bool(buy_text))
        self._lbl_pct_sell.setText(sell_text)
        self._lbl_pct_sell.setVisible(bool(sell_text))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _active_contract_cached(self) -> dict | None:
        if not self._active_symbol:
            return None
        try:
            from src.core import instruments
            return instruments.get_contract(self._active_symbol)
        except Exception:
            return None

    @staticmethod
    def _build_l1_params_for(sym: str) -> dict | None:
        """Return the param dict ``IBWorkerThread.subscribe_background_symbols``
        expects for *sym*, or ``None`` when the chip's instrument entry
        has no conid (in which case L1 can't be subscribed regardless).
        Mirror of :meth:`OrderEntryPanel._build_bg_params_for_symbol` so
        BookTrader can subscribe its active symbol's L1 directly when
        OrderEntry's chip bar doesn't already cover it.
        """
        from src.core import instruments
        return instruments.build_md_params(sym) if sym else None

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

        from src.core import instruments
        mult = instruments.get_multiplier(sym) or 1.0
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
        # Same idempotent re-subscription for L1 so the ladder gets
        # prices for symbols only present in BookTrader's chip bar.
        l1_params = self._build_l1_params_for(self._active_symbol)
        if l1_params is not None:
            self._bg_worker.subscribe_background_symbols([l1_params])

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
                    # Replay statuses emitted before we wired — the
                    # post-connect snapshot fires ~1-2 s after connect
                    # and a working open order never re-emits on its
                    # own, so without this the ladder shows no badge
                    # for orders restored across an app restart.
                    snapshot = getattr(worker, "order_status_snapshot", None)
                    if callable(snapshot):
                        for d in snapshot():
                            self._on_order_status(d)
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

    # ── Jump-to-position (positions panel row click) ─────────────────────────

    def focus_position(self, pos: dict) -> None:
        """Select (or add) the symbol chip for a portfolio position and
        prefill Qty with the position size.

        Called by FlashTradeView when the user clicks a position row.
        The qty fill must run AFTER the symbol selection because
        ``_on_symbol_selected`` resets the qty row to the sec-type
        presets.  In percent mode the fill is deliberately skipped —
        silently flipping the user out of % sizing would be worse than
        leaving the percentage untouched.
        """
        key, sec = _chip_key_for_position(pos)
        if not key:
            return
        meta = None
        if sec == "OPT":
            # Before selection: sec_type + L1 params must resolve.
            # Heals existing references and returns the metadata for a
            # chip that doesn't exist yet.
            meta = (_ensure_position_contract_saved(pos)
                    or _contract_meta_from_position(pos))
        if key in {s.upper() for s in self._symbols}:
            self._on_symbol_selected(key, _sec_type_for(key))
        else:
            self._add_symbol(key, meta=meta)
        from src.ui.widgets.size_unit_parts import UNIT_PCT
        try:
            qty = abs(int(float(pos.get("position") or 0)))
        except (TypeError, ValueError):
            qty = 0
        if qty > 0 and self._qty_row.current_unit() != UNIT_PCT:
            self._qty_row.set_qty(qty)   # visual only — emits no signal
            self._on_qty_changed(qty)    # sync _active_qty + tier sections

    def focus_order(self, order: dict) -> None:
        """Jump the ladder to an order-blotter row's instrument.

        Called by FlashTradeView when the user clicks the Symbol cell in
        the Orders view.  Translates the blotter dict (display symbol +
        order qty) into a position-shaped payload and reuses the
        ``focus_position`` flow: chip select/add, qty prefill (preset
        chip lights when the qty matches, custom field otherwise), and
        the price subscription that starts the ladder streaming.
        """
        pos = _position_like_from_order(order)
        if pos is None:
            return
        self.focus_position(pos)

    # ── Add-symbol popup (searchable) ────────────────────────────────────────

    def open_symbol_search(self) -> None:
        """Public entry point for the global "/" shortcut (MainWindow)."""
        self._show_add_symbol_popup()

    def _show_add_symbol_popup(self) -> None:
        """Searchable add-symbol popup: saved contracts appear instantly
        and, as the user types, live IBKR stock matches stream in via
        reqMatchingSymbols — no need to add the stock on the Contracts
        tab first.  Picking an IBKR row auto-saves the contract."""
        from src.ui.widgets.symbol_search_popup import (
            SymbolSearchPopup,
            apply_future_pick,
            connected_workers_by_priority,
            ensure_contract_saved,
        )
        dlg = SymbolSearchPopup(
            self,
            workers=connected_workers_by_priority(self._active_threads),
            exclude={s.upper() for s in self._symbols})

        def _picked(entry: dict) -> None:
            from src.core import instruments
            if (entry.get("source") == "ibkr"
                    and entry.get("sec_type") == "FUT"):
                sym, replaced = apply_future_pick(self, entry)
                if not sym:
                    return  # user declined the replacement
                self._add_symbol(
                    sym, meta=instruments.contract_meta_from_result(entry))
                if replaced:
                    self.contract_replaced.emit(sym)
                return
            sym, meta = ensure_contract_saved(entry)
            if sym:
                self._add_symbol(sym, meta=meta)

        dlg.symbol_selected.connect(_picked)
        # Anchor below the ticker's "+" button.
        btn = self._ticker._btn_add      # noqa: SLF001 — private-ish, single-caller
        dlg.open_below(btn)

    def _add_symbol(self, sym: str, meta: dict | None = None) -> None:
        sym = (sym or "").strip().upper()
        if not sym or sym in {s.upper() for s in self._symbols}:
            return
        if meta:
            # Classify from the incoming metadata when the entry isn't
            # stored yet (the resolver can't know a brand-new symbol).
            new_sec = str(meta.get("sec_type", "") or "") or _sec_type_for(sym)
        else:
            new_sec = _sec_type_for(sym)
        if new_sec != "FUT":
            # The STK group renders at most 5 × 3 chips.  When it's
            # full, the new symbol replaces the LAST chip (row 3,
            # column 5) instead of becoming an invisible overflow
            # entry.  Dropping the chip entry IS the cleanup.
            stk_group = [s for s in self._symbols
                         if _sec_type_for(s) != "FUT"]
            if len(stk_group) >= 15:
                replaced = stk_group[-1]
                self._symbols.remove(replaced)
                log.info("BookTrader: STK chip group full — %s replaces "
                         "%s", sym, replaced)
        # Option chips always occupy the LAST position of the chip bar:
        # an option appends to the tail, and a new stock slots in BEFORE
        # the first option chip so options stay pinned at the end as the
        # list grows.  The OCC-shape check covers options whose contract
        # couldn't be saved (no conid → _sec_type_for is "?").
        def _is_opt(s: str) -> bool:
            return _sec_type_for(s) == "OPT" or format_option_symbol(s) != s
        if new_sec == "FUT" or _is_opt(sym):
            self._symbols.append(sym)
        else:
            opt_idx = next(
                (i for i, s in enumerate(self._symbols) if _is_opt(s)), None)
            if opt_idx is None:
                self._symbols.append(sym)
            else:
                self._symbols.insert(opt_idx, sym)
        config_manager.save_book_trader_symbols(self._symbols)
        if meta:
            config_manager.upsert_instrument(sym, meta)
        entries = [(s, _sec_type_for(s)) for s in self._symbols]
        self._chip_bar.set_symbols(entries)
        # The FUT group still renders at most 5 × 2 chips; an
        # overflowing future is persisted and activated but its chip is
        # not visible.
        if new_sec == "FUT":
            group = [s for s, sec in entries if sec == "FUT"]
            if len(group) > 10:
                log.warning("BookTrader: chip group full — %s added and "
                            "activated but its chip is not rendered", sym)
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
