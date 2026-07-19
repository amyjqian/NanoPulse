"""AuthWindow — the pre-main authentication flow.

Hosts Setup / Login / Locked / Change-password in a ``QStackedWidget`` and
routes between them.  Emits ``authenticated(User)`` exactly once, when a user
has fully signed in (including clearing any forced password change).  The
controller in ``main.py`` listens for that and swaps in the ``MainWindow``.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from src.auth import AuthService
from . import style as S
from .setup_page import SetupPage
from .login_page import LoginPage
from .locked_page import LockedPage
from .change_pw_page import ChangePwPage

log = logging.getLogger(__name__)

_SETTINGS_ORG = "nanoPulse"
_SETTINGS_APP = "nanoPulse"
_REMEMBER_KEY = "auth/remember_username"


class AuthWindow(QWidget):
    """Emits ``authenticated(User)`` when sign-in fully completes."""

    authenticated = Signal(object)

    def __init__(self, service: AuthService, parent=None):
        super().__init__(parent)
        self._service = service
        self.setWindowTitle("NanoPulse")
        self.resize(960, 800)
        self.setMinimumSize(760, 520)

        self._build_ui()
        self._route_initial()

    # ── build ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # Pages sit directly on the window background (which paintEvent fills
        # with BG_APP) — each page draws its own centred card, so there is no
        # outer frame.  The real OS window supplies the title bar.
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("QStackedWidget { background: transparent; }")

        self._setup = SetupPage(self._service)
        self._login = LoginPage(self._service)
        self._locked = LockedPage()
        self._change_pw = ChangePwPage(self._service)

        for page in (self._setup, self._login, self._locked, self._change_pw):
            self._stack.addWidget(page)

        root.addWidget(self._stack, stretch=1)

        # Wiring.
        self._setup.admin_created.connect(self._on_admin_created)
        self._login.authenticated.connect(self._on_login_ok)
        self._login.locked.connect(self._on_locked)
        self._login.setup_requested.connect(self._show_setup)
        self._login.reset_requested.connect(self._on_reset)
        self._locked.expired.connect(self._show_login)
        self._change_pw.changed.connect(self._on_pw_changed)
        self._change_pw.cancelled.connect(self._show_login)

    # ── background paint (matches reference app bg) ──────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(S.BG_APP))
        p.end()

    # ── routing ──────────────────────────────────────────────────────────────

    def _route_initial(self) -> None:
        # First run (no accounts yet) → jump straight to admin setup.
        # Once an admin exists, land on the login screen.
        if not self._service.has_any_account():
            self._show_setup()
        else:
            self._show_login()

    def _show_setup(self) -> None:
        self._stack.setCurrentWidget(self._setup)

    def _on_admin_created(self, _user) -> None:
        # Per the reference: first-run setup lands on Login, not straight in.
        self._show_login(prefill=_user.username)

    def _show_login(self, prefill: str | None = None) -> None:
        self._locked.stop()
        remembered = prefill if prefill is not None else self._remembered_username()
        self._login.prefill(remembered)
        # Setup link when the store is empty; Reset link once accounts exist.
        has_accounts = self._service.has_any_account()
        self._login.set_setup_link_visible(not has_accounts)
        self._login.set_reset_link_visible(has_accounts)
        self._stack.setCurrentWidget(self._login)

    def _on_reset(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        if S.message_box(
            self, "Reset to default",
            "This deletes ALL user accounts and returns the app to first-run "
            "setup. This cannot be undone.\n\nContinue?",
            icon=QMessageBox.Icon.Warning,
            buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            default=QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._service.reset_all()
        self._show_login()  # now empty → shows the setup link

    def _on_locked(self, seconds: int) -> None:
        self._locked.start(seconds)
        self._stack.setCurrentWidget(self._locked)

    def _on_login_ok(self, user) -> None:
        self._persist_remember(user.username)
        if user.must_change_pw:
            self._change_pw.prepare(user.username, forced=True)
            self._stack.setCurrentWidget(self._change_pw)
            return
        self.authenticated.emit(user)

    def _on_pw_changed(self, username: str) -> None:
        # Forced change complete — proceed with the refreshed user record.
        user = self._service.get_user(username)
        if user is not None:
            self.authenticated.emit(user)

    # ── remember-me (username only, never the password) ──────────────────────

    def _settings(self):
        from PySide6.QtCore import QSettings
        return QSettings(_SETTINGS_ORG, _SETTINGS_APP)

    def _remembered_username(self) -> str:
        return str(self._settings().value(_REMEMBER_KEY, "") or "")

    def _persist_remember(self, username: str) -> None:
        s = self._settings()
        if self._login.remember_checked():
            s.setValue(_REMEMBER_KEY, username)
        else:
            s.remove(_REMEMBER_KEY)
