"""Shared palette + styled-widget factory helpers for the auth screens.

Colours are ported straight from ``NanoPulse Login.dc.html`` (near-black
terminal theme) so the login flow reads as the same product as the reference.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QLineEdit, QMessageBox, QPushButton

# Bundled images live in <project-root>/resources/images.
_IMAGES_DIR = Path(__file__).resolve().parents[3] / "resources" / "images"


def image_path(name: str) -> str:
    """Absolute path to a bundled image (e.g. ``image_path('admin.png')``)."""
    return str(_IMAGES_DIR / name)


# ── Styled message box ────────────────────────────────────────────────────────
# A bare QMessageBox renders dark-on-dark under the app's Fusion dark palette
# (unreadable), so force a readable dark theme with an explicit stylesheet.
_MSGBOX_QSS = """
    QMessageBox { background: #1e2230; }
    QMessageBox QLabel { color: #C9D1D9; font-size: 13px; background: transparent; }
    QPushButton {
        background: #272b3a; color: #C9D1D9;
        border: 1px solid #30363D; border-radius: 6px;
        padding: 6px 16px; min-width: 76px;
    }
    QPushButton:hover { background: #2f3547; }
    QPushButton:default { background: #2d7ff9; color: #fff; border: none; }
    QPushButton:default:hover { background: #4b93fb; }
"""


def message_box(
    parent,
    title: str,
    text: str,
    *,
    icon=QMessageBox.Icon.Information,
    buttons=QMessageBox.StandardButton.Ok,
    default=None,
) -> QMessageBox.StandardButton:
    """Show a dark-themed, readable modal message box and return the button
    the user clicked."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(icon)
    box.setStandardButtons(buttons)
    if default is not None:
        box.setDefaultButton(default)
    box.setStyleSheet(_MSGBOX_QSS)
    return box.exec()

# ── Palette (auth / terminal theme) ──────────────────────────────────────────
BG_APP = "#060608"
BG_CARD = "#101013"
BG_PANEL = "#0a0a0d"
BG_INPUT = "#0d0d10"
BORDER = "#26262d"
BORDER_INPUT = "#2a2a31"
TEXT = "#eeeef0"
TEXT_MUTED = "#8a8a92"
TEXT_LABEL = "#a2a2aa"
TEXT_FAINT = "#55555e"
ACCENT_TEAL = "#2ec4a6"
PRIMARY_BLUE = "#2d7ff9"
PRIMARY_BLUE_HOVER = "#4b93fb"
DANGER = "#e23b3b"
DANGER_TEXT = "#f08080"
WARN = "#e0a63a"
OK_GREEN = "#34b34a"

# Strength-bar "off" colour
BAR_OFF = "#1f1f26"


def input_qss() -> str:
    return f"""
        QLineEdit {{
            background: {BG_INPUT};
            border: 1px solid {BORDER_INPUT};
            border-radius: 8px;
            color: {TEXT};
            font-size: 14px;
            padding: 0 13px;
            min-height: 42px;
        }}
        QLineEdit:focus {{
            border: 1px solid {ACCENT_TEAL};
        }}
        QLineEdit::placeholder {{ color: {TEXT_FAINT}; }}
    """


def make_input(placeholder: str = "", *, password: bool = False) -> QLineEdit:
    le = QLineEdit()
    le.setPlaceholderText(placeholder)
    le.setStyleSheet(input_qss())
    le.setMinimumHeight(42)
    if password:
        le.setEchoMode(QLineEdit.EchoMode.Password)
    return le


def make_label(text: str, *, size: int = 12, color: str = TEXT_LABEL,
               bold: bool = False) -> QLabel:
    lbl = QLabel(text)
    weight = 600 if bold else 500
    lbl.setStyleSheet(
        f"color: {color}; font-size: {size}px; font-weight: {weight};"
        " background: transparent; border: none;"
    )
    return lbl


def make_title(text: str, *, size: int = 22) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: {TEXT}; font-size: {size}px; font-weight: 600;"
        " background: transparent; border: none;"
    )
    return lbl


def make_primary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setMinimumHeight(44)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {PRIMARY_BLUE};
            border: none;
            border-radius: 8px;
            color: #fff;
            font-size: 15px;
            font-weight: 600;
        }}
        QPushButton:hover {{ background: {PRIMARY_BLUE_HOVER}; }}
        QPushButton:disabled {{ background: #24303f; color: #6a7280; }}
    """)
    return btn


def make_secondary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setMinimumHeight(44)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: transparent;
            border: 1px solid {BORDER_INPUT};
            border-radius: 8px;
            color: {TEXT_LABEL};
            font-size: 14px;
            font-weight: 500;
            padding: 0 18px;
        }}
        QPushButton:hover {{ background: #17171c; color: {TEXT}; }}
    """)
    return btn


def make_banner(kind: str = "error") -> QLabel:
    """A hidden-by-default inline message banner.  ``kind`` ∈ {error, ok}."""
    lbl = QLabel()
    lbl.setWordWrap(True)
    lbl.setVisible(False)
    if kind == "ok":
        bg, border, color = "rgba(52,179,74,.1)", "rgba(52,179,74,.35)", OK_GREEN
    else:
        bg, border, color = "rgba(226,59,59,.1)", "rgba(226,59,59,.35)", DANGER_TEXT
    lbl.setStyleSheet(
        f"background: {bg}; border: 1px solid {border}; color: {color};"
        " border-radius: 8px; padding: 10px 12px; font-size: 13px;"
    )
    return lbl


def set_banner(lbl: QLabel, text: str, kind: str = "error") -> None:
    """Update a banner's text + colour and show/hide it."""
    if not text:
        lbl.setVisible(False)
        return
    if kind == "ok":
        bg, border, color = "rgba(52,179,74,.1)", "rgba(52,179,74,.35)", OK_GREEN
    else:
        bg, border, color = "rgba(226,59,59,.1)", "rgba(226,59,59,.35)", DANGER_TEXT
    lbl.setStyleSheet(
        f"background: {bg}; border: 1px solid {border}; color: {color};"
        " border-radius: 8px; padding: 10px 12px; font-size: 13px;"
    )
    lbl.setText(text)
    lbl.setVisible(True)
