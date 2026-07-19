"""Add-user modal — Username + Display name only.  Role is always Trader
(there is no way to create a second admin through the UI)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from src.auth import AuthService

_BG = "#1e2230"
_BG_INPUT = "#272b3a"
_BORDER = "#30363D"
_TEXT = "#C9D1D9"
_TEXT_DIM = "#8B949E"
_BLUE = "#2f60d8"
_GREEN = "#5fce77"


class AddUserDialog(QDialog):
    """On accept, exposes ``created_user`` and ``temp_password``."""

    def __init__(self, service: AuthService, parent=None):
        super().__init__(parent)
        self._service = service
        self.created_user = None
        self.temp_password = ""
        self.setWindowTitle("Add user")
        self.setModal(True)
        self.setFixedWidth(400)
        self.setStyleSheet(f"QDialog {{ background: {_BG}; }}")
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(0)

        title = QLabel("Add user")
        title.setStyleSheet(
            f"color: {_TEXT}; font-size: 17px; font-weight: 600; background: transparent;")
        root.addWidget(title)
        sub = QLabel("They'll set their own password on first sign-in.")
        sub.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 12px; background: transparent;")
        root.addSpacing(4)
        root.addWidget(sub)
        root.addSpacing(18)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        self._banner.setStyleSheet(
            "background: rgba(226,59,59,.1); border: 1px solid rgba(226,59,59,.35);"
            " color: #f08080; border-radius: 8px; padding: 9px 11px; font-size: 12px;")
        root.addWidget(self._banner)
        root.addSpacing(8)

        root.addWidget(self._label("Username"))
        root.addSpacing(6)
        self._user = self._input("e.g. j.walker")
        root.addWidget(self._user)
        root.addSpacing(14)

        root.addWidget(self._label("Display name"))
        root.addSpacing(6)
        self._name = self._input("e.g. Jordan Walker")
        root.addWidget(self._name)
        root.addSpacing(14)

        badge = QLabel("  Trader  ")
        badge.setStyleSheet(
            f"background: rgba(52,179,74,.14); color: {_GREEN}; border-radius: 20px;"
            " font-size: 11px; font-weight: 600; padding: 3px 9px;")
        note = QLabel("New users are traders — they can place and manage trades.")
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {_TEXT_DIM}; font-size: 11px; background: transparent;")
        badge_row = QHBoxLayout()
        badge_row.setSpacing(9)
        badge_row.addWidget(badge)
        badge_row.addWidget(note, stretch=1)
        wrap = QWidget()
        wrap.setObjectName("traderNote")
        wrap.setStyleSheet(
            "#traderNote { background: rgba(52,179,74,.08);"
            " border: 1px solid rgba(52,179,74,.25); border-radius: 8px; }")
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(12, 10, 12, 10)
        wl.addLayout(badge_row)
        root.addWidget(wrap)
        root.addSpacing(20)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        cancel = QPushButton("Cancel")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setMinimumHeight(42)
        cancel.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {_BORDER};"
            f" border-radius: 8px; color: {_TEXT_DIM}; font-size: 13px; padding: 0 18px; }}"
            f"QPushButton:hover {{ background: #1f2836; color: {_TEXT}; }}")
        cancel.clicked.connect(self.reject)
        create = QPushButton("Create user")
        create.setCursor(Qt.CursorShape.PointingHandCursor)
        create.setMinimumHeight(42)
        create.setStyleSheet(
            f"QPushButton {{ background: {_BLUE}; border: none; border-radius: 8px;"
            " color: #fff; font-size: 13px; font-weight: 600; }}"
            "QPushButton:hover { background: #3d70e6; }")
        create.clicked.connect(self._create)
        btn_row.addWidget(cancel)
        btn_row.addWidget(create, stretch=1)
        root.addLayout(btn_row)

        self._user.returnPressed.connect(self._create)
        self._name.returnPressed.connect(self._create)

    def _label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: #aeb6c2; font-size: 12px; font-weight: 500; background: transparent;")
        return lbl

    def _input(self, placeholder: str) -> QLineEdit:
        le = QLineEdit()
        le.setPlaceholderText(placeholder)
        le.setMinimumHeight(40)
        le.setStyleSheet(
            f"QLineEdit {{ background: {_BG_INPUT}; border: 1px solid {_BORDER};"
            f" border-radius: 8px; color: {_TEXT}; font-size: 13px; padding: 0 12px; }}"
            f"QLineEdit:focus {{ border: 1px solid #3b7ddd; }}")
        return le

    def _create(self) -> None:
        try:
            user, temp = self._service.add_trader(
                self._user.text(), self._name.text())
        except ValueError as e:
            self._banner.setText(str(e))
            self._banner.setVisible(True)
            return
        self.created_user = user
        self.temp_password = temp
        self.accept()
