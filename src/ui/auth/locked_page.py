"""Locked screen — countdown until sign-in is re-enabled."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import style as S


class LockedPage(QWidget):
    """Ticks a countdown; emits ``expired()`` when it reaches zero."""

    expired = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._left = 0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.addStretch()

        col = QVBoxLayout()
        col.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon = QLabel("🔒")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            f"color: {S.DANGER}; font-size: 30px; background: rgba(226,59,59,.1);"
            " border: 1px solid rgba(226,59,59,.35); border-radius: 18px; padding: 14px;")
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(icon)
        row.addStretch()
        col.addLayout(row)
        col.addSpacing(22)

        title = S.make_title("Account temporarily locked", size=21)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(title)

        blurb = QLabel(
            "Too many failed sign-in attempts. For security, sign-in is "
            "disabled for a few minutes.")
        blurb.setWordWrap(True)
        blurb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        blurb.setFixedWidth(360)
        blurb.setStyleSheet(
            f"color: {S.TEXT_MUTED}; font-size: 13px; background: transparent;")
        col.addSpacing(10)
        crow = QHBoxLayout()
        crow.addStretch()
        crow.addWidget(blurb)
        crow.addStretch()
        col.addLayout(crow)
        col.addSpacing(24)

        box = QVBoxLayout()
        box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cap = QLabel("TRY AGAIN IN")
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cap.setStyleSheet(
            f"color: {S.TEXT_FAINT}; font-size: 11px; letter-spacing: 1px;"
            " background: transparent;")
        self._clock = QLabel("05:00")
        self._clock.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._clock.setStyleSheet(
            f"color: {S.DANGER}; font-size: 34px; font-weight: 600;"
            " font-family: 'JetBrains Mono', Menlo, monospace; background: transparent;")
        holder = QWidget()
        holder.setObjectName("lockHolder")
        holder.setStyleSheet(
            f"#lockHolder {{ background: {S.BG_CARD}; border: 1px solid {S.BORDER};"
            " border-radius: 12px; }}")
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(34, 18, 34, 18)
        hl.setSpacing(4)
        hl.addWidget(cap)
        hl.addWidget(self._clock)
        hrow = QHBoxLayout()
        hrow.addStretch()
        hrow.addWidget(holder)
        hrow.addStretch()
        col.addLayout(hrow)

        outer.addLayout(col)
        outer.addStretch()

    def start(self, seconds: int) -> None:
        self._left = max(0, int(seconds))
        self._render()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        self._left -= 1
        if self._left <= 0:
            self._timer.stop()
            self.expired.emit()
            return
        self._render()

    def _render(self) -> None:
        m, s = divmod(self._left, 60)
        self._clock.setText(f"{m:02d}:{s:02d}")
