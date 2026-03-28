"""
Main canvas / content area – placeholder for future views (tables, charts, etc.).
"""
from PySide6.QtWidgets import QWidget, QSizePolicy
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QColor, QFont, QPen

CONTENT_BG = "#0D1117"
BORDER = "#21262D"
TEXT_DIM = "#484F58"


class Canvas(QWidget):
    """Empty main content area, ready to host future widgets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Fill background
        p.fillRect(self.rect(), QColor(CONTENT_BG))

        # Subtle grid dots (optional visual texture)
        dot_color = QColor(BORDER)
        dot_color.setAlpha(80)
        p.setPen(QPen(dot_color, 1))
        spacing = 32
        for x in range(spacing, self.width(), spacing):
            for y in range(spacing, self.height(), spacing):
                p.drawPoint(x, y)

        # Placeholder label – first line
        p.setPen(QColor(TEXT_DIM))
        font_title = QFont("SF Pro Display, Helvetica Neue, Arial", 15)
        font_title.setWeight(QFont.Weight.Medium)
        p.setFont(font_title)

        centre_x = self.width() // 2
        centre_y = self.height() // 2

        p.drawText(
            self.rect().adjusted(0, 0, 0, -30),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
            "Empty Canvas for Future Views",
        )

        # Second line (subtitle)
        font_sub = QFont("SF Pro Display, Helvetica Neue, Arial", 13)
        p.setFont(font_sub)
        sub_color = QColor(TEXT_DIM)
        sub_color.setAlpha(160)
        p.setPen(sub_color)
        p.drawText(
            self.rect().adjusted(0, 30, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "(e.g., QTableView, Charts)",
        )

        p.end()


