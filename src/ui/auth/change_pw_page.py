"""Change-password screen.

Used in two situations:
  * forced after login when ``must_change_pw`` is set (Cancel hidden);
  * voluntarily from Preferences (Cancel returns to the app).
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from src.auth import AuthService, AuthError
from . import style as S

log = logging.getLogger(__name__)


class ChangePwPage(QWidget):
    """Emits ``changed(username)`` on success, ``cancelled()`` on Cancel."""

    changed = Signal(str)
    cancelled = Signal()

    def __init__(self, service: AuthService, parent=None):
        super().__init__(parent)
        self._service = service
        self._username = ""
        self._forced = False
        self._build_ui()

    def _build_ui(self) -> None:
        # Scroll area → the card keeps its natural size and centres, scrolling
        # only when the window is too short (never compresses/overlaps).
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
        card.setObjectName("cpCard")
        card.setFixedWidth(400)
        card.setStyleSheet(
            f"#cpCard {{ background: {S.BG_CARD}; border: 1px solid {S.BORDER};"
            " border-radius: 14px; }}")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(30, 30, 30, 30)
        cl.setSpacing(0)

        cl.addWidget(S.make_title("Change password", size=20))
        self._subtitle = QLabel("Choose a strong password you don't use elsewhere.")
        self._subtitle.setWordWrap(True)
        self._subtitle.setStyleSheet(
            f"color: {S.TEXT_MUTED}; font-size: 13px; background: transparent;")
        cl.addSpacing(7)
        cl.addWidget(self._subtitle)
        cl.addSpacing(20)

        self._banner = S.make_banner("error")
        cl.addWidget(self._banner)
        cl.addSpacing(10)

        cl.addWidget(S.make_label("Current password"))
        cl.addSpacing(7)
        self._old = S.make_input(password=True)
        cl.addWidget(self._old)
        cl.addSpacing(16)

        cl.addWidget(S.make_label("New password"))
        cl.addSpacing(7)
        self._new = S.make_input(password=True)
        cl.addWidget(self._new)
        cl.addSpacing(16)

        cl.addWidget(S.make_label("Confirm new password"))
        cl.addSpacing(7)
        self._new2 = S.make_input(password=True)
        cl.addWidget(self._new2)
        cl.addSpacing(22)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self._cancel_btn = S.make_secondary_button("Cancel")
        self._cancel_btn.clicked.connect(self.cancelled.emit)
        self._save_btn = S.make_primary_button("Update password")
        self._save_btn.clicked.connect(self._submit)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._save_btn, stretch=1)
        cl.addLayout(btn_row)

        outer.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()
        scroll.setWidget(holder)
        root.addWidget(scroll)

        self._new2.returnPressed.connect(self._submit)

    def prepare(self, username: str, *, forced: bool) -> None:
        """Reset fields for *username*.  When *forced* the Cancel button and
        the must-change notice make it clear the user can't skip this."""
        self._username = username
        self._forced = forced
        self._old.clear()
        self._new.clear()
        self._new2.clear()
        S.set_banner(self._banner, "")
        self._cancel_btn.setVisible(not forced)
        self._subtitle.setText(
            "You must set a new password before continuing."
            if forced else "Choose a strong password you don't use elsewhere.")
        self._old.setFocus()

    def _submit(self) -> None:
        old, new, new2 = self._old.text(), self._new.text(), self._new2.text()
        if new != new2:
            S.set_banner(self._banner, "New passwords do not match.")
            return
        try:
            self._service.change_password(self._username, old, new)
        except AuthError:
            S.set_banner(self._banner, "Your current password is incorrect.")
            return
        except ValueError as e:
            S.set_banner(self._banner, str(e))
            return
        S.set_banner(self._banner, "Password updated successfully.", kind="ok")
        self.changed.emit(self._username)
