"""
Main application window for NanoPulse Trading Platform.
"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QFrame,
    QLineEdit,
    QPlainTextEdit,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QPainter, QColor, QShortcut

from .widgets.top_tab_bar import TopTabBar
from .widgets.statusbar import StatusBar
from .widgets.accounts_model import AccountsTableModel
from .widgets.connections_view import ConnectionsView
from .widgets.flash_trade_view import FlashTradeView
from .widgets.strategy_editor import StrategyEditorView
from .widgets.preferences_dialog import PreferencesDialog
from src.core.ib_worker import IBWorkerThread
from src.core import config_manager

log = logging.getLogger(__name__)

_C_WINDOW_BG = QColor("#262E3F")

# Tab label → stacked-widget index
_PAGE_INDEX: dict[str, int] = {
    "Connect":        0,
    "Nano":           1,
    "StrategyEditor": 2,
}


class MainWindow(QWidget):
    """Top-level window.  Uses plain QWidget (not QMainWindow) so there is
    zero hidden chrome from menuBar / statusBar / centralWidget plumbing."""

    # Emitted when the signed-in user asks to sign out (from Preferences).
    # The app controller listens and swaps back to the login window.
    sign_out_requested = Signal()

    def __init__(self, current_user=None, auth_service=None):
        super().__init__()
        self.setWindowTitle("NanoPulse")
        self.resize(1280, 840)
        self.setMinimumSize(900, 600)
        self.setContentsMargins(0, 0, 0, 0)

        # Authenticated session (may be None for legacy / test construction).
        self._current_user = current_user
        self._auth_service = auth_service

        # Session lock (⌘L / padlock button).  The overlay is created
        # lazily on first lock; while locked, global shortcuts are
        # suppressed and the workers keep running underneath.
        self._locked = False
        self._lock_overlay = None

        # Shared data structures
        self._accounts_model = AccountsTableModel(self)
        self.active_threads: dict[str, IBWorkerThread] = {}

        # Pre-warm the strategy-tier cache so tier chips are available
        # when the first widgets are constructed below.  Contract
        # identity lives on the chip/hot-button entries themselves
        # (src/core/instruments.py) — nothing to pre-warm.
        from src.core.reference_data import get_instance as _rd
        _rd().reload_strategy_tiers()

        # Enrich bare chip / hot-button entries with contract metadata
        # from the legacy contracts.db (idempotent; also runs on sign-in
        # via set_active_config — this covers legacy/test construction).
        try:
            from src.core import instruments
            instruments.migrate_active_config_from_db()
        except Exception:
            log.exception("Instrument migration failed")

        self._setup_ui()
        self._setup_shortcuts()
        self._apply_role_gating()
        self._restore_geometry()

        # Self-healing contract metadata: whenever a worker connects,
        # chips/hot-buttons missing a conid (or holding an expired FUT)
        # are re-qualified via TWS and their subscriptions refreshed.
        from src.core.instrument_qualifier import InstrumentQualifier
        self._instrument_qualifier = InstrumentQualifier(
            self.active_threads, self)
        self._instrument_qualifier.contract_requalified.connect(
            self._flash_trade._on_contract_replaced)

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start(1000)
        self._update_status()

        log.info("MainWindow initialised (%dx%d)", self.width(), self.height())

    def _update_status(self) -> None:
        # Attach the re-qualifier to any worker created since the last
        # tick (workers are created dynamically on the Connect page).
        self._instrument_qualifier.scan_workers()
        # Selected = checked accounts in the Account List panel.
        try:
            selected_count = self._flash_trade._account_list.get_selected_count()
        except AttributeError:
            selected_count = 0
        # Active filter → blue chip centered in the tab bar.  Shows
        # the active group's name (or "All" / "None" for the built-in
        # defaults) with the selected-account count appended, e.g.
        # "Alpha 2".  Hidden for a hand-picked subset that doesn't
        # match any named group.
        try:
            label = self._flash_trade._account_list.active_filter_label()
        except AttributeError:
            label = None
        self._tab_bar.set_active_group(label, selected_count)

        # Transaction P&L across ALL accounts on the status bar —
        # deliberately independent of the checked-account selection so
        # the bar always reports the whole book.  Computed from
        # in-memory fills + cached prices — a few ms worst case, so the
        # 1 s cadence is effectively free.
        try:
            panel = self._flash_trade._trades_panel
            self._status_bar.set_pnl_summary(
                panel.pnl_totals(None), panel.pnl_timeframe_label())
        except AttributeError:
            self._status_bar.set_pnl_summary(None)

    # ── Window geometry persistence ───────────────────────────────────────────

    def _restore_geometry(self) -> None:
        layout = config_manager.load_layout()
        geom = layout.get("main_window")
        if geom and isinstance(geom, dict):
            self.resize(geom.get("w", 1280), geom.get("h", 840))
            if "x" in geom and "y" in geom:
                self.move(geom["x"], geom["y"])
            if geom.get("maximized"):
                self.showMaximized()

    def _save_geometry(self) -> None:
        layout = config_manager.load_layout()
        layout["main_window"] = {
            "x": self.x(), "y": self.y(),
            "w": self.width(), "h": self.height(),
            "maximized": self.isMaximized(),
        }
        config_manager.save_layout(layout)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ── Top tab bar ───────────────────────────────────────────────────────
        self._tab_bar = TopTabBar(self)
        self._tab_bar.nav_changed.connect(self._on_nav_changed)
        self._tab_bar.tabBarClicked.connect(
            self._on_tab_clicked)  # Handle all tab clicks
        root_layout.addWidget(self._tab_bar)

        # ── Stacked pages ─────────────────────────────────────────────────────
        self._pages = QStackedWidget(self)
        self._pages.setFrameShape(QFrame.Shape.NoFrame)
        self._pages.setContentsMargins(0, 0, 0, 0)
        self._pages.setLineWidth(0)

        self._connections_view = ConnectionsView(
            accounts_model=self._accounts_model,
            active_threads=self.active_threads,
            parent=self,
        )                                                  # index 0 – Connect
        self._flash_trade = FlashTradeView(
            active_threads=self.active_threads,
            accounts_model=self._accounts_model,
            parent=self,
            open_preferences=self._open_trades_preferences,
        )                                                  # index 1 – FlashTrade
        self._strategy_editor = StrategyEditorView(
            parent=self,
        )                                                  # index 2 – StrategyEditor

        # ── Account pill toggles the Account Management panel ───────────────
        # The pill is always visible.  Click toggles the panel; when
        # the user is on a non-Nano tab, the click also switches to
        # the Nano tab so the toggle is visible.
        self._tab_bar.accountsToggleRequested.connect(self._toggle_accounts)
        self._tab_bar.lockRequested.connect(self.lock_session)
        # The padlock needs a user to re-authenticate — hide it for
        # legacy / test construction with no auth session.
        self._tab_bar.set_lock_visible(
            self._current_user is not None and self._auth_service is not None)

        self._pages.addWidget(self._connections_view)
        self._pages.addWidget(self._flash_trade)
        self._pages.addWidget(self._strategy_editor)
        self._pages.setCurrentIndex(0)

        root_layout.addWidget(self._pages, stretch=1)

        # ── Status bar ────────────────────────────────────────────────────────
        self._status_bar = StatusBar(self)
        self._status_bar.setAttribute(
            Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        root_layout.addWidget(self._status_bar)

        # Surface FlashTradeView's transient messages (e.g. "Order skipped
        # on N disconnected account(s): …") in the status bar so the user
        # gets a non-blocking confirmation that some accounts were skipped.
        self._flash_trade.transient_status_message.connect(
            self._status_bar.show_message)

    # ── Keyboard shortcuts ───────────────────────────────────────────────────

    def _setup_shortcuts(self) -> None:
        """Window-wide symbol-search hotkeys:

        * ``/``        → ladder (Book Trader) symbol search
        * ``Shift+/``  → stock order view (OrderEntry) symbol search

        Implemented as an application event filter rather than
        QShortcut because Shift+/ arrives as the ``?`` key and
        QShortcut's shift-modified matching is platform-dependent.
        Both keys are suppressed while a text-entry widget has focus so
        typing a literal "/" or "?" in an input never hijacks the
        keystroke.

        ``Ctrl+L`` (⌘L on macOS) locks the session — same action as the
        tab bar's padlock button.
        """
        QApplication.instance().installEventFilter(self)
        self._lock_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self._lock_shortcut.setContext(
            Qt.ShortcutContext.ApplicationShortcut)
        self._lock_shortcut.activated.connect(self.lock_session)

    def eventFilter(self, obj, event):  # noqa: N802 – Qt override
        if (event.type() == QEvent.Type.KeyPress
                and not self._locked
                and isinstance(obj, QWidget)
                and not event.isAutoRepeat()
                and event.key() in (Qt.Key.Key_Slash, Qt.Key.Key_Question)
                and QApplication.activeWindow() is self):
            fw = QApplication.focusWidget()
            if not isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit,
                                   QAbstractSpinBox)):
                self._open_symbol_search(
                    "ladder" if event.key() == Qt.Key.Key_Slash
                    else "order")
                return True
        return super().eventFilter(obj, event)

    def _open_symbol_search(self, target: str) -> None:
        """Open the add-symbol search popup on the Nano page.

        Navigates to the Nano tab first if needed so the popup can
        anchor to its visible "+" button.  No-op for admin sessions
        (no trade surfaces) and while the user is typing in an input.
        """
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit,
                           QAbstractSpinBox)):
            return
        user = self._current_user
        if user is not None and not user.can_trade:
            return

        def _open() -> None:
            if target == "ladder":
                self._flash_trade._book_trading.open_symbol_search()
            else:
                self._flash_trade._order_entry.open_symbol_search()

        if self._pages.currentIndex() != _PAGE_INDEX["Nano"]:
            for label, btn in self._tab_bar._buttons.items():
                btn.set_active(label == "Nano")
            self._on_nav_changed("Nano")
            # Let the page-switch layout settle before anchoring the
            # popup to the "+" button's on-screen position.
            QTimer.singleShot(0, _open)
        else:
            _open()

    # ── Role gating ─────────────────────────────────────────────────────────────

    def _apply_role_gating(self) -> None:
        """Admins manage users but cannot trade — hide the trade surfaces and
        disable order placement.  Traders keep the full trading UI.  No user
        (legacy / test construction) leaves everything visible."""
        user = self._current_user
        if user is None or user.can_trade:
            return

        # Admin session: hide trade tabs + Account pill, block order placement.
        for tab in ("Nano", "StrategyEditor"):
            self._tab_bar.set_tab_visible(tab, False)
        self._tab_bar.set_accounts_pill_visible(False)
        self._flash_trade.set_trading_allowed(False)

        # Land on a non-trade page.
        for label, btn in self._tab_bar._buttons.items():
            btn.set_active(label == "Connect")
        self._on_nav_changed("Connect")
        log.info("Role gating applied for admin user %r", user.username)

    def _request_sign_out(self) -> None:
        self.sign_out_requested.emit()

    # ── Session lock ──────────────────────────────────────────────────────────

    def lock_session(self) -> None:
        """Cover the window with an opaque lock overlay (⌘L / padlock /
        Preferences → Lock).  Workers, subscriptions and working orders
        keep running underneath — this hides the UI, it doesn't stop the
        session.  No-op without an authenticated user (nothing to
        re-authenticate) or when already locked."""
        if (self._locked or self._current_user is None
                or self._auth_service is None):
            return
        # Floating windows would leak data around the overlay — close
        # them first.  Preferences is modeless; symbol-search popups are
        # frameless Tool windows anchored to buttons.
        for dlg in self.findChildren(PreferencesDialog):
            dlg.close()
        from src.ui.widgets.symbol_search_popup import SymbolSearchPopup
        for popup in self.findChildren(SymbolSearchPopup):
            popup.close()
        if self._lock_overlay is None:
            from src.ui.auth.lock_overlay import LockOverlay

            def _feed_live() -> bool:
                return any(
                    t.isRunning() and t.is_ib_connected
                    for t in self.active_threads.values())

            self._lock_overlay = LockOverlay(
                self._current_user, self._auth_service, self,
                feed_live_provider=_feed_live)
            self._lock_overlay.unlocked.connect(self._on_unlocked)
            self._lock_overlay.sign_out_requested.connect(
                self._request_sign_out)
        self._locked = True
        self._lock_overlay.activate()
        log.info("Session locked by %r", self._current_user.username)

    def _on_unlocked(self) -> None:
        self._locked = False
        if self._lock_overlay is not None:
            self._lock_overlay.hide()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._lock_overlay is not None and self._locked:
            self._lock_overlay.setGeometry(self.rect())

    # ── Navigation ────────────────────────────────────────────────────────────

    def _on_nav_changed(self, name: str) -> None:
        idx = _PAGE_INDEX.get(name)
        if idx is not None:
            log.debug("Nav → %s (page %d)", name, idx)
            self._pages.setCurrentIndex(idx)

    def _toggle_accounts(self) -> None:
        """Pill click handler — toggle the Account Management panel.
        When the user is on a non-Nano tab, navigate there first so
        the toggle is observable."""
        if self._pages.currentIndex() != _PAGE_INDEX["Nano"]:
            for label, btn in self._tab_bar._buttons.items():
                btn.set_active(label == "Nano")
            self._on_nav_changed("Nano")
            # When we just switched to Nano, force-show the panel
            # rather than toggling — otherwise the user clicked
            # "Account" intending to see it and got nothing visible.
            self._flash_trade.set_accounts_panel_visible(True)
            return
        # Already on Nano — pure toggle.
        self._flash_trade.set_accounts_panel_visible(
            not self._flash_trade.is_accounts_panel_visible())

    def _on_tab_clicked(self, name: str) -> None:
        """Handle tab bar clicks - intercept Settings tab and launch modeless dialog."""
        if name == "Settings":
            self._launch_preferences(modeless=True)
        else:
            # Normal tab navigation
            self._on_nav_changed(name)

    def _on_settings(self) -> None:
        """Open the Preferences dialog."""
        self._launch_preferences(modeless=False)

    def _launch_preferences(self, *, modeless: bool,
                            section: str | None = None,
                            subview: str | None = None) -> "PreferencesDialog":
        """Single entry point for opening the global Preferences dialog.

        ``section`` / ``subview`` are used by gear buttons to land on
        Columns → Positions / Trades / Summary without a second click.
        """
        class _CombinedProvider:
            _POS_KINDS = ("positions", "positions_summary")

            def __init__(self, trades_panel, position_panel):
                self._trades = trades_panel
                self._positions = position_panel

            def column_state(self, kind):
                if kind in self._POS_KINDS:
                    return self._positions.column_state(kind)
                return self._trades.column_state(kind)

            def apply_columns(self, kind, keys):
                if kind in self._POS_KINDS:
                    self._positions.apply_columns(kind, keys)
                else:
                    self._trades.apply_columns(kind, keys)

        provider = _CombinedProvider(
            self._flash_trade.trades_panel,
            self._flash_trade.account_position,
        )
        auth_context = None
        if self._current_user is not None:
            auth_context = {
                "user": self._current_user,
                "service": self._auth_service,
                "on_sign_out": self._request_sign_out,
                "on_lock": self.lock_session,
            }
        from src.ui.widgets.symbol_search_popup import (
            connected_workers_by_priority,
        )
        dlg = PreferencesDialog(
            self, columns_provider=provider, auth_context=auth_context,
            workers=connected_workers_by_priority(self.active_threads))
        dlg.preferences_saved.connect(
            self._flash_trade.refresh_market_data_subscriptions)
        if section is not None:
            dlg.navigate_to(section, subview)
        if modeless:
            dlg.show()
        else:
            dlg.exec()
        return dlg

    def _open_trades_preferences(self, view_kind: str) -> None:
        """Callback wired into gear buttons.  ``view_kind`` is
        ``"positions"``, ``"flat"``, or ``"summary"``."""
        self._launch_preferences(modeless=True, section="Columns",
                                 subview=view_kind)

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), _C_WINDOW_BG)
        p.end()

    # ── Title-bar double-click (maximise / restore) ───────────────────────────

    def event(self, event):
        if event.type() == QEvent.Type.NonClientAreaMouseButtonDblClick:
            if event.button() == Qt.MouseButton.LeftButton:
                if self.isMaximized() or self.isFullScreen():
                    self.showNormal()
                else:
                    self.showMaximized()
                return True
        return super().event(event)

    # ── Graceful shutdown ─────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        """Save geometry and stop all worker threads before closing."""
        self._save_geometry()

        # Persist order-status column widths
        self._flash_trade._order_status.save_table_state()

        log.info("closeEvent – shutting down %d worker(s)",
                 len(self.active_threads))

        # Signal all workers to stop
        for label, worker in list(self.active_threads.items()):
            log.info("Requesting worker shutdown: %s", label)
            worker.stop()

        # Wait for all workers to finish with a longer timeout (10 seconds per worker)
        all_stopped = True
        for label, worker in list(self.active_threads.items()):
            log.debug("Waiting for worker %s to finish...", label)
            if not worker.wait(3000):  # 3 second timeout – tasks are cancelled immediately
                log.warning(
                    "Worker %s did not stop gracefully after 3s", label)
                all_stopped = False
            else:
                log.debug("Worker %s stopped successfully", label)

        # Clear the threads dict
        self.active_threads.clear()

        if all_stopped:
            log.info("All workers stopped gracefully")
        else:
            log.warning("Some workers did not stop gracefully – forcing exit")

        super().closeEvent(event)
