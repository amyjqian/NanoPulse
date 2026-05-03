"""
TradesPanel – control-bar + QStackedWidget hosting the flat and summary
trades views, each wrapped in a ``TradesFilterProxy``.

Public API (unchanged signature)
-------------------------------
add_fill(fill_dict)   – routes the fill into *both* underlying models so
                        flipping the Summary toggle is instant (no re-fetch).
clear_fills()         – wipes both models.

The Settings gear forwards to a host-supplied ``open_preferences``
callback (typically ``MainWindow._open_trades_preferences``) which opens
the global Preferences dialog at Columns → Trades / Summary.  Column
changes come back through the ``ColumnsProvider`` protocol
(``column_state`` / ``apply_columns``); Apply projects the new
visible-keys order onto the table via ``setColumnHidden`` +
``header.moveSection``.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QMenu,
    QStackedWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from typing import Callable

from src.core import config_manager
from .trades_control_bar import TradesControlBar
from .trades_filter_proxy import TradesFilterProxy, _try_parse_time_query
from .trades_model import (
    TradesModel,
    COL_TIME, COL_SYMBOL, COL_SECTYPE, COL_SIDE, COL_QTY, COL_PRICE, COL_EXCH,
    COL_REF, COL_ACCOUNT, COL_EXPIRY, COL_STRIKE_RIGHT, COL_COMM, COL_REAL_PNL,
    COL_ORDER_ID, COL_SUBMITTER, COL_LIQUIDATION, COL_LAST_LIQ,
)
from .trades_summary_model import (
    TradesSummaryModel,
    S_COL_SYMBOL, S_COL_TYPE, S_COL_BUYS, S_COL_SELLS, S_COL_NET,
    S_COL_AVG_BUY, S_COL_AVG_SELL, S_COL_TRANS_TOTAL, S_COL_NET_TOTAL,
    S_COL_COMMISSION, S_COL_NET_INCL_COMM, S_COL_REALIZED, S_COL_EXCHANGES,
)

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Control-bar combo key → (days_back, session-open time-of-day in ET).
# ``None`` time means no preset threshold; just the rolling N-day
# window.  Morning uses 09:30 (US equities open); Afternoon 12:00;
# Evening 18:00 (after the US cash session close, covering the evening
# futures re-open and overnight trades).
_FILTER_PRESETS: dict[str, tuple[int, tuple[int, int] | None]] = {
    "evening":   (1, (18, 0)),
    "afternoon": (1, (12, 0)),
    "morning":   (1, (9, 30)),
    "today":     (1, None),
    "last_2":    (2, None),
    "last_3":    (3, None),
    "last_4":    (4, None),
    "last_5":    (5, None),
    "last_6":    (6, None),
    "last_7":    (7, None),
}

# ── Theme tokens ──────────────────────────────────────────────────────────────
BG_TABLE     = "#1A1D24"
BG_TABLE_ALT = "#1E222A"
BG_HEADER    = "#14171D"
BORDER       = "#30363D"
TEXT         = "#C9D1D9"
TEXT_DIM     = "#8B949E"

_TABLE_FONT = QFont("Menlo, Consolas, monospace", 11)

_TABLE_STYLE = f"""
    QTableView {{
        background-color: {BG_TABLE};
        alternate-background-color: {BG_TABLE_ALT};
        color: {TEXT};
        gridline-color: {BORDER};
        border: none;
        font-size: 11px;
        selection-background-color: #253545;
        selection-color: {TEXT};
    }}
    QHeaderView::section {{
        background-color: {BG_HEADER};
        color: {TEXT_DIM};
        border: none;
        border-bottom: 1px solid {BORDER};
        border-right: 1px solid {BORDER};
        padding: 4px 8px;
        font-weight: 700;
        font-size: 10px;
        letter-spacing: 0.06em;
    }}
    QTableView::item {{ padding: 0px 6px; }}
    QScrollBar:vertical {{
        background: {BG_TABLE};
        width: 8px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: #4A4E58;
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{ background: #5E6370; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar:horizontal {{
        background: {BG_TABLE};
        height: 10px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background: #4A4E58;
        border-radius: 4px;
        min-width: 40px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: #5E6370; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
"""


# Default selected columns per view (keys, in order).  Leaves advanced
# columns hidden so first-run looks like the reference screenshot.
_DEFAULT_FLAT_VISIBLE = [
    "time", "symbol", "sec_type", "side", "qty", "price",
    "exchange", "order_ref", "account", "expiry", "strike_right",
    "commission", "realized_pnl", "submitter",
]
_DEFAULT_SUMMARY_VISIBLE = [
    "symbol", "sec_type", "buys", "sells", "net", "avg_buy", "avg_sell",
    "trans_total", "net_total", "commission", "net_incl_comm",
    "realized_pnl", "exchanges",
]


class TradesPanel(QWidget):
    """Control-bar + stacked flat/summary tables, each behind a proxy.

    The gear button in the control bar does *not* open its own dialog.
    Instead ``open_preferences`` is called with the current view kind
    ("flat"/"summary") so the host can route the request to the global
    Preferences dialog (Columns section) at the right subview.
    """

    def __init__(self, parent=None, *,
                 open_preferences: Callable[[str], None] | None = None):
        super().__init__(parent)
        self._open_preferences = open_preferences
        self._flat_model = TradesModel(self)
        self._summary_model = TradesSummaryModel(self)

        self._flat_proxy = TradesFilterProxy(self, is_summary=False)
        self._flat_proxy.setSourceModel(self._flat_model)

        self._summary_proxy = TradesFilterProxy(self, is_summary=True)
        self._summary_proxy.setSourceModel(self._summary_model)

        # Load persisted column state (visible keys + widths) — falls back
        # to defaults on first run / missing keys.
        persisted = self._load_persisted_columns()
        self._visible_flat = persisted["flat"].get(
            "visible", list(_DEFAULT_FLAT_VISIBLE))
        self._visible_summary = persisted["summary"].get(
            "visible", list(_DEFAULT_SUMMARY_VISIBLE))
        # Migration: surface newly-introduced columns to users whose
        # persisted visible-list predates them.  Without this, upgrade
        # → new column hidden forever until the user opens Preferences.
        for new_key, after_key in (
                ("trans_total", "avg_sell"),
                ("sec_type", "symbol"),
        ):
            if new_key not in self._visible_summary:
                try:
                    i = self._visible_summary.index(after_key)
                    self._visible_summary.insert(i + 1, new_key)
                except ValueError:
                    self._visible_summary.append(new_key)
        self._saved_widths_flat: dict[str, int] = dict(
            persisted["flat"].get("widths", {}))
        self._saved_widths_summary: dict[str, int] = dict(
            persisted["summary"].get("widths", {}))

        # Debounced save – user drag/resize fires many events per second.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(350)
        self._save_timer.timeout.connect(self._save_persisted_columns)

        # Suppress save callbacks while we programmatically move/resize
        # sections during init and apply_columns.
        self._suspend_save = True

        self._setup_ui()
        self._apply_column_visibility("flat")
        self._apply_column_visibility("summary")

        self._wire_header_signals(self._flat_table, "flat")
        self._wire_header_signals(self._summary_table, "summary")

        self._suspend_save = False

    # ── UI construction ─────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Control bar is not added to our own layout — PositionsTradesTabs
        # reparents it into the QTabWidget's top-right corner so the
        # utility controls sit on the same row as the POSITIONS / TRADES
        # tabs.  Signal wiring still lives here since TradesPanel owns
        # the proxies the bar drives.
        self._control_bar = TradesControlBar(self)
        self._control_bar.summaryToggled.connect(self._on_summary_toggled)
        self._control_bar.dateFilterChanged.connect(self._on_date_changed)
        self._control_bar.searchTextChanged.connect(self._on_search_changed)
        self._control_bar.settingsClicked.connect(self._on_gear_clicked)

        # Keep the visible-trade counter in sync with the flat proxy.
        # invalidateFilter() (from search/date changes) emits layoutChanged;
        # incremental add_fill() emits rowsInserted; clear() → modelReset.
        self._flat_proxy.layoutChanged.connect(self._refresh_trade_count)
        self._flat_proxy.rowsInserted.connect(self._refresh_trade_count)
        self._flat_proxy.rowsRemoved.connect(self._refresh_trade_count)
        self._flat_proxy.modelReset.connect(self._refresh_trade_count)
        self._refresh_trade_count()

        self._stack = QStackedWidget(self)
        self._flat_table = self._make_table(self._flat_proxy)
        self._summary_table = self._make_table(self._summary_proxy)
        self._stack.addWidget(self._flat_table)      # index 0
        self._stack.addWidget(self._summary_table)   # index 1
        root.addWidget(self._stack, 1)

        self._apply_flat_default_widths()
        self._apply_summary_default_widths()

        # Summary-only: right-click menu + Delete-key shortcut to
        # temporarily hide rows from the totals footer.
        self._summary_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._summary_table.customContextMenuRequested.connect(
            self._on_summary_context_menu)
        delete_sc = QShortcut(
            QKeySequence(Qt.Key.Key_Delete), self._summary_table)
        delete_sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_sc.activated.connect(self._hide_current_summary_row)

    def _make_table(self, proxy) -> QTableView:
        t = QTableView(self)
        t.setModel(proxy)
        t.setAlternatingRowColors(True)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.verticalHeader().setVisible(False)
        t.setShowGrid(False)
        t.setFont(_TABLE_FONT)
        t.setStyleSheet(_TABLE_STYLE)
        t.verticalHeader().setDefaultSectionSize(30)

        hdr = t.horizontalHeader()
        hdr.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                 | Qt.AlignmentFlag.AlignVCenter)
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionsMovable(True)
        return t

    def _apply_flat_default_widths(self) -> None:
        fm = QFontMetrics(self._flat_table.font())
        pad = fm.horizontalAdvance("W")
        samples = {
            COL_TIME:         "Mmm00 00:00:00",
            COL_SYMBOL:       "WWWWWW",
            COL_SECTYPE:      "FUTW",
            COL_SIDE:         "BOTW",
            COL_QTY:          "99999",
            COL_PRICE:        "9999.99",
            COL_EXCH:         "NASDAQW",
            COL_REF:          "nano_0000_000000_wwww",
            COL_ACCOUNT:      "WWWWWWWWW",
            COL_EXPIRY:       "YYYYMMDD",
            COL_STRIKE_RIGHT: "99999 P",
            COL_COMM:         "999.99",
            COL_REAL_PNL:     "+9999.99",
            COL_ORDER_ID:     "999999",
            COL_SUBMITTER:    "qiancapital168",
            COL_LIQUIDATION:  "LIQ",
            COL_LAST_LIQ:     "LLQ",
        }
        for col, sample in samples.items():
            self._flat_table.setColumnWidth(col, fm.horizontalAdvance(sample) + pad)

    def _apply_summary_default_widths(self) -> None:
        fm = QFontMetrics(self._summary_table.font())
        pad = fm.horizontalAdvance("W")
        samples = {
            S_COL_SYMBOL:        "WWWWWW",
            S_COL_TYPE:          "WWWW",
            S_COL_BUYS:          "99999",
            S_COL_SELLS:         "99999",
            S_COL_NET:           "+99999",
            S_COL_AVG_BUY:       "99,999.99",
            S_COL_AVG_SELL:      "99,999.99",
            S_COL_TRANS_TOTAL:   "999.99m",
            S_COL_NET_TOTAL:     "+9,999,999.99",
            S_COL_COMMISSION:    "9,999.99",
            S_COL_NET_INCL_COMM: "+9,999,999.99",
            S_COL_REALIZED:      "+9,999.99",
            S_COL_EXCHANGES:     "NASDAQ, ARCA",
        }
        for col, sample in samples.items():
            self._summary_table.setColumnWidth(col, fm.horizontalAdvance(sample) + pad)

    # ── Control-bar handlers ────────────────────────────────────────────────

    def _on_summary_toggled(self, on: bool) -> None:
        self._stack.setCurrentIndex(1 if on else 0)

    def _on_date_changed(self, key: str) -> None:
        days, hm = _FILTER_PRESETS.get(key, (1, None))
        threshold: datetime | None = None
        if hm is not None:
            anchor = self._flat_proxy.anchorDate()
            h, m = hm
            threshold = datetime(
                anchor.year, anchor.month, anchor.day, h, m,
                tzinfo=_ET,
            )
        self._flat_proxy.setDaysBack(days)
        self._summary_model.set_days_back(days)
        self._flat_proxy.setPresetThreshold(threshold)
        self._summary_proxy.setPresetThreshold(threshold)
        self._refresh_summary_threshold()

    def _on_search_changed(self, text: str) -> None:
        self._flat_proxy.setSearchText(text)
        # Summary filtering happens at the fill level inside the model
        # — the proxy matches aggregate rows only and would hide every
        # row whenever the user types a per-fill field (e.g. account).
        # Route substring text to the model; datetime queries go
        # through ``_refresh_summary_threshold``.
        parsed = _try_parse_time_query(text)
        self._summary_model.set_search_text(
            "" if parsed is not None else text)
        self._refresh_summary_threshold()

    def _refresh_trade_count(self, *_args) -> None:
        """Push the flat proxy's visible-row count to the control bar.
        The *_args absorb whatever signal payload Qt passes in
        (rowsInserted/rowsRemoved carry parent/first/last)."""
        self._control_bar.setTradeCount(self._flat_proxy.rowCount())

    def _refresh_summary_threshold(self) -> None:
        """Summary model has a single ``_time_threshold`` slot; mirror
        the proxy's "preset beats search-parsed" priority so the flat
        view and summary totals stay in sync."""
        threshold = self._flat_proxy.presetThreshold()
        if threshold is None:
            threshold = _try_parse_time_query(self._control_bar.searchText())
        self._summary_model.set_time_threshold(threshold)

    # ── Summary row hide / restore ──────────────────────────────────────────

    def _summary_row_at(self, idx) -> tuple[tuple, str] | None:
        """Map a view-index on the summary table to ``(lookup_key, label)``.
        ``lookup_key`` is the model's internal ``_order`` key (``cid:…``-
        based) used for ``hide_symbol``; ``label`` is the human-readable
        Symbol/sec-type combo for the menu.  Returns ``None`` for invalid
        indexes or the totals footer row."""
        if not idx.isValid():
            return None
        src = self._summary_proxy.mapToSource(idx)
        key = self._summary_model.row_key(src.row())
        if key is None:
            return None
        agg = self._summary_model.row_data(src.row()) or {}
        sym = agg.get("symbol", "")
        if not sym:
            return None
        sec = agg.get("sec_type", "") or ""
        label = f"{sym} {sec}" if sec and sec != "STK" else sym
        return key, label

    def _on_summary_context_menu(self, pos) -> None:
        menu = QMenu(self._summary_table)
        info = self._summary_row_at(self._summary_table.indexAt(pos))

        hide_action = menu.addAction(f"Hide {info[1]}") if info else None

        restore_action = None
        n_hidden = self._summary_model.hidden_count()
        if n_hidden:
            suffix = "s" if n_hidden > 1 else ""
            restore_action = menu.addAction(
                f"Restore {n_hidden} hidden row{suffix}")

        if not menu.actions():
            return
        chosen = menu.exec(
            self._summary_table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is hide_action and info:
            self._summary_model.hide_symbol(info[0])
        elif chosen is restore_action:
            self._summary_model.restore_hidden()

    def _hide_current_summary_row(self) -> None:
        """Delete-key handler: hide the focused row."""
        info = self._summary_row_at(self._summary_table.currentIndex())
        if info:
            self._summary_model.hide_symbol(info[0])

    # ── Column visibility + order ───────────────────────────────────────────

    def _active_view_kind(self) -> str:
        return "summary" if self._stack.currentIndex() == 1 else "flat"

    def _on_gear_clicked(self) -> None:
        """Forward to the host's preferences router so the user lands on
        the right Columns subview."""
        if self._open_preferences is None:
            return
        self._open_preferences(self._active_view_kind())

    # ── ColumnsProvider protocol ────────────────────────────────────────────

    def column_state(self, kind: str
                     ) -> tuple[list[str], list[str], list[str]]:
        if kind == "summary":
            return (self._summary_model.column_keys(),
                    self._summary_model.column_headers(),
                    list(self._visible_summary))
        return (self._flat_model.column_keys(),
                self._flat_model.column_headers(),
                list(self._visible_flat))

    def apply_columns(self, kind: str, visible_keys: list[str]) -> None:
        if kind == "summary":
            self._visible_summary = list(visible_keys)
        else:
            self._visible_flat = list(visible_keys)
        self._apply_column_visibility(kind)
        # Re-snapshot so widths (possibly new columns) are captured too.
        self._snapshot_into_state(kind)
        self._save_timer.start()

    def _apply_column_visibility(self, kind: str) -> None:
        """Project the visible-keys list onto the QTableView header:
        hide non-selected columns, reorder selected ones to match, and
        apply any persisted widths on top."""
        prev_suspend = self._suspend_save
        self._suspend_save = True
        try:
            if kind == "flat":
                table = self._flat_table
                all_keys = self._flat_model.column_keys()
                visible = self._visible_flat
                saved_widths = self._saved_widths_flat
            else:
                table = self._summary_table
                all_keys = self._summary_model.column_keys()
                visible = self._visible_summary
                saved_widths = self._saved_widths_summary
            hdr = table.horizontalHeader()

            key_to_logical = {k: i for i, k in enumerate(all_keys)}

            # 1. Hide / show
            visible_set = set(visible)
            for k, logical in key_to_logical.items():
                hdr.setSectionHidden(logical, k not in visible_set)

            # 2. Reorder
            for target_visual, key in enumerate(visible):
                logical = key_to_logical.get(key)
                if logical is None:
                    continue
                current_visual = hdr.visualIndex(logical)
                if current_visual != target_visual:
                    hdr.moveSection(current_visual, target_visual)

            # 3. Apply persisted widths (no-op for columns not in saved state)
            for key, width in saved_widths.items():
                logical = key_to_logical.get(key)
                if logical is not None and width > 0:
                    table.setColumnWidth(logical, int(width))
        finally:
            self._suspend_save = prev_suspend

    # ── Header change plumbing → debounced persistence ──────────────────────

    def _wire_header_signals(self, table: QTableView, kind: str) -> None:
        hdr = table.horizontalHeader()
        hdr.sectionResized.connect(
            lambda _logical, _old, _new, k=kind: self._on_header_changed(k))
        hdr.sectionMoved.connect(
            lambda _logical, _from, _to, k=kind: self._on_header_changed(k))

    def _on_header_changed(self, kind: str) -> None:
        if self._suspend_save:
            return
        self._snapshot_into_state(kind)
        self._save_timer.start()

    def _snapshot_into_state(self, kind: str) -> None:
        """Read the table's current visual order + widths back into our
        in-memory state so what we persist matches what the user sees."""
        if kind == "flat":
            table = self._flat_table
            all_keys = self._flat_model.column_keys()
        else:
            table = self._summary_table
            all_keys = self._summary_model.column_keys()
        hdr = table.horizontalHeader()

        # Visible keys in current visual order, skipping hidden sections.
        visible: list[str] = []
        for visual in range(hdr.count()):
            logical = hdr.logicalIndex(visual)
            if logical < 0 or logical >= len(all_keys):
                continue
            if hdr.isSectionHidden(logical):
                continue
            visible.append(all_keys[logical])

        # Widths – store per key for every non-hidden column.
        widths: dict[str, int] = {}
        for logical, key in enumerate(all_keys):
            if hdr.isSectionHidden(logical):
                continue
            w = table.columnWidth(logical)
            if w > 0:
                widths[key] = int(w)

        if kind == "flat":
            self._visible_flat = visible
            self._saved_widths_flat = widths
        else:
            self._visible_summary = visible
            self._saved_widths_summary = widths

    # ── Persistence (preferences.trades_columns) ────────────────────────────

    def _load_persisted_columns(self) -> dict[str, dict]:
        """Returns ``{"flat": {"visible": [...], "widths": {...}},
        "summary": {...}}``.  Missing sections become empty dicts so
        callers can `.get(...)` into them with defaults."""
        try:
            prefs = config_manager.load_preferences()
        except Exception:
            log.exception("TradesPanel: load_preferences failed")
            return {"flat": {}, "summary": {}}
        tc = prefs.get("trades_columns", {})
        flat = tc.get("flat", {}) if isinstance(tc, dict) else {}
        summ = tc.get("summary", {}) if isinstance(tc, dict) else {}
        return {
            "flat": flat if isinstance(flat, dict) else {},
            "summary": summ if isinstance(summ, dict) else {},
        }

    def _save_persisted_columns(self) -> None:
        """Atomically merge our current column state into the preferences
        JSON.  Triggered by the debounce timer."""
        try:
            prefs = config_manager.load_preferences()
            prefs["trades_columns"] = {
                "flat": {
                    "visible": list(self._visible_flat),
                    "widths":  dict(self._saved_widths_flat),
                },
                "summary": {
                    "visible": list(self._visible_summary),
                    "widths":  dict(self._saved_widths_summary),
                },
            }
            config_manager.save_preferences(prefs)
        except Exception:
            log.exception("TradesPanel: save_preferences failed")

    # ── Public API (unchanged) ──────────────────────────────────────────────

    @property
    def control_bar(self) -> TradesControlBar:
        """Hand-off point for the host (``PositionsTradesTabs``) to park
        the utility controls in the QTabWidget's corner."""
        return self._control_bar

    def add_fill(self, fill: dict) -> None:
        self._flat_model.add_fill(fill)
        self._summary_model.add_fill(fill)

    def clear_fills(self) -> None:
        self._flat_model.clear()
        self._summary_model.clear()
