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

from PySide6.QtCore import QEvent, QItemSelectionModel, Qt, QTimer
from PySide6.QtGui import QFontMetrics, QKeySequence, QShortcut
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
from . import table_theme as tt
from .trades_control_bar import TradesControlBar
from .trades_date_filter import TradeWindow, window_for_key
from .trades_filter_proxy import TradesFilterProxy, _try_parse_time_query
from .trades_model import (
    TradesModel, HEADERS,
    COL_TIME, COL_SYMBOL, COL_SECTYPE, COL_SIDE, COL_QTY, COL_PRICE, COL_EXCH,
    COL_REF, COL_ACCOUNT, COL_EXPIRY, COL_STRIKE_RIGHT, COL_COMM, COL_REAL_PNL,
    COL_ORDER_ID, COL_SUBMITTER, COL_LIQUIDATION, COL_LAST_LIQ,
)
from .trades_summary_model import (
    TradesSummaryModel, S_HEADERS,
    S_COL_SYMBOL, S_COL_TYPE, S_COL_BUYS, S_COL_SELLS, S_COL_NET,
    S_COL_AVG_BUY, S_COL_AVG_SELL, S_COL_TRANS_TOTAL, S_COL_NET_TOTAL,
    S_COL_PAIRED_PNL, S_COL_UNPAIRED_PNL,
    S_COL_COMMISSION, S_COL_NET_INCL_COMM, S_COL_REALIZED, S_COL_EXCHANGES,
)

log = logging.getLogger(__name__)

# ── Theme ─────────────────────────────────────────────────────────────────────
# The table look is shared with the Positions tab — both tables sit behind
# the same tab bar, so any divergence reads as a glitch on tab change.
_PANEL_OBJECT_NAME = "TradesPanel"




# Default selected columns per view (keys, in order).  Leaves advanced
# columns hidden so first-run looks like the reference screenshot.
_DEFAULT_FLAT_VISIBLE = [
    "time", "symbol", "sec_type", "side", "qty", "price",
    "exchange", "order_ref", "account", "expiry", "strike_right",
    "commission", "realized_pnl", "submitter",
]
_DEFAULT_SUMMARY_VISIBLE = [
    "symbol", "sec_type", "buys", "sells", "net", "avg_buy", "avg_sell",
    "trans_total", "paired_pnl", "unpaired_pnl", "net_total",
    "commission", "net_incl_comm", "realized_pnl", "exchanges",
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
                ("paired_pnl", "trans_total"),
                ("unpaired_pnl", "paired_pnl"),
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

        # Header-click sort state per view kind: (logical column, order).
        # ``(-1, None)`` = unsorted (source order).  Clicking a caption
        # cycles descending → ascending → unsorted.
        self._sort_state: dict[str, tuple[int, Qt.SortOrder | None]] = {
            "flat": (-1, None),
            "summary": (-1, None),
        }

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
        # Same card as the Positions panel, so the two tabs don't shift
        # when you switch between them.
        self.setObjectName(_PANEL_OBJECT_NAME)
        self.setStyleSheet(tt.panel_card_qss(_PANEL_OBJECT_NAME))

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # Control bar is not added to our own layout — PositionsTradesTabs
        # reparents it into the QTabWidget's top-right corner so the
        # utility controls sit on the same row as the POSITIONS / TRADES
        # tabs.  Signal wiring still lives here since TradesPanel owns
        # the proxies the bar drives.
        self._control_bar = TradesControlBar(self)
        self._control_bar.summaryToggled.connect(self._on_summary_toggled)
        self._control_bar.dateFilterChanged.connect(self._on_date_changed)
        # The named-day entries are literal dates, so they go stale at the
        # ET midnight rollover — rebuild the combo and re-resolve when the
        # proxy notices the anchor advance.
        self._flat_proxy.anchorDateChanged.connect(self._on_anchor_changed)
        self._control_bar.searchTextChanged.connect(self._on_search_changed)
        self._control_bar.settingsClicked.connect(self._on_gear_clicked)

        # Keep the visible-trade counter in sync with whichever view is
        # active.  Flat: invalidateFilter() (search/date) emits
        # layoutChanged; incremental add_fill() emits rowsInserted;
        # clear() → modelReset.  Summary: hide_symbol() emits rowsRemoved,
        # restore_hidden()/filter rebuilds emit modelReset, and per-fill
        # folds emit dataChanged — all of which can change the visible
        # fill count, so the summary counter tracks user-hidden rows too.
        self._flat_proxy.layoutChanged.connect(self._refresh_trade_count)
        self._flat_proxy.rowsInserted.connect(self._refresh_trade_count)
        self._flat_proxy.rowsRemoved.connect(self._refresh_trade_count)
        self._flat_proxy.modelReset.connect(self._refresh_trade_count)
        self._summary_model.rowsInserted.connect(self._refresh_trade_count)
        self._summary_model.rowsRemoved.connect(self._refresh_trade_count)
        self._summary_model.modelReset.connect(self._refresh_trade_count)
        self._summary_model.dataChanged.connect(self._refresh_trade_count)

        # Mirror per-fill hides from the flat list into the summary model
        # so a trade hidden in the list also drops out of its aggregate.
        self._flat_proxy.hiddenKeysChanged.connect(
            self._sync_summary_hidden_fills)

        self._stack = QStackedWidget(self)
        self._flat_table, self._flat_row_delegate = \
            self._make_table(self._flat_proxy, tt.GroupBandDelegate)
        self._summary_table, self._summary_row_delegate = \
            self._make_table(self._summary_proxy)
        self._stack.addWidget(self._flat_table)      # index 0
        self._stack.addWidget(self._summary_table)   # index 1
        root.addWidget(self._stack, 1)

        # The section-header bands ("TRADE LEVEL" / "ACCOUNT LEVEL")
        # merge into one full-width cell — re-applied on any change that
        # can move rows (mirrors the Positions panel's _apply_spans).
        self._summary_proxy.rowsInserted.connect(self._apply_summary_spans)
        self._summary_proxy.rowsRemoved.connect(self._apply_summary_spans)
        self._summary_proxy.modelReset.connect(self._apply_summary_spans)
        self._summary_proxy.layoutChanged.connect(self._apply_summary_spans)
        self._apply_summary_spans()

        # Initial count — deferred until the stack exists since
        # _refresh_trade_count reads the active view kind from it.
        self._refresh_trade_count()

        self._apply_flat_default_widths()
        self._apply_summary_default_widths()

        # Both views allow multi-row selection (set in _make_table) so a
        # batch of rows can be temporarily hidden via the right-click menu
        # or the Delete key — summary hides whole symbol aggregates, flat
        # hides individual fills.
        self._summary_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._summary_table.customContextMenuRequested.connect(
            self._on_summary_context_menu)
        summary_delete_sc = QShortcut(
            QKeySequence(Qt.Key.Key_Delete), self._summary_table)
        summary_delete_sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        summary_delete_sc.activated.connect(self._hide_selected_summary_rows)

        self._flat_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._flat_table.customContextMenuRequested.connect(
            self._on_flat_context_menu)
        flat_delete_sc = QShortcut(
            QKeySequence(Qt.Key.Key_Delete), self._flat_table)
        flat_delete_sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        flat_delete_sc.activated.connect(self._hide_selected_flat_rows)

    def _make_table(self, proxy, delegate_cls=tt.FooterAwareRowDelegate
                    ) -> tuple[QTableView, tt.FooterAwareRowDelegate]:
        """Build one of the two trade tables.

        Returns the view and its row delegate — the caller keeps a
        reference so the delegate isn't garbage-collected and so tests can
        drive its hover state.
        """
        t = QTableView(self)
        t.setModel(proxy)
        # Shared chrome: no zebra, no gridlines, uppercase captions, and
        # viewport mouse tracking for the row-hover delegate.
        tt.apply_table_chrome(t)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Click selects a row; ⌘-click toggles individual rows and ⇧-click
        # extends a range — Qt's ExtendedSelection gives all three, and it
        # maps ⌘ to ControlModifier on macOS.  This is also what the hide
        # flows target: "Hide N selected trades" and the Delete shortcut.
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        # Row hover, plus the 1px rule between data rows.  The flat table
        # bands each order's fills (GroupBandDelegate) and puts the rule on
        # group boundaries only; the summary model's "P&L" / "Acct P&L"
        # footer rows answer IsFooterRowRole and are skipped either way.
        delegate = delegate_cls(t)
        t.setItemDelegate(delegate)

        hdr = t.horizontalHeader()
        hdr.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                 | Qt.AlignmentFlag.AlignVCenter)
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setSectionsMovable(True)
        # Caption clicks drive the desc → asc → unsorted cycle in
        # ``_on_header_sort_clicked``.  The view's own sortingEnabled stays
        # off — it only knows Qt's two-state asc/desc toggle.  The header
        # flips its indicator itself on click (that's built into
        # QHeaderView when sections are clickable); the handler overwrites
        # it with the cycle's actual state right after.
        hdr.setSectionsClickable(True)
        hdr.setSortIndicatorShown(False)
        return t, delegate

    def _apply_summary_spans(self, *_args) -> None:
        """Merge each section-header band of the summary view into one
        full-width cell, so the bands read as plain labels rather than
        a row of columns.

        clearSpans() first — resetting individual cells with
        setSpan(row, col, 1, 1) triggers Qt's "single cell span won't
        be added" warning for every ordinary row (same rationale as the
        Positions panel's _apply_spans)."""
        table = self._summary_table
        proxy = self._summary_proxy
        table.clearSpans()
        cols = proxy.columnCount()
        for row in range(proxy.rowCount()):
            idx = proxy.index(row, 0)
            rank = idx.data(tt.SectionRankRole)
            if rank == 3 or (
                    rank == 0 and idx.data(tt.IsFooterRowRole)):
                table.setSpan(row, 0, 1, cols)

    # ── Selection behaviour ─────────────────────────────────────────────────

    def hideEvent(self, event) -> None:
        """Drop any selection when this tab stops being the visible one.

        A selection here only ever means "rows I'm about to hide", so
        carrying it across a tab switch would leave a stale highlight
        waiting when you come back.
        """
        tt.clear_selections(getattr(self, "_flat_table", None),
                            getattr(self, "_summary_table", None))
        super().hideEvent(event)

    def _apply_flat_default_widths(self) -> None:
        samples = {
            COL_TIME:         "Mmm00 00:00:00",
            COL_SYMBOL:       "WWWWWW",
            COL_SECTYPE:      "FUTW",
            COL_SIDE:         "BOTW",
            COL_QTY:          "99999",
            COL_PRICE:        "9999.99",
            COL_EXCH:         "NASDAQW",
            COL_REF:          "nano_000000_000000_wwww",
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
        self._apply_default_widths(self._flat_table, samples, HEADERS)

    def _apply_summary_default_widths(self) -> None:
        samples = {
            S_COL_SYMBOL:        "WWWWWW",
            S_COL_TYPE:          "WWWW",
            S_COL_BUYS:          "99999",
            S_COL_SELLS:         "99999",
            S_COL_NET:           "+99999",
            S_COL_AVG_BUY:       "99,999.99",
            S_COL_AVG_SELL:      "99,999.99",
            S_COL_TRANS_TOTAL:   "999.99m",
            S_COL_PAIRED_PNL:    "+9,999,999.99",
            S_COL_UNPAIRED_PNL:  "+9,999,999.99",
            S_COL_NET_TOTAL:     "+9,999,999.99",
            S_COL_COMMISSION:    "9,999.99",
            S_COL_NET_INCL_COMM: "+9,999,999.99",
            S_COL_REALIZED:      "+9,999.99",
            S_COL_EXCHANGES:     "NASDAQ, ARCA",
        }
        self._apply_default_widths(self._summary_table, samples, S_HEADERS)

    @staticmethod
    def _apply_default_widths(table: QTableView, samples: dict[int, str],
                              headers: list[str]) -> None:
        """Size each column to fit its widest value *and* its caption —
        the uppercase tracked-out captions are wider than they look."""
        fm     = QFontMetrics(table.font())
        hdr_fm = QFontMetrics(tt.COL_HEADER_FONT)
        for col, sample in samples.items():
            table.setColumnWidth(
                col, tt.default_column_width(fm, hdr_fm, sample, headers[col]))

    # ── Control-bar handlers ────────────────────────────────────────────────

    def _on_summary_toggled(self, on: bool) -> None:
        self._stack.setCurrentIndex(1 if on else 0)
        # The two views count differently (flat = visible rows, summary =
        # fills behind the un-hidden aggregates) so re-source on toggle.
        self._refresh_trade_count()

    def _on_date_changed(self, key: str) -> None:
        """Resolve the combo key to a window and push it to both views.

        Every entry — session preset, named day, rolling span,
        week-to-date — is one ``TradeWindow``, so this is the whole
        mapping the panel used to spell out as a preset table plus a
        threshold/days_back split.
        """
        window = window_for_key(key, self._flat_proxy.anchorDate())
        self._flat_proxy.setWindow(window)
        self._summary_model.set_window(window)
        self._refresh_search_window()

    def _on_anchor_changed(self) -> None:
        """ET midnight rolled over under a running app.

        The named-day entries are literal dates, so the combo is rebuilt
        against the new anchor.  Every window is re-resolved either way:
        even "Today" is yesterday's midnight until it is rebuilt.
        """
        self._control_bar.rebuildDateOptions()
        self._on_date_changed(self._control_bar.filterKey())

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
        self._refresh_search_window()

    def _refresh_trade_count(self, *_args) -> None:
        """Push the active view's trade count to the control bar.  Flat
        view reports its visible rows; summary view reports the number of
        fills behind the un-hidden aggregates so hiding a summary row
        drops the count.  The *_args absorb whatever signal payload Qt
        passes in (rowsInserted/rowsRemoved carry parent/first/last;
        dataChanged carries topLeft/bottomRight/roles)."""
        if self._active_view_kind() == "summary":
            count = self._summary_model.visible_fill_count()
        else:
            count = self._flat_proxy.rowCount()
        # Price ticks fire the summary's dataChanged without changing any
        # fill count — skip the control-bar restyle (a full QSS re-polish
        # of the label) unless the number actually moved.
        if count == getattr(self, "_last_trade_count", None):
            return
        self._last_trade_count = count
        self._control_bar.setTradeCount(count)

    def _refresh_search_window(self) -> None:
        """Mirror the search box's parsed datetime into the summary
        model.  The precedence against the combo's window lives in one
        place (``TradeWindow`` consumers' ``effective_window``), so the
        flat view and the summary totals resolve it identically."""
        parsed = _try_parse_time_query(self._control_bar.searchText())
        self._summary_model.set_search_window(
            TradeWindow.from_threshold(parsed) if parsed else None)

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

    def _selected_summary_rows(self) -> list[tuple[tuple, str]]:
        """``(lookup_key, label)`` for every selected summary row, skipping
        invalid indexes and the totals footer.  Returns at most one entry
        per row (``selectedRows`` already de-dupes per row)."""
        sel = self._summary_table.selectionModel()
        if sel is None:
            return []
        infos: list[tuple[tuple, str]] = []
        for idx in sel.selectedRows():
            info = self._summary_row_at(idx)
            if info:
                infos.append(info)
        return infos

    def _hide_rows(self, infos: list[tuple[tuple, str]]) -> None:
        """Hide each row by its lookup key.  Keys are resolved before any
        removal so the shifting row indices during the loop don't matter."""
        for key, _label in infos:
            self._summary_model.hide_symbol(key)

    def _on_summary_context_menu(self, pos) -> None:
        menu = QMenu(self._summary_table)
        selected = self._selected_summary_rows()
        clicked = self._summary_row_at(self._summary_table.indexAt(pos))

        # Right-clicking a row outside the current selection targets just
        # that row; clicking inside (or with several rows selected) acts
        # on the whole selection.
        selected_keys = {k for k, _ in selected}
        if clicked and clicked[0] not in selected_keys:
            targets = [clicked]
        else:
            targets = selected or ([clicked] if clicked else [])

        hide_action = None
        if targets:
            if len(targets) == 1:
                hide_action = menu.addAction(f"Hide {targets[0][1]}")
            else:
                hide_action = menu.addAction(
                    f"Hide {len(targets)} selected rows")

        # Match buy & sell — single-row action on the right-clicked row.
        # ("&&" renders a literal ampersand; a single "&" is a mnemonic.)
        match_action = None
        unmatch_action = None
        if clicked:
            ckey = clicked[0]
            if self._summary_model.is_matched(ckey):
                unmatch_action = menu.addAction("Restore full buy/sell")
            elif self._summary_model.can_match(ckey):
                match_action = menu.addAction("Match buy && sell")

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
        if chosen is hide_action and targets:
            self._hide_rows(targets)
        elif chosen is match_action and clicked:
            self._summary_model.match_symbol(clicked[0])
        elif chosen is unmatch_action and clicked:
            self._summary_model.unmatch_symbol(clicked[0])
        elif chosen is restore_action:
            self._summary_model.restore_hidden()

    def _hide_selected_summary_rows(self) -> None:
        """Delete-key handler: hide every selected row, falling back to the
        focused row when the selection is empty."""
        infos = self._selected_summary_rows()
        if not infos:
            info = self._summary_row_at(self._summary_table.currentIndex())
            if info:
                infos = [info]
        self._hide_rows(infos)

    # ── Flat row hide / restore ─────────────────────────────────────────────

    def _flat_row_at(self, idx) -> tuple[str, str] | None:
        """Map a view-index on the flat table to ``(exec_id, label)``.
        ``exec_id`` is the per-fill identity the proxy hides on; ``label``
        is a short ``side qty symbol`` descriptor for the menu.  Returns
        ``None`` for invalid indexes or fills without an exec_id (which
        can't be reliably hidden)."""
        if not idx.isValid():
            return None
        src = self._flat_proxy.mapToSource(idx)
        data = self._flat_model.row_data(src.row()) or {}
        exec_id = data.get("exec_id", "")
        if not exec_id:
            return None
        sym = data.get("local_symbol", "") or data.get("symbol", "") or "—"
        side = data.get("side", "") or ""
        try:
            qty = int(float(data.get("shares", 0) or 0))
            qty_s = str(qty) if qty else ""
        except (TypeError, ValueError):
            qty_s = ""
        parts = [p for p in (side, qty_s, sym) if p]
        return exec_id, " ".join(parts) if parts else sym

    def _selected_flat_rows(self) -> list[tuple[str, str]]:
        """``(exec_id, label)`` for every selected flat row, skipping
        invalid rows and fills without an exec_id."""
        sel = self._flat_table.selectionModel()
        if sel is None:
            return []
        infos: list[tuple[str, str]] = []
        for idx in sel.selectedRows():
            info = self._flat_row_at(idx)
            if info:
                infos.append(info)
        return infos

    def _on_flat_context_menu(self, pos) -> None:
        menu = QMenu(self._flat_table)
        selected = self._selected_flat_rows()
        clicked = self._flat_row_at(self._flat_table.indexAt(pos))

        # Right-clicking a row outside the current selection targets just
        # that row; clicking inside (or with several rows selected) acts
        # on the whole selection.
        selected_keys = {k for k, _ in selected}
        if clicked and clicked[0] not in selected_keys:
            targets = [clicked]
        else:
            targets = selected or ([clicked] if clicked else [])

        hide_action = None
        if targets:
            if len(targets) == 1:
                hide_action = menu.addAction(f"Hide {targets[0][1]}")
            else:
                hide_action = menu.addAction(
                    f"Hide {len(targets)} selected trades")

        restore_action = None
        n_hidden = self._flat_proxy.hiddenCount()
        if n_hidden:
            suffix = "s" if n_hidden > 1 else ""
            restore_action = menu.addAction(
                f"Restore {n_hidden} hidden trade{suffix}")

        if not menu.actions():
            return
        chosen = menu.exec(self._flat_table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is hide_action and targets:
            self._flat_proxy.hideKeys([k for k, _ in targets])
        elif chosen is restore_action:
            self._flat_proxy.restoreHidden()

    def _hide_selected_flat_rows(self) -> None:
        """Delete-key handler: hide every selected flat row, falling back
        to the focused row when the selection is empty."""
        infos = self._selected_flat_rows()
        if not infos:
            info = self._flat_row_at(self._flat_table.currentIndex())
            if info:
                infos = [info]
        if infos:
            self._flat_proxy.hideKeys([k for k, _ in infos])

    def _sync_summary_hidden_fills(self) -> None:
        """Push the flat view's hidden exec_ids into the summary model so a
        fill hidden in the list also disappears from its symbol aggregate.
        Driven by ``TradesFilterProxy.hiddenKeysChanged``."""
        self._summary_model.set_hidden_fills(self._flat_proxy.hiddenKeys())

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
        hdr.sectionClicked.connect(
            lambda logical, k=kind: self._on_header_sort_clicked(k, logical))

    # ── Header-click sorting (desc → asc → unsorted) ────────────────────────

    def _on_header_sort_clicked(self, kind: str, logical: int) -> None:
        """Cycle the clicked column through descending → ascending →
        unsorted.  Clicking a different column starts its cycle fresh at
        descending.  Unsorted restores the source model's natural order
        (flat: newest fill first; summary: first-fill insertion order)."""
        if kind == "flat":
            table, proxy = self._flat_table, self._flat_proxy
        else:
            table, proxy = self._summary_table, self._summary_proxy
        hdr = table.horizontalHeader()

        col, order = self._sort_state.get(kind, (-1, None))
        if col != logical:
            col, order = logical, Qt.SortOrder.DescendingOrder
        elif order == Qt.SortOrder.DescendingOrder:
            order = Qt.SortOrder.AscendingOrder
        else:
            col, order = -1, None

        self._sort_state[kind] = (col, order)
        if order is None:
            # sort(-1) is Qt's documented "back to source order".
            proxy.sort(-1)
            hdr.setSortIndicatorShown(False)
            hdr.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        else:
            proxy.sort(col, order)
            hdr.setSortIndicatorShown(True)
            hdr.setSortIndicator(col, order)

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

    # Bumped when a restyle changes what a column needs to be wide enough
    # for.  v2 = the shared table look (wider cell padding, uppercase
    # captions, tabular figures).
    _STYLE_VERSION = 2

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
        if not isinstance(tc, dict):
            return {"flat": {}, "summary": {}}
        flat = tc.get("flat", {})
        summ = tc.get("summary", {})
        flat = flat if isinstance(flat, dict) else {}
        summ = summ if isinstance(summ, dict) else {}

        if tc.get("style_v", 1) < self._STYLE_VERSION:
            # Widths saved against the old font and padding would truncate
            # their own values now.  Drop them so the defaults recompute;
            # the user's column choice and order (visible) are kept.
            flat = {k: v for k, v in flat.items() if k != "widths"}
            summ = {k: v for k, v in summ.items() if k != "widths"}
            log.info("TradesPanel: retired column widths saved before the "
                     "restyle (style_v < %d)", self._STYLE_VERSION)

        return {"flat": flat, "summary": summ}

    def _save_persisted_columns(self) -> None:
        """Atomically merge our current column state into the preferences
        JSON.  Triggered by the debounce timer."""
        try:
            prefs = config_manager.load_preferences()
            prefs["trades_columns"] = {
                "style_v": self._STYLE_VERSION,
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

    def set_selected_accounts(self, accounts) -> None:
        """Follow the Account List panel's checked accounts (raw IB ids;
        empty = show all).  Flat rows filter in the proxy; the summary
        re-aggregates from only the matching fills."""
        self._flat_proxy.setSelectedAccounts(accounts)
        self._summary_model.set_selected_accounts(accounts)

    def pnl_totals(self, accounts=None) -> tuple[float, float, float, float]:
        """(R.P&L, U.P&L, P&L, commission) across *accounts* (raw IB ids;
        empty/None = all) over the control bar's current time frame — the
        status bar's aggregate.  Follows whatever the date combo is set
        to but ignores the view's search text, account filter, and
        hidden rows; ``effectiveWindow`` folds in a datetime typed into
        the search box, matching what the flat view shows."""
        return self._summary_model.pnl_totals(
            accounts, self._flat_proxy.effectiveWindow())

    def pnl_timeframe_label(self) -> str:
        """Display label of the active time frame ("Today", "Morning",
        "Last 3 Days", …) for the status bar caption."""
        return self._control_bar.filterLabel()

    def update_last_price(self, symbol: str, price: float) -> None:
        """Live last-trade price feed — the summary's P&L footer row
        marks unclosed positions to it."""
        self._summary_model.set_last_price(symbol, price)

    def clear_fills(self) -> None:
        self._flat_model.clear()
        self._summary_model.clear()
        # Reset the flat hide set too (and, via its signal, the summary's
        # mirrored copy) so a fresh fill stream starts with nothing hidden.
        self._flat_proxy.restoreHidden()
