"""
TradesColumnsDialog – preferences dialog for Trades column management.

Side-by-side QListWidgets (Selected / Available) with +/- row controls
and an internal-move drag-drop reorder on the Selected list.  A search
box above Available filters its items in place.

The dialog is *view-aware*: callers pass ``view_kind`` ("flat" or
"summary"), the ordered list of every known column, display headers, and
the currently visible columns.  ``Apply`` emits ``columnsApplied`` with
the new ordered list of visible keys; callers project that onto their
QTableView via ``setColumnHidden`` / ``header.moveSection``.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

# ── Theme tokens ──────────────────────────────────────────────────────────────
ACCENT     = "#CC6600"
ACCENT_DIM = "#8A4500"
BG_DIALOG  = "#181B22"
BG_PANEL   = "#1E222A"
BG_INPUT   = "#1A1D24"
BORDER     = "#30363D"
TEXT       = "#C9D1D9"
TEXT_BOLD  = "#E6EDF3"
TEXT_DIM   = "#8B949E"
LINK       = "#4C9AFF"

_DLG_QSS = f"""
    QDialog#tradesColumnsDialog {{
        background: {BG_DIALOG};
        color: {TEXT};
    }}
    QFrame#sidebar {{
        background: {BG_DIALOG};
        border-right: 1px solid {BORDER};
    }}
    QLabel.sidebarItem {{
        color: {TEXT_DIM};
        font-size: 13px;
        padding: 8px 18px;
    }}
    QLabel#sidebarItemActive {{
        color: {TEXT_BOLD};
        font-weight: 700;
        background: transparent;
    }}
    QLabel#dlgTitle {{
        color: {TEXT_BOLD};
        font-size: 18px;
        font-weight: 700;
    }}
    QLabel#backLink {{
        color: {LINK};
        font-size: 12px;
    }}
    QLabel#listTitle {{
        color: {TEXT};
        font-size: 13px;
        font-weight: 600;
        padding: 8px 12px 4px 12px;
    }}
    QFrame#listFrame {{
        background: {BG_PANEL};
        border: 1px solid {BORDER};
        border-radius: 6px;
    }}
    QListWidget {{
        background: {BG_PANEL};
        color: {TEXT};
        border: none;
        outline: none;
    }}
    QListWidget::item {{
        padding: 0px;
        border: none;
        background: transparent;
    }}
    QListWidget::item:selected {{
        background: {ACCENT_DIM};
        color: #FFFFFF;
    }}
    QLineEdit#availSearch {{
        background: {BG_INPUT};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 4px 8px;
        font-size: 11px;
    }}
    QLineEdit#availSearch:focus {{
        border-color: {ACCENT};
    }}
    QPushButton#rowAction {{
        background: #2a2f3a;
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 10px;
        min-width: 20px;
        max-width: 20px;
        min-height: 20px;
        max-height: 20px;
        font-size: 14px;
        font-weight: 700;
    }}
    QPushButton#rowAction:hover {{
        border-color: {ACCENT};
        color: {ACCENT};
    }}
    QLabel#dragHandle {{
        color: {TEXT_DIM};
        font-size: 14px;
        padding: 0 6px;
    }}
    QDialogButtonBox QPushButton {{
        background: {BG_PANEL};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 5px 14px;
        min-width: 72px;
    }}
    QDialogButtonBox QPushButton:hover {{
        border-color: {ACCENT};
        color: {ACCENT};
    }}
    QDialogButtonBox QPushButton:default {{
        background: {ACCENT};
        color: #FFFFFF;
        border-color: {ACCENT};
    }}
    QDialogButtonBox QPushButton:default:hover {{
        background: {ACCENT_DIM};
        border-color: {ACCENT_DIM};
    }}
"""


def _row_widget(label_text: str, action: str, parent=None) -> QWidget:
    """Build the per-item row: [±] label  [≡]? ."""
    w = QWidget(parent)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(8, 4, 8, 4)
    lay.setSpacing(10)

    btn = QPushButton("−" if action == "remove" else "+", w)
    btn.setObjectName("rowAction")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setProperty("actionKind", action)
    lay.addWidget(btn)

    lbl = QLabel(label_text, w)
    lbl.setStyleSheet(f"color: {TEXT}; font-size: 12px;")
    lay.addWidget(lbl, 1)

    if action == "remove":
        handle = QLabel("\u2630", w)  # ☰ hamburger
        handle.setObjectName("dragHandle")
        lay.addWidget(handle)

    # Expose widgets for later lookup
    w._action_btn = btn         # type: ignore[attr-defined]
    w._label = lbl              # type: ignore[attr-defined]
    return w


class TradesColumnsDialog(QDialog):
    """Modal dialog – two QListWidgets managing column visibility + order.

    Signal
    ------
    columnsApplied(list[str])
        Ordered list of visible column *keys* after the user applies
        the change.  Emitted on Apply / OK, not on Cancel.
    """

    columnsApplied = Signal(list)

    def __init__(self, *,
                 view_kind: str,
                 all_keys: list[str],
                 all_headers: list[str],
                 visible_keys: list[str],
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("tradesColumnsDialog")
        self.setWindowTitle("Preferences")
        self.setStyleSheet(_DLG_QSS)
        self.setModal(True)
        self.resize(820, 620)

        self._view_kind = view_kind
        # key → header lookup for display
        self._header_by_key: dict[str, str] = dict(zip(all_keys, all_headers))
        self._all_keys = list(all_keys)
        self._visible_keys = [k for k in visible_keys if k in self._header_by_key]

        self._build_ui()
        self._populate_lists()

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Left sidebar (decorative) ──
        sidebar = QFrame(self)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(160)
        sb_lay = QVBoxLayout(sidebar)
        sb_lay.setContentsMargins(0, 14, 0, 14)
        sb_lay.setSpacing(2)
        for name in ("Display", "Hotkeys", "Quick Trade",
                     "Columns", "Trading", "Messages", "Lock and Exit"):
            lbl = QLabel(name, sidebar)
            lbl.setProperty("class", "sidebarItem")
            lbl.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            if name == "Columns":
                lbl.setObjectName("sidebarItemActive")
            else:
                lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 13px; padding: 8px 18px;")
            sb_lay.addWidget(lbl)
        sb_lay.addStretch(1)
        root.addWidget(sidebar)

        # ── Right pane ──
        right = QWidget(self)
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(24, 18, 24, 18)
        right_lay.setSpacing(14)

        back_link = QLabel("\u25c0  Back to Column Management", right)
        back_link.setObjectName("backLink")
        right_lay.addWidget(back_link)

        title_text = ("Trades Columns" if self._view_kind == "flat"
                      else "Summary Columns")
        title = QLabel(title_text, right)
        title.setObjectName("dlgTitle")
        right_lay.addWidget(title)

        # Separator
        sep = QFrame(right)
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {BORDER}; background: {BORDER}; max-height: 1px;")
        right_lay.addWidget(sep)

        # Two lists side by side
        lists_row = QHBoxLayout()
        lists_row.setSpacing(16)

        lists_row.addWidget(self._build_selected_frame())
        lists_row.addWidget(self._build_available_frame())
        right_lay.addLayout(lists_row, 1)

        # Button bar
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel,
            parent=right,
        )
        btns.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._on_apply)
        btns.rejected.connect(self.reject)
        right_lay.addWidget(btns)

        root.addWidget(right, 1)

    def _build_selected_frame(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("listFrame")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        title = QLabel("Selected Columns", frame)
        title.setObjectName("listTitle")
        lay.addWidget(title)

        self._selected_list = QListWidget(frame)
        self._selected_list.setObjectName("selectedList")
        self._selected_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self._selected_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._selected_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._selected_list.setSpacing(1)
        lay.addWidget(self._selected_list, 1)
        return frame

    def _build_available_frame(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("listFrame")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(12, 8, 12, 0)
        hdr_row.setSpacing(8)
        title = QLabel("Available Columns", frame)
        title.setObjectName("listTitle")
        title.setContentsMargins(0, 0, 0, 0)
        hdr_row.addWidget(title)
        hdr_row.addStretch(1)

        self._avail_search = QLineEdit(frame)
        self._avail_search.setObjectName("availSearch")
        self._avail_search.setPlaceholderText("Search")
        self._avail_search.textChanged.connect(self._filter_available)
        self._avail_search.setFixedWidth(180)
        hdr_row.addWidget(self._avail_search)
        lay.addLayout(hdr_row)

        self._available_list = QListWidget(frame)
        self._available_list.setObjectName("availableList")
        self._available_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._available_list.setSpacing(1)
        lay.addWidget(self._available_list, 1)
        return frame

    # ── List population ─────────────────────────────────────────────────────

    def _populate_lists(self) -> None:
        selected = list(self._visible_keys)
        available = [k for k in self._all_keys if k not in selected]
        for key in selected:
            self._add_selected_row(key)
        for key in available:
            self._add_available_row(key)

    def _add_selected_row(self, key: str) -> None:
        w = _row_widget(self._header_by_key.get(key, key), "remove", self._selected_list)
        w._action_btn.clicked.connect(  # type: ignore[attr-defined]
            lambda _=False, k=key: self._move_to_available(k))
        it = QListWidgetItem(self._selected_list)
        it.setData(Qt.ItemDataRole.UserRole, key)
        it.setSizeHint(w.sizeHint())
        self._selected_list.addItem(it)
        self._selected_list.setItemWidget(it, w)

    def _add_available_row(self, key: str) -> None:
        w = _row_widget(self._header_by_key.get(key, key), "add", self._available_list)
        w._action_btn.clicked.connect(  # type: ignore[attr-defined]
            lambda _=False, k=key: self._move_to_selected(k))
        it = QListWidgetItem(self._available_list)
        it.setData(Qt.ItemDataRole.UserRole, key)
        it.setSizeHint(w.sizeHint())
        self._available_list.addItem(it)
        self._available_list.setItemWidget(it, w)

    # ── Row actions ─────────────────────────────────────────────────────────

    def _take_row(self, lw: QListWidget, key: str) -> bool:
        for i in range(lw.count()):
            it = lw.item(i)
            if it is not None and it.data(Qt.ItemDataRole.UserRole) == key:
                lw.takeItem(i)
                return True
        return False

    def _move_to_available(self, key: str) -> None:
        if self._take_row(self._selected_list, key):
            self._add_available_row(key)
            self._filter_available(self._avail_search.text())

    def _move_to_selected(self, key: str) -> None:
        if self._take_row(self._available_list, key):
            self._add_selected_row(key)

    def _filter_available(self, text: str) -> None:
        needle = (text or "").strip().lower()
        for i in range(self._available_list.count()):
            it = self._available_list.item(i)
            key = it.data(Qt.ItemDataRole.UserRole) or ""
            header = self._header_by_key.get(key, key).lower()
            it.setHidden(bool(needle) and needle not in header and needle not in key.lower())

    # ── Apply / OK ──────────────────────────────────────────────────────────

    def _current_selected_keys(self) -> list[str]:
        keys: list[str] = []
        for i in range(self._selected_list.count()):
            it = self._selected_list.item(i)
            if it is None:
                continue
            k = it.data(Qt.ItemDataRole.UserRole)
            if k:
                keys.append(str(k))
        return keys

    def _on_apply(self) -> None:
        self.columnsApplied.emit(self._current_selected_keys())
        self.accept()
