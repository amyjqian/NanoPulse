"""Session-lock overlay — opaque cover over MainWindow.

Locking hides the trading UI (positions, P&L, account ids) without
touching the session underneath: IB worker threads, subscriptions and
working orders keep running.  Unlock re-authenticates the *current* user
through :class:`AuthService`, honouring the same failed-attempt /
lockout rules as the login screen so lock never becomes a weaker side
door.  A "Sign out instead" link covers the walk-away-forever case.

The overlay is a plain child widget of MainWindow (not a dialog) so it
inherits the window's geometry, stacks above every page, and can't be
dismissed with Escape.  The host is responsible for sizing it on window
resizes and for closing any floating windows (Preferences, symbol-search
popups) before showing it — data outside the main window's rect would
otherwise leak around the cover.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from src.auth import AccountLocked, AuthError, InvalidCredentials
from . import style as S
from .login_page import _eye_icon

log = logging.getLogger(__name__)

_MONO = "'JetBrains Mono', Menlo, Consolas, monospace"


class LockOverlay(QWidget):
    """Opaque full-window cover with a password prompt for the signed-in
    user.  Emits ``unlocked`` on a successful re-auth; the host hides the
    overlay.  Emits ``sign_out_requested`` from the escape-hatch link.

    *feed_live_provider* (optional) is a zero-arg callable the host
    supplies to report whether any broker connection is currently live —
    shown as the "Feed live" pill so the user knows the session kept
    running while locked.
    """

    unlocked = Signal()
    sign_out_requested = Signal()

    def __init__(self, user, service, parent=None, *,
                 feed_live_provider=None):
        super().__init__(parent)
        self._user = user
        self._service = service
        self._feed_live_provider = feed_live_provider
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._build_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(4)

        card = QFrame()
        card.setFixedWidth(480)
        card.setStyleSheet(
            f"QFrame {{ background: {S.BG_CARD};"
            f" border: 1px solid {S.BORDER}; border-radius: 16px; }}")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(36, 32, 36, 30)
        cl.setSpacing(0)

        # ── Header: app icon + wordmark + user line | feed pill ──────────
        head = QHBoxLayout()
        head.setSpacing(12)

        icon = QLabel()
        icon.setFixedSize(40, 40)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("background: transparent; border: none;")
        _pm = QPixmap(S.image_path("app.png"))
        if not _pm.isNull():
            icon.setPixmap(_pm.scaled(
                40, 40, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            icon.setText("⚡")
            icon.setStyleSheet(
                f"color: {S.ACCENT_TEAL}; font-size: 20px;"
                " background: rgba(46,196,166,.12);"
                " border: 1px solid rgba(46,196,166,.35);"
                " border-radius: 10px;")
        head.addWidget(icon)

        name_col = QVBoxLayout()
        name_col.setSpacing(2)
        wordmark = QLabel(
            f"<span style='color:{S.TEXT};'>Nano</span>"
            f"<span style='color:{S.ACCENT_TEAL};'>Pulse</span>")
        wordmark.setTextFormat(Qt.TextFormat.RichText)
        wordmark.setStyleSheet(
            "font-size: 19px; font-weight: 700;"
            " background: transparent; border: none;")
        name_col.addWidget(wordmark)
        # Show the USERNAME — it's the identity the password field
        # re-authenticates; display names are free-form text.
        who = (getattr(self._user, "username", "")
               or getattr(self._user, "display_name", "") or "?")
        role = ("admin" if getattr(self._user, "is_admin", False)
                else "trader")
        user_line = QLabel(f"{who}&nbsp;&nbsp;·&nbsp;&nbsp;{role.upper()}")
        user_line.setTextFormat(Qt.TextFormat.RichText)
        user_line.setStyleSheet(
            f"color: {S.TEXT_MUTED}; font-size: 12px;"
            f" font-family: {_MONO};"
            " background: transparent; border: none;")
        name_col.addWidget(user_line)
        head.addLayout(name_col)
        head.addStretch()

        self._feed_pill = QLabel()
        self._feed_pill.setStyleSheet("background: transparent; border: none;")
        head.addWidget(self._feed_pill, 0, Qt.AlignmentFlag.AlignTop)
        cl.addLayout(head)
        cl.addSpacing(28)

        # ── Body ─────────────────────────────────────────────────────────
        title = QLabel("Locked")
        title.setStyleSheet(
            f"color: {S.TEXT}; font-size: 24px; font-weight: 700;"
            " background: transparent; border: none;")
        cl.addWidget(title)
        cl.addSpacing(6)

        sub = QLabel("Enter your password to continue trading.")
        sub.setStyleSheet(
            f"color: {S.TEXT_MUTED}; font-size: 14px;"
            " background: transparent; border: none;")
        cl.addWidget(sub)
        cl.addSpacing(20)

        self._pw = S.make_input("", password=True)
        self._reveal = self._pw.addAction(
            _eye_icon(), QLineEdit.ActionPosition.TrailingPosition)
        self._reveal.setCheckable(True)
        self._reveal.triggered.connect(self._toggle_reveal)
        self._pw.returnPressed.connect(self._attempt)
        # Typing again clears the previous error state.
        self._pw.textEdited.connect(lambda _t: self._set_error(""))
        cl.addWidget(self._pw)
        cl.addSpacing(8)

        # Plain red error line under the field (mock style — no boxed
        # banner).  Kept under the name ``_banner`` so the show/hide
        # semantics match the other auth screens.
        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        self._banner.setStyleSheet(
            f"color: {S.DANGER_TEXT}; font-size: 13px;"
            " background: transparent; border: none;")
        cl.addWidget(self._banner)
        cl.addSpacing(14)

        self._btn = S.make_primary_button("Unlock")
        self._btn.clicked.connect(self._attempt)
        cl.addWidget(self._btn)
        cl.addSpacing(14)

        out = QLabel(
            f'<a href="signout" style="color:{S.TEXT_MUTED};'
            ' text-decoration:none;">Sign out instead</a>')
        out.setTextFormat(Qt.TextFormat.RichText)
        out.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        out.setOpenExternalLinks(False)
        out.setStyleSheet(
            f"color: {S.TEXT_MUTED}; font-size: 14px;"
            " background: transparent; border: none;")
        out.linkActivated.connect(
            lambda _href: self.sign_out_requested.emit())
        cl.addWidget(out)

        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(card)
        row.addStretch()
        outer.addLayout(row)
        outer.addStretch(5)

    # ── Behaviour ────────────────────────────────────────────────────────────

    def activate(self) -> None:
        """Show the overlay fresh: cleared password, no stale error, and
        the feed pill reflecting the current connection state."""
        self._set_error("")
        self._pw.clear()
        self._reveal.setChecked(False)
        self._toggle_reveal(False)
        self._refresh_feed_pill()
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self._pw.setFocus()

    def _toggle_reveal(self, checked: bool) -> None:
        self._pw.setEchoMode(
            QLineEdit.EchoMode.Normal if checked
            else QLineEdit.EchoMode.Password)

    def _refresh_feed_pill(self) -> None:
        live = False
        if callable(self._feed_live_provider):
            try:
                live = bool(self._feed_live_provider())
            except Exception:
                live = False
        if live:
            dot, text, color = S.OK_GREEN, "Feed live", S.OK_GREEN
            bg, border = "rgba(52,179,74,.08)", "rgba(52,179,74,.35)"
        else:
            dot, text, color = S.TEXT_FAINT, "Feed offline", S.TEXT_MUTED
            bg, border = "transparent", S.BORDER
        self._feed_pill.setTextFormat(Qt.TextFormat.RichText)
        self._feed_pill.setText(
            f"<span style='color:{dot};'>●</span>&nbsp; {text}")
        self._feed_pill.setStyleSheet(
            f"color: {color}; font-size: 13px; font-weight: 600;"
            f" background: {bg}; border: 1px solid {border};"
            " border-radius: 14px; padding: 5px 14px;")

    def _set_error(self, msg: str) -> None:
        """Red error line + red field border; empty *msg* clears both."""
        if msg:
            self._banner.setText(msg)
            self._banner.setVisible(True)
            self._pw.setStyleSheet(
                S.input_qss()
                + f"QLineEdit {{ border: 1px solid {S.DANGER}; }}")
        else:
            self._banner.setVisible(False)
            self._pw.setStyleSheet(S.input_qss())

    def _attempt(self) -> None:
        username = getattr(self._user, "username", "") or ""
        try:
            self._service.authenticate(username, self._pw.text())
        except AccountLocked as e:
            self._pw.clear()
            mins = max(1, (e.seconds_remaining + 59) // 60)
            self._set_error(
                f"Too many attempts — try again in {mins} min.")
            return
        except InvalidCredentials as e:
            self._pw.clear()
            self._set_error(str(e))
            return
        except (ValueError, AuthError) as e:
            self._pw.clear()
            self._set_error(str(e))
            return
        self._pw.clear()
        self._set_error("")
        log.info("Session unlocked by %r", username)
        self.unlocked.emit()

    # Opaque cover — the whole point is that nothing behind it shows.
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(S.BG_APP))
        p.end()

    # Swallow clicks so nothing reaches widgets underneath.
    def mousePressEvent(self, event) -> None:  # noqa: N802
        event.accept()
