"""Voluntary change-password modal, opened from Preferences by any signed-in
user.  (The *forced* first-login change uses ``ChangePwPage`` in the auth
window instead.)"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from src.auth import AuthService, AuthError

_BG = "#1e2230"
_BG_INPUT = "#272b3a"
_BORDER = "#30363D"
_TEXT = "#C9D1D9"
_TEXT_DIM = "#8B949E"
_BLUE = "#0071e3"
_GREEN = "#5fce77"
_DANGER = "#f08080"


class ChangePasswordDialog(QDialog):
    def __init__(self, service: AuthService, username: str, parent=None):
        super().__init__(parent)
        self._service = service
        self._username = username
        self.setWindowTitle("Change password")
        self.setModal(True)
        self.setFixedWidth(380)
        self.setStyleSheet(f"QDialog {{ background: {_BG}; }}")
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(0)

        title = QLabel("Change password")
        title.setStyleSheet(
            f"color: {_TEXT}; font-size: 17px; font-weight: 600; background: transparent;")
        root.addWidget(title)
        root.addSpacing(16)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        root.addWidget(self._banner)
        root.addSpacing(8)

        self._old = self._field(root, "Current password")
        self._new = self._field(root, "New password")
        self._new2 = self._field(root, "Confirm new password")
        root.addSpacing(12)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        cancel = QPushButton("Cancel")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setMinimumHeight(40)
        cancel.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {_BORDER};"
            f" border-radius: 8px; color: {_TEXT_DIM}; font-size: 13px; padding: 0 18px; }}"
            f"QPushButton:hover {{ background: #262b3a; color: {_TEXT}; }}")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Update password")
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.setMinimumHeight(40)
        save.setStyleSheet(
            f"QPushButton {{ background: {_BLUE}; border: none; border-radius: 8px;"
            " color: #fff; font-size: 13px; font-weight: 600; }}"
            "QPushButton:hover { background: #1a86ff; }")
        save.clicked.connect(self._submit)
        btn_row.addWidget(cancel)
        btn_row.addWidget(save, stretch=1)
        root.addLayout(btn_row)

        self._new2.returnPressed.connect(self._submit)

    def _field(self, root: QVBoxLayout, label: str) -> QLineEdit:
        lbl = QLabel(label)
        lbl.setStyleSheet(
            f"color: #aeb6c2; font-size: 12px; font-weight: 500; background: transparent;")
        root.addWidget(lbl)
        root.addSpacing(6)
        le = QLineEdit()
        le.setEchoMode(QLineEdit.EchoMode.Password)
        le.setMinimumHeight(40)
        le.setStyleSheet(
            f"QLineEdit {{ background: {_BG_INPUT}; border: 1px solid {_BORDER};"
            f" border-radius: 8px; color: {_TEXT}; font-size: 13px; padding: 0 12px; }}"
            f"QLineEdit:focus {{ border: 1px solid {_BLUE}; }}")
        root.addWidget(le)
        root.addSpacing(14)
        return le

    def _show_banner(self, text: str, ok: bool = False) -> None:
        color = _GREEN if ok else _DANGER
        bg = "rgba(52,179,74,.1)" if ok else "rgba(226,59,59,.1)"
        border = "rgba(52,179,74,.35)" if ok else "rgba(226,59,59,.35)"
        self._banner.setStyleSheet(
            f"background: {bg}; border: 1px solid {border}; color: {color};"
            " border-radius: 8px; padding: 9px 11px; font-size: 12px;")
        self._banner.setText(text)
        self._banner.setVisible(True)

    def _submit(self) -> None:
        old, new, new2 = self._old.text(), self._new.text(), self._new2.text()
        if new != new2:
            self._show_banner("New passwords do not match.")
            return
        try:
            self._service.change_password(self._username, old, new)
        except AuthError:
            self._show_banner("Your current password is incorrect.")
            return
        except ValueError as e:
            self._show_banner(str(e))
            return
        self._show_banner("Password updated successfully.", ok=True)
        # Give the user a moment to see the confirmation, then close.
        from PySide6.QtCore import QTimer
        QTimer.singleShot(700, self.accept)
