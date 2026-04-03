"""
Bottom status bar with live clock and market status.
"""
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QFrame
from PySide6.QtCore import Qt, QTimer, QDateTime
from PySide6.QtGui import QPainter, QColor, QFont, QPen, QBrush

BAR_HEIGHT = 24
ACCENT = "#3FB950"
TEXT_PRIMARY = "#C9D1D9"
TEXT_SECONDARY = "#8B949E"
TEXT_DIM = "#484F58"
BAR_BG = "#161B22"
BORDER = "#30363D"


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
        font = QFont("SF Mono, Consolas, Menlo, monospace", 8)
        if bold:
            font.setWeight(QFont.Weight.Medium)
        lbl.setFont(font)

    # ── Timer ─────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        self._time_lbl.setText(
            QDateTime.currentDateTime().toString("hh:mm:ss"))

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
