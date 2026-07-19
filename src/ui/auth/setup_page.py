"""First-run setup — create the single administrator account."""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from src.auth import AuthService
from . import style as S

log = logging.getLogger(__name__)


class SetupPage(QWidget):
    """Emits ``admin_created(User)`` once the admin account is written."""

    admin_created = Signal(object)

    def __init__(self, service: AuthService, parent=None):
        super().__init__(parent)
        self._service = service
        self._build_ui()

    def _build_ui(self) -> None:
        # A scroll area keeps the card at its natural size and centres it when
        # there's room, scrolling only when the window is too short — so the
        # rows can never compress/overlap on a small window.
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(36, 36, 36, 36)
        outer.addStretch()

        card = QWidget()
        card.setObjectName("setupCard")
        card.setFixedWidth(420)
        # Scope the border to the card so it doesn't cascade onto child labels.
        card.setStyleSheet(
            f"#setupCard {{ background: {S.BG_CARD}; border: 1px solid {S.BORDER};"
            " border-radius: 14px; }}")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(32, 32, 32, 32)
        cl.setSpacing(0)

        shield = QLabel()
        shield.setAlignment(Qt.AlignmentFlag.AlignLeft)
        _pm = QPixmap(S.image_path("admin.png"))
        if not _pm.isNull():
            # The PNG already includes its own rounded teal badge — just render
            # it (scaled to fit, high-quality) with no extra background box.
            shield.setPixmap(_pm.scaled(
                44, 44, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            # Fallback to the emoji badge if the asset is missing.
            shield.setText("🛡")
            shield.setStyleSheet(
                f"color: {S.ACCENT_TEAL}; font-size: 22px;"
                " background: rgba(46,196,166,.12);"
                " border: 1px solid rgba(46,196,166,.35);"
                " border-radius: 12px; padding: 8px 10px;")
        row = QHBoxLayout()
        row.addWidget(shield)
        row.addStretch()
        cl.addLayout(row)
        cl.addSpacing(18)

        cl.addWidget(S.make_title("Welcome to NanoPulse", size=21))
        blurb = QLabel(
            "No accounts exist yet. Create the administrator "
            "account to get started.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(
            f"color: {S.TEXT_MUTED}; font-size: 13px; background: transparent;")
        cl.addSpacing(8)
        cl.addWidget(blurb)
        cl.addSpacing(20)

        self._banner = S.make_banner("error")
        cl.addWidget(self._banner)
        cl.addSpacing(10)

        cl.addWidget(S.make_label("Admin username"))
        cl.addSpacing(7)
        self._user = S.make_input("admin")
        self._user.setText("admin")
        cl.addWidget(self._user)
        cl.addSpacing(16)

        cl.addWidget(S.make_label("Password"))
        cl.addSpacing(7)
        self._pw = S.make_input("Enter a password", password=True)
        self._pw.textChanged.connect(self._update_strength)
        cl.addWidget(self._pw)
        cl.addSpacing(8)

        # strength meter
        meter = QHBoxLayout()
        meter.setSpacing(5)
        self._bars = []
        for _ in range(3):
            bar = QLabel()
            bar.setFixedHeight(4)
            bar.setStyleSheet(f"background: {S.BAR_OFF}; border-radius: 2px;")
            self._bars.append(bar)
            meter.addWidget(bar, stretch=1)
        self._strength_lbl = QLabel("")
        self._strength_lbl.setFixedWidth(54)
        self._strength_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._strength_lbl.setStyleSheet(
            f"color: {S.TEXT_MUTED}; font-size: 11px; background: transparent;")
        meter.addWidget(self._strength_lbl)
        cl.addLayout(meter)
        cl.addSpacing(16)

        cl.addWidget(S.make_label("Confirm password"))
        cl.addSpacing(7)
        self._pw2 = S.make_input("Re-enter password", password=True)
        cl.addWidget(self._pw2)

        # Live match hint — updates as either password field changes.
        cl.addSpacing(6)
        self._match_lbl = QLabel("")
        self._match_lbl.setVisible(False)
        cl.addWidget(self._match_lbl)
        cl.addSpacing(12)
        self._pw.textChanged.connect(self._update_match)
        self._pw2.textChanged.connect(self._update_match)

        # Amber notice.  The label carries no stylesheet padding (padding on a
        # word-wrapped QLabel isn't counted in its height and clips the text) —
        # padding comes from the container's layout margins instead.
        warn_box = QWidget()
        warn_box.setObjectName("adminWarn")
        warn_box.setMinimumHeight(62)  # room for the 2-line notice at 420px card
        warn_box.setStyleSheet(
            "#adminWarn { background: rgba(224,166,58,.08);"
            " border: 1px solid rgba(224,166,58,.25); border-radius: 8px; }")
        wl = QVBoxLayout(warn_box)
        wl.setContentsMargins(12, 11, 12, 11)
        warn = QLabel(
            "The admin account manages users and cannot place trades. "
            "Create trader accounts afterwards.")
        warn.setWordWrap(True)
        warn.setStyleSheet(
            "color: #cba766; font-size: 12px; background: transparent; border: none;")
        wl.addWidget(warn)
        cl.addWidget(warn_box)
        cl.addSpacing(30)

        # "&&" so Qt renders a literal ampersand instead of a mnemonic.
        self._btn = S.make_primary_button("Create admin && continue")
        self._btn.clicked.connect(self._create)
        cl.addWidget(self._btn)

        outer.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()
        scroll.setWidget(holder)
        root.addWidget(scroll)

        self._pw2.returnPressed.connect(self._create)

    # ── strength meter ───────────────────────────────────────────────────────

    def _update_strength(self, pw: str) -> None:
        n = 0
        if len(pw) >= 8:
            n += 1
        if any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            n += 1
        if any(c.isdigit() for c in pw) or any(not c.isalnum() for c in pw):
            n += 1
        color = S.DANGER if n <= 1 else (S.WARN if n == 2 else S.OK_GREEN)
        label = "" if not pw else ("Weak" if n <= 1 else "Fair" if n == 2 else "Strong")
        for i, bar in enumerate(self._bars):
            on = pw and i < n
            bar.setStyleSheet(
                f"background: {color if on else S.BAR_OFF}; border-radius: 2px;")
        self._strength_lbl.setText(label)

    # ── live match hint ───────────────────────────────────────────────────────

    def _update_match(self, _=None) -> None:
        """Show a green ✓ when both passwords match, a red note when they
        differ, and nothing until the confirm field has been touched."""
        pw, pw2 = self._pw.text(), self._pw2.text()
        if not pw2:
            self._match_lbl.setVisible(False)
            return
        if pw == pw2:
            self._match_lbl.setText("✓  Passwords match")
            self._match_lbl.setStyleSheet(
                f"color: {S.OK_GREEN}; font-size: 12px; background: transparent;")
        else:
            self._match_lbl.setText("✕  Passwords do not match")
            self._match_lbl.setStyleSheet(
                f"color: {S.DANGER_TEXT}; font-size: 12px; background: transparent;")
        self._match_lbl.setVisible(True)

    # ── create ───────────────────────────────────────────────────────────────

    def _create(self) -> None:
        u, p, p2 = self._user.text().strip(), self._pw.text(), self._pw2.text()
        if p != p2:
            S.set_banner(self._banner, "Passwords do not match.")
            return
        try:
            user = self._service.create_admin(u, p)
        except ValueError as e:
            S.set_banner(self._banner, str(e))
            return
        S.set_banner(self._banner, "")
        self._pw.clear()
        self._pw2.clear()
        self.admin_created.emit(user)
