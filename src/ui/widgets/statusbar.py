"""
Bottom status bar with live clock and market status.
"""
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QFrame
from PySide6.QtCore import Qt, QTimer, QDateTime
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QBrush

BAR_HEIGHT = 24
ACCENT = "#3FB950"
TEXT_PRIMARY = "#E6EBF2"
TEXT_SECONDARY = "#A3AFC2"
TEXT_DIM = "#6A7690"
BAR_BG = "#151922"
BORDER = "#262E3F"


class StatusDot(QWidget):
    """Animated green status indicator dot."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self._color = "#3FB950"

    def set_color(self, hex_color: str) -> None:
        self._color = hex_color
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(QColor(self._color)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(1, 1, 8, 8)
        p.end()


class Separator(QFrame):
    """Thin vertical separator for the status bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.VLine)
        self.setFixedHeight(14)
        self.setFixedWidth(1)


class StatusBar(QWidget):
    """Custom bottom status bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(BAR_HEIGHT)
        self._setup_ui()
        self._apply_colors()

        # Live clock
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._tick)
        self._clock.start(1000)
        self._tick()

        # One-shot timer for transient ``show_message`` overlays;
        # ``_normal_status_text`` snapshots the real status label so we
        # can restore it when the overlay expires.
        self._normal_status_text = "NORMAL OPERATIONS"
        self._transient_timer = QTimer(self)
        self._transient_timer.setSingleShot(True)
        self._transient_timer.timeout.connect(self._clear_transient)

    # ── Build UI ─────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 10, 0)
        layout.setSpacing(0)

        # LEFT: status dot + label
        self._dot = StatusDot(self)
        layout.addWidget(self._dot)
        layout.addSpacing(6)

        self._status_lbl = QLabel("NORMAL OPERATIONS", self)
        self._mono_font(self._status_lbl, bold=True)
        layout.addWidget(self._status_lbl)

        layout.addStretch(1)

        # RIGHT group
        self._build_lbl = self._make_label("Build 1.0.0")
        layout.addWidget(self._build_lbl)

        layout.addSpacing(8)
        self._sep1 = self._make_label("|")
        layout.addWidget(self._sep1)
        layout.addSpacing(8)

        self._market_lbl = self._make_label("Market: Close")
        layout.addWidget(self._market_lbl)

        layout.addSpacing(8)
        self._sep2 = self._make_label("|")
        layout.addWidget(self._sep2)
        layout.addSpacing(8)

        self._time_lbl = self._make_label("--:--:--")
        layout.addWidget(self._time_lbl)

    def _make_label(self, text: str) -> QLabel:
        lbl = QLabel(text, self)
        self._mono_font(lbl)
        return lbl

    @staticmethod
    def _mono_font(lbl: QLabel, bold: bool = False) -> None:
        font = QFont("JetBrains Mono, Menlo, Consolas, monospace", 8)
        if bold:
            font.setWeight(QFont.Weight.Medium)
        lbl.setFont(font)

    # ── Timer ─────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        self._time_lbl.setText(
            QDateTime.currentDateTime().toString("hh:mm:ss"))

    # ── Transient messages ───────────────────────────────────────────────────

    def show_message(self, text: str, duration_ms: int = 6000) -> None:
        """Show ``text`` in the leading status slot for ``duration_ms``
        milliseconds, replacing the usual "NORMAL OPERATIONS" string,
        then restore the original.  Calling again before the timer
        expires resets the countdown."""
        if not text:
            return
        self._status_lbl.setText(text)
        self._status_lbl.setStyleSheet(
            f"color: #F2A13A; letter-spacing: 0.8px;")  # amber accent
        self._transient_timer.stop()
        self._transient_timer.start(max(int(duration_ms), 500))

    def _clear_transient(self) -> None:
        self._status_lbl.setText(self._normal_status_text)
        self._status_lbl.setStyleSheet(
            f"color: {TEXT_PRIMARY}; letter-spacing: 1.2px;")

    def _apply_colors(self) -> None:
        self._dot.set_color(ACCENT)

        self._status_lbl.setStyleSheet(
            f"color: {TEXT_PRIMARY}; letter-spacing: 1.2px;"
        )
        for lbl in (self._build_lbl, self._market_lbl, self._time_lbl):
            lbl.setStyleSheet(f"color: {TEXT_SECONDARY};")
        for sep in (self._sep1, self._sep2):
            sep.setStyleSheet(f"color: {TEXT_DIM};")

    # ── Background ────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(BAR_BG))
        # Top border line
        p.setPen(QPen(QColor(BORDER), 1))
        p.drawLine(0, 0, self.width(), 0)
        p.end()
