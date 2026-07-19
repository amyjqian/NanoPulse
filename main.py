"""
NanoPulse – Trading Platform
Entry point.
"""
import logging
import sys

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

from src.utils.logger import setup_logging
from src.ui.main_window import MainWindow
from src.ui.auth import AuthWindow
from src.auth import AuthService
from src.core import config_manager

log = logging.getLogger(__name__)


def handle_exception(exc_type, exc_value, exc_traceback):
    """Log unhandled exceptions to the main logger."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    log.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


def _build_auth_service() -> AuthService:
    """Construct the AuthService, honouring optional lockout overrides stored
    under ``preferences.security`` in app_config.json."""
    security = config_manager.load_preferences().get("security", {})
    threshold = int(security.get("lock_threshold", 3) or 3)
    minutes = int(security.get("lock_minutes", 5) or 5)
    return AuthService(lock_threshold=threshold, lock_minutes=minutes)


class AppController(QObject):
    """Owns the top-level window lifecycle: shows the auth flow, swaps in the
    MainWindow on successful sign-in, and returns to login on sign-out."""

    def __init__(self, service: AuthService):
        super().__init__()
        self._service = service
        self._auth_window: AuthWindow | None = None
        self._main_window: MainWindow | None = None
        # Windows pending deferred destruction (see _retire).  Holding a strong
        # reference here keeps them alive until the next event-loop tick.
        self._retired: list = []
        # Automatic-backup engine for the signed-in user (set on login).
        self._backup = None
        self._backup_timer: QTimer | None = None

    def start(self) -> None:
        self._show_auth()

    # ── auth → main ──────────────────────────────────────────────────────────

    def _show_auth(self) -> None:
        self._auth_window = AuthWindow(self._service)
        self._auth_window.authenticated.connect(self._on_authenticated)
        self._auth_window.show()

    def _retire(self, win, *, run_close: bool = False) -> None:
        """Hide *win* now and destroy it on the NEXT event-loop tick.

        The window swap is triggered from inside one of the closing window's
        own widget signals (a button click / link).  Destroying it synchronously
        would free the C++ object while Qt is still unwinding that signal —
        a use-after-free that hard-crashes the app.  Deferring with a held
        reference lets the signal fully unwind first.  ``run_close`` fires
        ``closeEvent`` (used for MainWindow so worker threads shut down)."""
        if win is None:
            return
        win.hide()
        self._retired.append(win)

        def _kill():
            try:
                self._retired.remove(win)
            except ValueError:
                pass
            if run_close:
                win.close()          # fire closeEvent (stop worker threads)
            win.deleteLater()

        QTimer.singleShot(0, _kill)

    def _on_authenticated(self, user) -> None:
        log.info("Signed in as %r (%s)", user.username, user.role.value)
        # Point config_manager at this user's own app_config-<username>.json
        # before building the main window (which reads layout/prefs on init),
        # and refresh the trading-prefs cache so it reflects the new user.
        config_manager.set_active_config(user.username)
        from src.core.trading_prefs import trading_prefs
        trading_prefs.reload()
        self._start_auto_backup(user.username)
        self._main_window = MainWindow(current_user=user, auth_service=self._service)
        self._main_window.sign_out_requested.connect(self._on_sign_out)
        self._main_window.show()
        # Retire the auth window only after the main window is visible so the
        # app never briefly has zero windows (which would quit the event loop).
        aw, self._auth_window = self._auth_window, None
        self._retire(aw)

    def _start_auto_backup(self, username: str) -> None:
        """Run a catch-up auto-backup now, then check hourly (daily cadence)."""
        from src.backup import ConfigBackup
        self._backup = ConfigBackup(username)
        try:
            self._backup.maybe_auto_backup()
        except Exception:
            log.exception("Startup auto-backup check failed")
        if self._backup_timer is None:
            self._backup_timer = QTimer(self)
            self._backup_timer.timeout.connect(self._on_auto_backup_tick)
            self._backup_timer.start(3_600_000)  # hourly schedule check

    def _on_auto_backup_tick(self) -> None:
        if self._backup is not None:
            try:
                self._backup.maybe_auto_backup()
            except Exception:
                log.exception("Hourly auto-backup check failed")

    def _on_sign_out(self) -> None:
        log.info("Sign out requested")
        # Stop the auto-backup engine for the outgoing user.
        if self._backup_timer is not None:
            self._backup_timer.stop()
        self._backup = None
        # Drop back to the shared base config until the next user signs in.
        config_manager.set_active_config(None)
        # Bring up a fresh login window first, then tear down the main window
        # (deferred, so its worker-shutdown closeEvent runs off the signal).
        self._show_auth()
        mw, self._main_window = self._main_window, None
        self._retire(mw, run_close=True)


def main() -> None:
    # Set global exception handler
    sys.excepthook = handle_exception

    setup_logging()
    log.info("NanoPulse starting …")

    app = QApplication(sys.argv)
    app.setApplicationName("nanoPulse")
    app.setOrganizationName("nanoPulse")
    app.setStyle("Fusion")

    # Default application font
    font = QFont()
    font.setFamilies(["SF Pro Display", "Helvetica Neue", "Arial"])
    font.setPointSize(11)
    app.setFont(font)

    # Authentication gate: the controller shows the login / first-run-setup
    # flow and only constructs the MainWindow once a user has signed in.
    service = _build_auth_service()
    controller = AppController(service)
    controller.start()

    log.info("Auth window shown – entering event loop")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

