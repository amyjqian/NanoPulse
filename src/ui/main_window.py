"""
Main application window for NanoPulse Trading Platform.
"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QFrame,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QPainter, QColor

from .widgets.top_tab_bar import TopTabBar
from .widgets.canvas import Canvas
from .widgets.statusbar import StatusBar
from .widgets.accounts_model import AccountsTableModel
from .widgets.connections_view import ConnectionsView
from .widgets.contracts_view import ContractsView
from .widgets.flash_trade_view import FlashTradeView
from .widgets.strategy_editor import StrategyEditorView
from .widgets.preferences_dialog import PreferencesDialog
from src.core.ib_worker import IBWorkerThread
from src.core import config_manager

log = logging.getLogger(__name__)

_C_WINDOW_BG = QColor("#30363D")

# Tab label → stacked-widget index
_PAGE_INDEX: dict[str, int] = {
    "Connect":        0,
    "Contracts":      1,
    "Nano":           2,
    "StrategyEditor": 3,
    "Executions":     4,
}


class MainWindow(QWidget):
    """Top-level window.  Uses plain QWidget (not QMainWindow) so there is
    zero hidden chrome from menuBar / statusBar / centralWidget plumbing."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("NanoPulse")
        self.resize(1280, 840)
        self.setMinimumSize(900, 600)
        self.setContentsMargins(0, 0, 0, 0)

        # Shared data structures
        self._accounts_model = AccountsTableModel(self)
        self.active_threads: dict[str, IBWorkerThread] = {}

        # Pre-warm the reference data cache so contracts and strategy tiers
        # are available when the first widgets are constructed below.
        from src.core.reference_data import get_instance as _rd
        _rd().reload_contracts()
        _rd().reload_strategy_tiers()

        self._setup_ui()
        self._restore_geometry()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start(1000)
        self._update_status()

        log.info("MainWindow initialised (%dx%d)", self.width(), self.height())

    def _update_status(self) -> None:
        conn_count = sum(
            1 for w in self.active_threads.values()
            if w._ib and w._ib.isConnected()
        )
        acct_count = self._accounts_model.rowCount()
        # Selected = checked accounts in the Account List panel
        try:
            selected_count = self._flash_trade._account_list.get_selected_count()
        except AttributeError:
            selected_count = 0
        self._tab_bar.set_status(conn_count, acct_count, selected_count)

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
        self._contracts_view = ContractsView(
            active_threads=self.active_threads,
            parent=self,
        )                                                  # index 1 – Contracts
        self._flash_trade = FlashTradeView(
            active_threads=self.active_threads,
            accounts_model=self._accounts_model,
            parent=self,
            open_preferences=self._open_trades_preferences,
        )                                                  # index 2 – FlashTrade
        self._strategy_editor = StrategyEditorView(
            parent=self,
        )                                                  # index 3 – StrategyEditor
        self._exec_placeholder = Canvas(
            self)              # index 4 – Executions

        # When contracts are added/removed, refresh the order entry dropdown
        self._contracts_view.contracts_changed.connect(
            self._flash_trade._order_entry._refresh_combo_items
        )

        self._pages.addWidget(self._connections_view)
        self._pages.addWidget(self._contracts_view)
        self._pages.addWidget(self._flash_trade)
        self._pages.addWidget(self._strategy_editor)
        self._pages.addWidget(self._exec_placeholder)
        self._pages.setCurrentIndex(0)

        root_layout.addWidget(self._pages, stretch=1)

        # ── Status bar ────────────────────────────────────────────────────────
        self._status_bar = StatusBar(self)
        self._status_bar.setAttribute(
            Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        root_layout.addWidget(self._status_bar)

    # ── Navigation ────────────────────────────────────────────────────────────

    def _on_nav_changed(self, name: str) -> None:
        idx = _PAGE_INDEX.get(name)
        if idx is not None:
            log.debug("Nav → %s (page %d)", name, idx)
            self._pages.setCurrentIndex(idx)

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

        ``section`` / ``subview`` are used by the Trades gear button to
        land on Columns → Trades / Summary without a second click.
        """
        dlg = PreferencesDialog(self, columns_provider=self._flash_trade.trades_panel)
        if section is not None:
            dlg.navigate_to(section, subview)
        if modeless:
            dlg.show()
        else:
            dlg.exec()
        return dlg

    def _open_trades_preferences(self, view_kind: str) -> None:
        """Callback wired into the Trades gear button.  ``view_kind`` is
        ``"flat"`` or ``"summary"``."""
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
