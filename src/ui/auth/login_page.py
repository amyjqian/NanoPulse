"""Login screen — brand panel + credential form, matching the reference."""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QPointF, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget,
)

from src.auth import AuthService, AuthError, InvalidCredentials, AccountLocked
from . import style as S

log = logging.getLogger(__name__)


class _EcgStrip(QWidget):
    """A static teal ECG waveform drawn along the bottom of the brand panel."""

    # One heartbeat, in px, as (dx, dy) where negative dy is up. Tiled across
    # the width and scaled to the strip height.
    _BEAT = [(0, 0), (40, 0), (50, -20), (62, 20), (72, -30),
             (80, 10), (92, 0), (150, 0)]
    _BEAT_W = 150

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(46)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        base, amp = h * 0.62, 0.7
        poly = QPolygonF()
        x0 = 0
        while x0 < w + self._BEAT_W:
            for dx, dy in self._BEAT:
                poly.append(QPointF(min(x0 + dx, w), base + dy * amp))
            x0 += self._BEAT_W
        pen = QPen(QColor(S.ACCENT_TEAL))
        pen.setWidthF(1.6)
        p.setPen(pen)
        p.setOpacity(0.75)
        p.drawPolyline(poly)
        p.end()


class _BrandPanel(QWidget):
    """Left panel: subtle grid background + right divider (children on top)."""

    def paintEvent(self, event):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(S.BG_PANEL))
        p.setPen(QPen(QColor(255, 255, 255, 6), 1))
        step = 34
        x = 0
        while x < w:
            p.drawLine(x, 0, x, h)
            x += step
        y = 0
        while y < h:
            p.drawLine(0, y, w, y)
            y += step
        p.setPen(QPen(QColor("#1d1d22"), 1))
        p.drawLine(w - 1, 0, w - 1, h)
        p.end()


class LoginPage(QWidget):
    """Emits ``authenticated(User)`` on success, or ``locked(seconds)`` when
    the attempt trips the lockout threshold."""

    authenticated = Signal(object)
    locked = Signal(int)
    # User clicked "Set up admin account" (shown only on first run).
    setup_requested = Signal()
    # User clicked "Reset to default" (shown only when accounts exist).
    reset_requested = Signal()

    def __init__(self, service: AuthService, parent=None):
        super().__init__(parent)
        self._service = service
        self._build_ui()

    # ── build ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Brand panel ──────────────────────────────────────────────────────
        brand = _BrandPanel()
        brand.setFixedWidth(430)
        bl = QVBoxLayout(brand)
        bl.setContentsMargins(38, 44, 38, 44)
        bl.setSpacing(0)

        logo_row = QHBoxLayout()
        logo_row.setSpacing(11)
        icon = QLabel()
        _pm = QPixmap(S.image_path("app.png"))
        if not _pm.isNull():
            icon.setPixmap(_pm.scaled(
                40, 40, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            icon.setText("⚡")
            icon.setStyleSheet(
                f"color: {S.ACCENT_TEAL}; font-size: 22px;"
                " background: rgba(46,196,166,.12);"
                " border: 1px solid rgba(46,196,166,.35);"
                " border-radius: 11px; padding: 6px 9px;")
        name = QLabel("NanoPulse")
        name.setStyleSheet(
            f"color: {S.TEXT}; font-size: 22px; font-weight: 700; background: transparent;")
        logo_row.addWidget(icon)
        logo_row.addWidget(name)
        logo_row.addStretch()
        bl.addLayout(logo_row)

        headline = QLabel("Precision trading, in\nreal time.")
        headline.setStyleSheet(
            f"color: {S.TEXT}; font-size: 26px; font-weight: 600; background: transparent;")
        bl.addSpacing(26)
        bl.addWidget(headline)

        tag = QLabel(
            "Institutional-grade execution with\nsub-millisecond market data.")
        tag.setStyleSheet(
            f"color: {S.TEXT_MUTED}; font-size: 14px; background: transparent;")
        bl.addSpacing(14)
        bl.addWidget(tag)
        bl.addStretch()

        bl.addWidget(_EcgStrip())
        bl.addSpacing(14)
        version = QLabel("v3.2.1 · SECURE SESSION · TLS 1.3")
        version.setStyleSheet(
            f"color: #45454d; font-size: 11px; font-family: 'SF Mono', monospace;"
            " background: transparent;")
        bl.addWidget(version)
        outer.addWidget(brand)

        # ── Form panel ───────────────────────────────────────────────────────
        # The form is a fixed-width block centred vertically + horizontally in
        # the right panel (top/bottom stretches keep it compact instead of
        # stretching the fields apart).
        form_host = QWidget()
        fh = QVBoxLayout(form_host)
        fh.setContentsMargins(40, 40, 40, 40)
        fh.addStretch()

        form = QWidget()
        form.setFixedWidth(340)
        fl = QVBoxLayout(form)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)

        fl.addWidget(S.make_title("Sign in"))
        subtitle = QLabel("Enter your credentials to access the terminal.")
        subtitle.setStyleSheet(
            f"color: {S.TEXT_MUTED}; font-size: 13px; background: transparent;")
        fl.addSpacing(8)
        fl.addWidget(subtitle)
        fl.addSpacing(22)

        self._banner = S.make_banner("error")
        fl.addWidget(self._banner)
        fl.addSpacing(14)

        fl.addWidget(S.make_label("Username"))
        fl.addSpacing(7)
        self._user = S.make_input("admin")
        fl.addWidget(self._user)
        fl.addSpacing(16)

        fl.addWidget(S.make_label("Password"))
        fl.addSpacing(7)
        self._pw = S.make_input("", password=True)
        # reveal toggle
        self._reveal = self._pw.addAction(
            _eye_icon(), QLineEdit.ActionPosition.TrailingPosition)
        self._reveal.setCheckable(True)
        self._reveal.triggered.connect(self._toggle_reveal)
        fl.addWidget(self._pw)
        fl.addSpacing(16)

        row = QHBoxLayout()
        self._remember = QCheckBox("Remember me")
        self._remember.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remember.setStyleSheet(
            f"QCheckBox {{ color: {S.TEXT_LABEL}; font-size: 13px; background: transparent; }}")
        row.addWidget(self._remember)
        row.addStretch()
        hint = QLabel("Forgot password? Contact admin")
        hint.setStyleSheet(f"color: {S.TEXT_FAINT}; font-size: 13px; background: transparent;")
        row.addWidget(hint)
        fl.addLayout(row)
        fl.addSpacing(22)

        self._btn = S.make_primary_button("Sign in")
        self._btn.clicked.connect(self._attempt)
        fl.addWidget(self._btn)

        # First-run helper — only visible when no accounts exist yet.
        self._setup_hint = QLabel(
            'First time here? '
            f'<a href="setup" style="color:{S.ACCENT_TEAL}; text-decoration:none;">'
            'Set up admin account</a>')
        self._setup_hint.setTextFormat(Qt.TextFormat.RichText)
        self._setup_hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._setup_hint.setOpenExternalLinks(False)
        self._setup_hint.setStyleSheet(
            f"color: {S.TEXT_FAINT}; font-size: 13px; background: transparent;")
        self._setup_hint.linkActivated.connect(lambda _: self.setup_requested.emit())
        self._setup_hint.setVisible(False)
        fl.addSpacing(20)
        fl.addWidget(self._setup_hint)

        # "Locked out? Reset to default" — only shown when accounts exist.
        self._reset_hint = QLabel(
            'Locked out? '
            '<a href="reset" style="color:#e0843a; text-decoration:none;">'
            'Reset to default</a>')
        self._reset_hint.setTextFormat(Qt.TextFormat.RichText)
        self._reset_hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._reset_hint.setOpenExternalLinks(False)
        self._reset_hint.setStyleSheet(
            f"color: {S.TEXT_FAINT}; font-size: 13px; background: transparent;")
        self._reset_hint.linkActivated.connect(lambda _: self.reset_requested.emit())
        self._reset_hint.setVisible(False)
        fl.addSpacing(8)
        fl.addWidget(self._reset_hint)

        center_row = QHBoxLayout()
        center_row.setContentsMargins(0, 0, 0, 0)
        center_row.addStretch()
        center_row.addWidget(form)
        center_row.addStretch()
        fh.addLayout(center_row)
        fh.addStretch()
        outer.addWidget(form_host, stretch=1)

        # Enter submits from either field.
        self._user.returnPressed.connect(self._attempt)
        self._pw.returnPressed.connect(self._attempt)

    # ── behaviour ────────────────────────────────────────────────────────────

    def _toggle_reveal(self, checked: bool) -> None:
        self._pw.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)

    def set_setup_link_visible(self, visible: bool) -> None:
        """Show the 'Set up admin account' link only on first run (no accounts)."""
        self._setup_hint.setVisible(visible)

    def set_reset_link_visible(self, visible: bool) -> None:
        """Show the 'Reset to default' link only when accounts already exist."""
        self._reset_hint.setVisible(visible)

    def prefill(self, username: str) -> None:
        if username:
            self._user.setText(username)
            self._remember.setChecked(True)
            self._pw.setFocus()
        else:
            self._user.setFocus()

    def remember_checked(self) -> bool:
        return self._remember.isChecked()

    def username(self) -> str:
        return self._user.text().strip()

    def reset_password_field(self) -> None:
        self._pw.clear()

    def _attempt(self) -> None:
        u, p = self._user.text().strip(), self._pw.text()
        try:
            user = self._service.authenticate(u, p)
        except AccountLocked as e:
            self._pw.clear()
            self.locked.emit(e.seconds_remaining)
            return
        except InvalidCredentials as e:
            self._pw.clear()
            S.set_banner(self._banner, str(e))
            return
        except (ValueError, AuthError) as e:
            S.set_banner(self._banner, str(e))
            return
        S.set_banner(self._banner, "")
        self._pw.clear()
        self.authenticated.emit(user)


def _eye_icon():
    """Tiny inline 'reveal password' icon so we don't ship an asset file."""
    from PySide6.QtGui import QIcon, QPixmap, QPainter, QPen, QColor
    from PySide6.QtCore import QRectF
    pm = QPixmap(20, 20)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(S.TEXT_MUTED), 1.6))
    p.drawArc(QRectF(2, 5, 16, 10), 0, 180 * 16)
    p.drawArc(QRectF(2, 5, 16, 10), 0, -180 * 16)
    p.drawEllipse(QRectF(8, 7, 4, 4))
    p.end()
    return QIcon(pm)
