"""User Management page (admin-only) — lives inside the Preferences dialog.

Table of users with Add / Enable-Disable / Remove.  The admin row's Remove is
disabled (the admin account is permanent).  Every mutation re-reads from the
service so the view can never drift from the DB.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from src.auth import AuthService, Role
from .add_user_dialog import AddUserDialog
from . import style as S

log = logging.getLogger(__name__)

_TEXT = "#C9D1D9"
_TEXT_DIM = "#8B949E"
_BORDER = "#30363D"
_BG_HEADER = "#202a39"
_BG_ROW = "#1a2330"
_BLUE = "#2f60d8"
_TEAL = "#2ec4a6"
_GREEN = "#5fce77"

_MONO = "'SF Mono', ui-monospace, Menlo, monospace"


class UsersPage(QWidget):
    def __init__(self, service: AuthService, parent=None):
        super().__init__(parent)
        self._service = service
        self._build_ui()
        self.refresh()

    # ── build ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        root.setSpacing(0)

        header = QHBoxLayout()
        title = QLabel("User Management")
        title.setStyleSheet(
            f"color: #f0f2f5; font-size: 16px; font-weight: 700; background: transparent;")
        header.addWidget(title)
        header.addStretch()
        add_btn = QPushButton("+  Add user")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setMinimumHeight(32)
        add_btn.setStyleSheet(
            f"QPushButton {{ background: {_BLUE}; border: none; border-radius: 6px;"
            " color: #fff; font-size: 12px; font-weight: 500; padding: 0 13px; }}"
            "QPushButton:hover { background: #3d70e6; }")
        add_btn.clicked.connect(self._on_add)
        header.addWidget(add_btn)
        root.addLayout(header)

        sub = QLabel("Admins manage users; only traders can trade.")
        sub.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 12px; background: transparent;")
        root.addSpacing(6)
        root.addWidget(sub)
        root.addSpacing(14)

        # Column header row.
        head = self._row_container(_BG_HEADER)
        hl = self._row_layout(head)
        for text, stretch, align in (
            ("USERNAME", 14, Qt.AlignmentFlag.AlignLeft),
            ("NAME", 15, Qt.AlignmentFlag.AlignLeft),
            ("ROLE", 9, Qt.AlignmentFlag.AlignLeft),
            ("STATUS", 9, Qt.AlignmentFlag.AlignLeft),
            ("LAST LOGIN", 11, Qt.AlignmentFlag.AlignLeft),
            ("ACTIONS", 12, Qt.AlignmentFlag.AlignRight),
        ):
            lbl = QLabel(text)
            lbl.setAlignment(align | Qt.AlignmentFlag.AlignVCenter)
            lbl.setStyleSheet(
                f"color: #7c8698; font-size: 10px; font-weight: 600;"
                " letter-spacing: 0.5px; background: transparent;")
            hl.addWidget(lbl, stretch=stretch)
        head.setObjectName("usersHeader")
        head.setStyleSheet(
            f"#usersHeader {{ background: {_BG_HEADER}; border: 1px solid #26313f;"
            " border-top-left-radius: 8px; border-top-right-radius: 8px; }}")
        root.addWidget(head)

        # Scrollable body.
        self._body = QWidget()
        self._body_lay = QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(0, 0, 0, 0)
        self._body_lay.setSpacing(0)
        self._body_lay.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.setWidget(self._body)
        root.addWidget(scroll, stretch=1)

        self._count = QLabel("")
        self._count.setStyleSheet(
            f"color: #6c7688; font-size: 12px; background: transparent;")
        root.addSpacing(10)
        root.addWidget(self._count)

    # ── row helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _row_container(bg: str) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {bg};")
        return w

    @staticmethod
    def _row_layout(w: QWidget) -> QHBoxLayout:
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 11, 16, 11)
        lay.setSpacing(12)
        return lay

    def _badge(self, text: str, color: str, bg: str) -> QLabel:
        lbl = QLabel(f" {text} ")
        lbl.setStyleSheet(
            f"background: {bg}; color: {color}; border-radius: 20px;"
            " font-size: 11px; font-weight: 600; padding: 3px 9px;")
        return lbl

    # ── data ─────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        # Clear existing rows (keep trailing stretch).
        while self._body_lay.count() > 1:
            item = self._body_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        users = self._service.list_users()
        active = sum(1 for u in users if u.enabled)
        for u in users:
            self._body_lay.insertWidget(self._body_lay.count() - 1, self._make_row(u))
        self._count.setText(
            f"{len(users)} user{'' if len(users) == 1 else 's'} · {active} active")

    def _make_row(self, user) -> QWidget:
        row = self._row_container(_BG_ROW)
        row.setObjectName("userRow")
        row.setStyleSheet(
            f"#userRow {{ background: {_BG_ROW}; border-left: 1px solid #26313f;"
            " border-right: 1px solid #26313f; border-bottom: 1px solid #222c39; }}")
        lay = self._row_layout(row)
        is_admin = user.role is Role.ADMIN

        uname = QLabel(user.username)
        uname.setStyleSheet(
            f"color: #eef1f5; font-weight: 500; font-size: 12px;"
            f" font-family: {_MONO}; background: transparent;")
        lay.addWidget(uname, stretch=14)

        name = QLabel(user.display_name)
        name.setStyleSheet(f"color: #c4ccd8; font-size: 13px; background: transparent;")
        lay.addWidget(name, stretch=15)

        role_wrap = QHBoxLayout()
        role_wrap.addWidget(
            self._badge(
                "Admin" if is_admin else "Trader",
                _TEAL if is_admin else _GREEN,
                "rgba(46,196,166,.12)" if is_admin else "rgba(52,179,74,.14)"))
        role_wrap.addStretch()
        role_host = QWidget()
        role_host.setStyleSheet("background: transparent;")
        role_host.setLayout(role_wrap)
        lay.addWidget(role_host, stretch=9)

        status = QLabel("● Active" if user.enabled else "● Disabled")
        status.setStyleSheet(
            f"color: {'#8a93a3' if user.enabled else '#6c7688'}; font-size: 12px;"
            " background: transparent;")
        lay.addWidget(status, stretch=9)

        last = QLabel(user.last_login or "—")
        last.setStyleSheet(
            f"color: #8a93a3; font-size: 12px; font-family: {_MONO}; background: transparent;")
        lay.addWidget(last, stretch=11)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch()
        toggle = QPushButton("Disable" if user.enabled else "Enable")
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.setMinimumHeight(29)
        toggle.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid #33404f;"
            f" border-radius: 6px; color: #aeb6c2; font-size: 12px; padding: 0 11px; }}"
            f"QPushButton:hover {{ background: #232e3c; color: #eef1f5; }}")
        toggle.clicked.connect(lambda _=False, un=user.username, en=user.enabled:
                               self._on_toggle(un, en))
        actions.addWidget(toggle)

        remove = QPushButton("Remove")
        remove.setMinimumHeight(29)
        if is_admin:
            remove.setEnabled(False)
            remove.setToolTip("The admin account cannot be removed")
            remove.setStyleSheet(
                "QPushButton { background: transparent; border: 1px solid #2b3646;"
                " border-radius: 6px; color: #3a4656; font-size: 12px; padding: 0 11px; }")
        else:
            remove.setCursor(Qt.CursorShape.PointingHandCursor)
            remove.setStyleSheet(
                "QPushButton { background: transparent; border: 1px solid rgba(226,59,59,.35);"
                " border-radius: 6px; color: #f26a6a; font-size: 12px; padding: 0 11px; }"
                "QPushButton:hover { background: rgba(226,59,59,.1); }")
            remove.clicked.connect(
                lambda _=False, un=user.username: self._on_remove(un))
        actions.addWidget(remove)

        act_host = QWidget()
        act_host.setStyleSheet("background: transparent;")
        act_host.setLayout(actions)
        lay.addWidget(act_host, stretch=12)
        return row

    # ── actions ──────────────────────────────────────────────────────────────

    def _on_add(self) -> None:
        dlg = AddUserDialog(self._service, self)
        if dlg.exec() and dlg.created_user is not None:
            S.message_box(
                self, "User created",
                f"Trader '{dlg.created_user.username}' was created.\n\n"
                f"Temporary password:  {dlg.temp_password}\n\n"
                "Share this with the user — they'll be asked to set their own "
                "password on first sign-in.")
            self.refresh()

    def _on_toggle(self, username: str, currently_enabled: bool) -> None:
        self._service.set_enabled(username, not currently_enabled)
        self.refresh()

    def _on_remove(self, username: str) -> None:
        if S.message_box(
            self, "Remove user",
            f"Remove user '{username}'? This cannot be undone.",
            icon=QMessageBox.Icon.Question,
            buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            default=QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self._service.remove_user(username)
        except ValueError as e:
            S.message_box(self, "Cannot remove", str(e),
                          icon=QMessageBox.Icon.Warning)
            return
        self.refresh()
