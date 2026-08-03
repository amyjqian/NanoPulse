"""Backup & Restore page for the Settings (Preferences) dialog.

Three cards — Create backup / Restore / Automatic backups — matching the
reference ``backup`` screen.  Create is available to all signed-in users;
Restore is admin-only (it can replace the admin account).
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QColor
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from src.backup import ConfigBackup

log = logging.getLogger(__name__)

# ── Settings (slate-blue) palette ─────────────────────────────────────────────
_BG = "#1b2431"
_CARD = "#1a2330"
_INPUT = "#141c28"
_BORDER = "#2b3646"
_BORDER2 = "#26313f"
_DASH = "#3a4656"
_HEAD = "#f0f2f5"
_TEXT = "#eef1f5"
_BODY = "#8a93a3"
_MUTED = "#6c7688"
_MONO_CLR = "#aeb6c2"
_BLUE = "#2f60d8"
_BLUE_HOVER = "#3d70e6"
_GREEN = "#5fce77"
_RED = "#f08080"
_AMBER = "#e0a63a"
_MONO = "'JetBrains Mono', Menlo, monospace"

_FILTER = "NanoPulse backup (*.npbak)"


class _Toggle(QWidget):
    """iOS-style on/off switch (42×24 track, sliding knob)."""

    toggled = Signal(bool)

    def __init__(self, on: bool = False, parent=None):
        super().__init__(parent)
        self._on = on
        self.setFixedSize(42, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def is_on(self) -> bool:
        return self._on

    def set_on(self, on: bool) -> None:
        if self._on != on:
            self._on = on
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on = not self._on
            self.update()
            self.toggled.emit(self._on)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QColor(_BLUE) if self._on else QColor("#33404f")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(0, 0, 42, 24, 12, 12)
        p.setBrush(QColor("#ffffff"))
        x = 21 if self._on else 3
        p.drawEllipse(x, 3, 18, 18)
        p.end()


class BackupPage(QWidget):
    """Emits ``reload_requested`` after a successful restore (host should sign
    the user out and return to Login so the new accounts/config take effect)."""

    reload_requested = Signal()

    def __init__(self, username: str, parent=None):
        super().__init__(parent)
        self._service = ConfigBackup(username)
        self._restore_path: str | None = None
        self._build_ui()

    # ── build ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header.
        header = QWidget()
        header.setStyleSheet(f"background: {_BG}; border-bottom: 1px solid {_BORDER};")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(26, 20, 26, 16)
        hl.setSpacing(6)
        title = QLabel("Backup & Restore")
        title.setStyleSheet(
            f"color: {_HEAD}; font-size: 18px; font-weight: 700; background: transparent;")
        subtitle = QLabel(
            "Save a copy of your app configuration, or restore it from a backup file.")
        subtitle.setStyleSheet(f"color: {_BODY}; font-size: 13px; background: transparent;")
        hl.addWidget(title)
        hl.addWidget(subtitle)
        root.addWidget(header)

        # Scrollable card column.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {_BG}; border: none; }}")
        body = QWidget()
        body.setStyleSheet(f"background: {_BG};")
        col = QVBoxLayout(body)
        col.setContentsMargins(26, 20, 26, 24)
        col.setSpacing(16)

        self._banner = QLabel()
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        col.addWidget(self._banner)

        col.addWidget(self._build_create_card())
        col.addWidget(self._build_restore_card())
        col.addWidget(self._build_auto_card())

        note = QLabel(
            "Backups include user accounts and roles. Passwords are stored only "
            "as secure hashes — never in plain text. Keep .npbak files somewhere safe.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {_MUTED}; font-size: 11.5px; background: transparent;")
        col.addWidget(note)
        col.addStretch()

        scroll.setWidget(body)
        root.addWidget(scroll, stretch=1)

    def _card(self) -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        w.setObjectName("bkCard")
        w.setStyleSheet(
            f"#bkCard {{ background: {_CARD}; border: 1px solid {_BORDER2};"
            " border-radius: 10px; }}")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(0)
        return w, lay

    def _h2(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {_TEXT}; font-size: 14.5px; font-weight: 600; background: transparent;")
        return lbl

    def _body_lbl(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {_BODY}; font-size: 12.5px; background: transparent;")
        return lbl

    def _primary_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(36)
        b.setStyleSheet(
            f"QPushButton {{ background: {_BLUE}; border: none; border-radius: 7px;"
            " color: #fff; font-size: 13px; font-weight: 500; padding: 0 15px; }}"
            f"QPushButton:hover {{ background: {_BLUE_HOVER}; }}"
            "QPushButton:disabled { background: #2a3446; color: #6a7280; }")
        return b

    def _outline_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(38)
        b.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid {_DASH};"
            f" border-radius: 7px; color: {_TEXT}; font-size: 13px; font-weight: 500;"
            " padding: 0 16px; }}"
            "QPushButton:hover { background: #232e3c; }"
            "QPushButton:disabled { color: #566173; border-color: #2a3446; }")
        return b

    # ── Card 1: create ───────────────────────────────────────────────────────

    def _build_create_card(self) -> QWidget:
        card, lay = self._card()
        row = QHBoxLayout()
        row.setSpacing(20)
        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(self._h2("Back up configuration"))
        left.addWidget(self._body_lbl(
            "Exports your config, user accounts, connections, contracts and "
            "strategy tiers to a single .npbak file."))
        self._last_lbl = QLabel(f"Last backup: {self._service.last_backup_display()}")
        self._last_lbl.setStyleSheet(f"color: {_MUTED}; font-size: 12px; background: transparent;")
        left.addWidget(self._last_lbl)
        row.addLayout(left, stretch=1)

        btn = self._primary_btn("Create backup")
        btn.clicked.connect(self._on_create)
        wrap = QVBoxLayout()
        wrap.addWidget(btn, alignment=Qt.AlignmentFlag.AlignTop)
        wrap.addStretch()
        row.addLayout(wrap)
        lay.addLayout(row)
        return card

    def _on_create(self) -> None:
        # Suggest the user's home folder for manual saves.
        from pathlib import Path
        default = str(Path.home() / self._service.default_filename())
        path, _ = QFileDialog.getSaveFileName(
            self, "Create backup", default, _FILTER)
        if not path:
            return
        if not path.endswith(".npbak"):
            path += ".npbak"
        try:
            self._service.export(path)
        except Exception as e:                                  # noqa: BLE001
            log.exception("Backup export failed")
            self._show_banner(f"Backup failed: {e}", ok=False)
            return
        self._last_lbl.setText(f"Last backup: {self._service.last_backup_display()}")
        self._show_banner("Backup file created successfully.", ok=True)

    # ── Card 2: restore ──────────────────────────────────────────────────────

    def _build_restore_card(self) -> QWidget:
        card, lay = self._card()
        lay.addWidget(self._h2("Restore from backup"))
        sub = self._body_lbl("Load a previously saved .npbak file.")
        sub.setContentsMargins(0, 6, 0, 0)
        lay.addWidget(sub)
        lay.addSpacing(12)

        # File picker: dashed choose button OR selected-file chip.
        self._choose_btn = QPushButton("⭱   Choose a backup file…")
        self._choose_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._choose_btn.setMinimumHeight(52)
        self._choose_btn.setStyleSheet(
            f"QPushButton {{ background: {_INPUT}; border: 1px dashed {_DASH};"
            f" border-radius: 8px; color: {_BODY}; font-size: 12.5px; }}"
            f"QPushButton:hover {{ border-color: #3b7ddd; color: {_MONO_CLR}; }}")
        self._choose_btn.clicked.connect(self._on_choose)
        lay.addWidget(self._choose_btn)

        self._chip = QWidget()
        self._chip.setObjectName("fileChip")
        self._chip.setStyleSheet(
            f"#fileChip {{ background: {_INPUT}; border: 1px solid {_BORDER};"
            " border-radius: 8px; }}")
        chip_l = QHBoxLayout(self._chip)
        chip_l.setContentsMargins(13, 10, 13, 10)
        self._chip_name = QLabel("")
        self._chip_name.setStyleSheet(
            f"color: {_TEXT}; font-size: 12.5px; font-family: {_MONO}; background: transparent;")
        change = QPushButton("Change")
        change.setCursor(Qt.CursorShape.PointingHandCursor)
        change.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {_BODY};"
            " font-size: 12px; }} QPushButton:hover { color: #eef1f5; }")
        change.clicked.connect(self._on_choose)
        chip_l.addWidget(QLabel("📄"))
        chip_l.addWidget(self._chip_name, stretch=1)
        chip_l.addWidget(change)
        self._chip.setVisible(False)
        lay.addWidget(self._chip)

        # Amber warning.
        warn = QWidget()
        warn.setObjectName("bkWarn")
        warn.setStyleSheet(
            "#bkWarn { background: rgba(224,166,58,.08);"
            " border: 1px solid rgba(224,166,58,.25); border-radius: 8px; }")
        wl = QHBoxLayout(warn)
        wl.setContentsMargins(13, 11, 13, 11)
        wtext = QLabel(
            "Restoring overwrites your current configuration and reloads the app.")
        wtext.setWordWrap(True)
        wtext.setStyleSheet("color: #cba766; font-size: 12px; background: transparent;")
        wl.addWidget(wtext)
        lay.addSpacing(14)
        lay.addWidget(warn)
        lay.addSpacing(16)

        self._restore_btn = self._outline_btn("Restore configuration")
        self._restore_btn.clicked.connect(self._on_restore)
        lay.addWidget(self._restore_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        return card

    def _on_choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a backup file", "", _FILTER)
        if not path:
            return
        ok, msg = self._service.validate(path)
        if not ok:
            self._show_banner(msg, ok=False)
            return
        self._restore_path = path
        from pathlib import Path
        self._chip_name.setText(Path(path).name)
        self._chip.setVisible(True)
        self._choose_btn.setVisible(False)
        self._show_banner("", ok=True)

    def _on_restore(self) -> None:
        if not self._restore_path:
            self._show_banner("Choose a backup file first.", ok=False)
            return
        if QMessageBox.warning(
            self, "Restore configuration",
            "This overwrites your current configuration, user accounts and "
            "databases with the backup, then signs you out to reload.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self._service.restore(self._restore_path)
        except Exception as e:                                  # noqa: BLE001
            log.exception("Restore failed")
            self._show_banner(f"Restore failed (no changes applied): {e}", ok=False)
            return
        QMessageBox.information(
            self, "Restore complete",
            "Configuration restored. The app will return to the sign-in screen "
            "to apply the changes.")
        self.reload_requested.emit()

    # ── Card 3: automatic ────────────────────────────────────────────────────

    def _build_auto_card(self) -> QWidget:
        card, lay = self._card()
        row = QHBoxLayout()
        row.setSpacing(20)
        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(self._h2("Automatic backups"))
        left.addWidget(self._body_lbl(
            "Keep a daily backup automatically, retaining the last 5 copies "
            "in the app data folder."))
        row.addLayout(left, stretch=1)
        self._toggle = _Toggle(self._service.is_auto_enabled())
        self._toggle.toggled.connect(self._on_toggle_auto)
        tw = QVBoxLayout()
        tw.addWidget(self._toggle, alignment=Qt.AlignmentFlag.AlignTop)
        tw.addStretch()
        row.addLayout(tw)
        lay.addLayout(row)
        return card

    def _on_toggle_auto(self, on: bool) -> None:
        self._service.set_auto_enabled(on)
        if on:
            # Kick off an immediate backup so the first daily copy exists.
            self._service.maybe_auto_backup()
            self._last_lbl.setText(f"Last backup: {self._service.last_backup_display()}")
            self._show_banner("Automatic daily backups enabled.", ok=True)
        else:
            self._show_banner("Automatic backups disabled.", ok=True)

    # ── banner ───────────────────────────────────────────────────────────────

    def _show_banner(self, text: str, *, ok: bool) -> None:
        if not text:
            self._banner.setVisible(False)
            return
        color = _GREEN if ok else _RED
        bg = "rgba(52,179,74,.1)" if ok else "rgba(226,59,59,.1)"
        border = "rgba(52,179,74,.35)" if ok else "rgba(226,59,59,.35)"
        self._banner.setStyleSheet(
            f"background: {bg}; border: 1px solid {border}; color: {color};"
            " border-radius: 8px; padding: 10px 13px; font-size: 12.5px;")
        self._banner.setText(text)
        self._banner.setVisible(True)
