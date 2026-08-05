"""Session lock — LockOverlay auth flow + tab-bar padlock.

MainWindow itself is worker-heavy, so the pieces are covered
individually: the overlay re-authenticates the current user through the
same AuthService rules as login (wrong password → banner, lockout →
cooldown message, success → ``unlocked``), and the TopTabBar padlock is
hidden until the host confirms an authenticated session.
"""
from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtWidgets import QLabel

from src.auth import AccountLocked, InvalidCredentials
from src.ui.auth.lock_overlay import LockOverlay
from src.ui.widgets.top_tab_bar import TopTabBar

_USER = SimpleNamespace(username="alex", display_name="Alex")


class _FakeService:
    def __init__(self, password="pw", locked_seconds=0):
        self.password = password
        self.locked_seconds = locked_seconds
        self.calls: list[tuple[str, str]] = []

    def authenticate(self, username, password):
        self.calls.append((username, password))
        if self.locked_seconds:
            raise AccountLocked(self.locked_seconds)
        if password != self.password:
            raise InvalidCredentials(2)
        return _USER


def _overlay(qtbot, service) -> LockOverlay:
    from PySide6.QtWidgets import QWidget
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(800, 600)
    ov = LockOverlay(_USER, service, host)
    # qtbot holds widgets weakly — pin the parent on the overlay so the
    # host (and with it the overlay's C++ object) survives this helper.
    ov._test_host = host
    ov.activate()
    return ov


def test_correct_password_unlocks(qtbot):
    svc = _FakeService(password="s3cret")
    ov = _overlay(qtbot, svc)
    fired = []
    ov.unlocked.connect(lambda: fired.append(True))
    ov._pw.setText("s3cret")
    ov._attempt()
    assert fired == [True]
    assert svc.calls == [("alex", "s3cret")]   # re-auths the CURRENT user
    assert ov._pw.text() == ""                 # never keeps the password


def test_wrong_password_shows_banner_and_stays_locked(qtbot):
    ov = _overlay(qtbot, _FakeService(password="right"))
    fired = []
    ov.unlocked.connect(lambda: fired.append(True))
    ov._pw.setText("wrong")
    ov._attempt()
    assert fired == []
    assert ov._pw.text() == ""
    assert ov._banner.text() != ""             # error surfaced


def test_lockout_shows_cooldown_message(qtbot):
    ov = _overlay(qtbot, _FakeService(locked_seconds=170))
    ov._pw.setText("anything")
    ov._attempt()
    assert "3 min" in ov._banner.text()        # 170 s rounds up to 3


def test_activate_resets_stale_state(qtbot):
    ov = _overlay(qtbot, _FakeService(password="right"))
    ov._pw.setText("wrong")
    ov._attempt()
    assert not ov._banner.isHidden()           # error banner showing
    ov.activate()                              # re-lock: fresh prompt
    assert ov._banner.isHidden()               # set_banner("") hides it
    assert ov._pw.text() == ""


def test_sign_out_link_emits(qtbot):
    ov = _overlay(qtbot, _FakeService())
    fired = []
    ov.sign_out_requested.connect(lambda: fired.append(True))
    link = next(lbl for lbl in ov.findChildren(QLabel)
                if "Sign out instead" in lbl.text())
    link.linkActivated.emit("signout")
    assert fired == [True]


def test_tab_bar_padlock_hidden_until_authenticated(qtbot):
    bar = TopTabBar()
    qtbot.addWidget(bar)
    assert bar._lock_btn.isHidden()            # legacy / test sessions
    bar.set_lock_visible(True)
    assert not bar._lock_btn.isHidden()
    fired = []
    bar.lockRequested.connect(lambda: fired.append(True))
    bar._lock_btn.click()
    assert fired == [True]


def test_feed_pill_reflects_provider(qtbot):
    from PySide6.QtWidgets import QWidget
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(800, 600)
    live = {"v": True}
    ov = LockOverlay(_USER, _FakeService(), host,
                     feed_live_provider=lambda: live["v"])
    ov._test_host = host
    ov.activate()
    assert "Feed live" in ov._feed_pill.text()
    live["v"] = False
    ov.activate()                              # re-lock re-reads the provider
    assert "Feed offline" in ov._feed_pill.text()


def test_error_clears_when_user_retypes(qtbot):
    ov = _overlay(qtbot, _FakeService(password="right"))
    ov._pw.setText("wrong")
    ov._attempt()
    assert not ov._banner.isHidden()
    ov._pw.textEdited.emit("r")                # first keystroke of retry
    assert ov._banner.isHidden()


def test_reveal_toggle_switches_echo_mode(qtbot):
    from PySide6.QtWidgets import QLineEdit
    ov = _overlay(qtbot, _FakeService())
    assert ov._pw.echoMode() == QLineEdit.EchoMode.Password
    ov._reveal.trigger()                       # eye on → plain text
    assert ov._pw.echoMode() == QLineEdit.EchoMode.Normal
    ov.activate()                              # re-lock always re-masks
    assert ov._pw.echoMode() == QLineEdit.EchoMode.Password
