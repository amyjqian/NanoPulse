"""
Columns section used inside ``PreferencesDialog``.

Three pages swapped via a ``QStackedWidget``:

* ``_ColumnsOverview`` – two rows: Trades / Summary of Trades (Image #4).
* ``_ColumnsEditor`` – per-view Selected / Available two-pane editor with
  drag-drop reorder, per-row +/- buttons, and search (Images #5 and #6).

The page is view-aware through a duck-typed ``columns_provider`` object
supplied by ``PreferencesDialog``:

    column_state(kind) -> (all_keys, all_headers, visible_keys)
    apply_columns(kind, visible_keys)

Where ``kind`` is ``"flat"`` or ``"summary"``.
"""
from __future__ import annotations

import logging
from typing import Protocol

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
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

SUBVIEW_OVERVIEW = "overview"
SUBVIEW_FLAT     = "flat"
SUBVIEW_SUMMARY  = "summary"


class ColumnsProvider(Protocol):
    """Duck-typed provider the page uses to read/write column state."""

    def column_state(self, kind: str) -> tuple[list[str], list[str], list[str]]:
        """Returns (all_keys, all_headers, visible_keys)."""
        ...

    def apply_columns(self, kind: str, visible_keys: list[str]) -> None:
        ...


# ═══════════════════════════════════════════════════════════════════════════════
#  Per-item row widget (Selected list: -/handle.  Available list: +)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_row_widget(label_text: str, action: str, parent=None) -> QWidget:
    """Build the per-item row widget:  [±] label  [≡]? ."""
    w = QWidget(parent)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(8, 4, 8, 4)
    lay.setSpacing(10)

    btn = QPushButton("\u2212" if action == "remove" else "+", w)
    btn.setObjectName("rowAction")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    lay.addWidget(btn)

    lbl = QLabel(label_text, w)
    lbl.setStyleSheet(f"color: {TEXT}; font-size: 12px;")
    lay.addWidget(lbl, 1)

    if action == "remove":
        handle = QLabel("\u2630", w)  # ☰
        handle.setObjectName("dragHandle")
        lay.addWidget(handle)

    w._action_btn = btn   # type: ignore[attr-defined]
    w._label = lbl        # type: ignore[attr-defined]
    return w


# ═══════════════════════════════════════════════════════════════════════════════
#  Overview page (Image #4)
# ═══════════════════════════════════════════════════════════════════════════════

_OVERVIEW_QSS = f"""
    QLabel#colTitle {{
        color: {TEXT_BOLD};
        font-size: 18px;
        font-weight: 700;
    }}
    QLabel#colSubtitle {{
        color: {TEXT_DIM};
        font-size: 12px;
    }}
    QLabel#defaultViewsTab {{
        color: {TEXT_BOLD};
        font-size: 12px;
        font-weight: 700;
        padding: 6px 0 6px 0;
        border-bottom: 2px solid {LINK};
    }}
    QLabel#customViewsTab {{
        color: {TEXT_DIM};
        font-size: 12px;
        font-weight: 600;
        padding: 6px 0 6px 0;
    }}
    QFrame#overviewCard {{
        background: {BG_PANEL};
        border: 1px solid {BORDER};
        border-radius: 6px;
    }}
    QFrame#overviewRow {{
        background: transparent;
        border: none;
        border-bottom: 1px solid {BORDER};
    }}
    QFrame#overviewRow[last="true"] {{
        border-bottom: none;
    }}
    QFrame#overviewRow:hover {{
        background: #232836;
    }}
    QLabel#overviewRowTitle {{
        color: {TEXT_BOLD};
        font-size: 13px;
        font-weight: 600;
    }}
    QLabel#overviewRowSubtitle {{
        color: {TEXT_DIM};
        font-size: 11px;
    }}
    QLabel#overviewIcon {{
        color: {TEXT_DIM};
        font-size: 16px;
        padding: 0 4px;
    }}
    QLabel#overviewIconActive {{
        color: {ACCENT};
        font-size: 16px;
        padding: 0 4px;
    }}
"""


class _OverviewRow(QFrame):
    """Clickable row in the Column Management overview."""
    clicked = Signal(str)  # kind = "flat" or "summary"

    def __init__(self, kind: str, title: str, subtitle: str,
                 icon_char: str, active: bool, parent=None):
        super().__init__(parent)
        self._kind = kind
        self.setObjectName("overviewRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(14)

        icon = QLabel(icon_char, self)
        icon.setObjectName("overviewIconActive" if active else "overviewIcon")
        icon.setFixedWidth(28)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        t = QLabel(title, self)
        t.setObjectName("overviewRowTitle")
        s = QLabel(subtitle, self)
        s.setObjectName("overviewRowSubtitle")
        s.setWordWrap(True)
        text_col.addWidget(t)
        text_col.addWidget(s)
        lay.addLayout(text_col, 1)

    def mousePressEvent(self, event) -> None:  # noqa: D401
        self.clicked.emit(self._kind)
        super().mousePressEvent(event)


class _ColumnsOverview(QWidget):
    """Top-level overview page – two rows that open an editor."""

    rowSelected = Signal(str)   # "flat" or "summary"

    def __init__(self, flat_summary: str, summary_summary: str,
                 parent=None):
        super().__init__(parent)
        self.setStyleSheet(_OVERVIEW_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(14)

        title = QLabel("Column Management", self)
        title.setObjectName("colTitle")
        root.addWidget(title)

        sub_row = QHBoxLayout()
        sub_row.setSpacing(6)
        sub = QLabel("Select which market data columns to display", self)
        sub.setObjectName("colSubtitle")
        info = QLabel("\u24d8", self)  # ⓘ
        info.setObjectName("colSubtitle")
        sub_row.addWidget(sub)
        sub_row.addWidget(info)
        sub_row.addStretch(1)
        root.addLayout(sub_row)

        # Tabs strip (decorative — only Default Views is functional here)
        tabs = QHBoxLayout()
        tabs.setSpacing(18)
        default_tab = QLabel("Default Views", self)
        default_tab.setObjectName("defaultViewsTab")
        custom_tab = QLabel("Custom Views", self)
        custom_tab.setObjectName("customViewsTab")
        tabs.addWidget(default_tab)
        tabs.addWidget(custom_tab)
        tabs.addStretch(1)
        root.addLayout(tabs)

        # Card holding the two rows
        card = QFrame(self)
        card.setObjectName("overviewCard")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        flat_row = _OverviewRow(
            kind="flat", title="Trades", subtitle=flat_summary,
            icon_char="\u2714", active=True, parent=card,   # ✔ check
        )
        summary_row = _OverviewRow(
            kind="summary", title="Summary of Trades",
            subtitle=summary_summary, icon_char="\u25A4",    # ▤ grid-ish
            active=False, parent=card,
        )
        summary_row.setProperty("last", "true")
        flat_row.clicked.connect(self.rowSelected.emit)
        summary_row.clicked.connect(self.rowSelected.emit)
        card_lay.addWidget(flat_row)
        card_lay.addWidget(summary_row)

        root.addWidget(card)
        root.addStretch(1)


# ═══════════════════════════════════════════════════════════════════════════════
#  Editor page (Images #5 and #6)
# ═══════════════════════════════════════════════════════════════════════════════

_EDITOR_QSS = f"""
    QLabel#backLink {{
        color: {LINK};
        font-size: 12px;
    }}
    QLabel#editorTitle {{
        color: {TEXT_BOLD};
        font-size: 18px;
        font-weight: 700;
    }}
    QFrame#listFrame {{
        background: {BG_PANEL};
        border: 1px solid {BORDER};
        border-radius: 6px;
    }}
    QLabel#listTitle {{
        color: {TEXT};
        font-size: 13px;
        font-weight: 600;
        padding: 8px 12px 4px 12px;
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


class _ColumnsEditor(QWidget):
    """Two-list column editor, used for both flat and summary views."""

    applied = Signal(str, list)   # (kind, visible_keys in order)
    back    = Signal()

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(_EDITOR_QSS)
        self._kind = kind
        # key → header lookup populated each time the editor is loaded
        self._header_by_key: dict[str, str] = {}
        self._all_keys: list[str] = []
        self._build_ui()

    # Public: load / reload from provider state
    def load(self, all_keys: list[str], all_headers: list[str],
             visible_keys: list[str], *, title: str) -> None:
        self._all_keys = list(all_keys)
        self._header_by_key = dict(zip(all_keys, all_headers))
        self._title.setText(title)

        self._selected_list.clear()
        self._available_list.clear()

        visible = [k for k in visible_keys if k in self._header_by_key]
        available = [k for k in all_keys if k not in visible]
        for key in visible:
            self._add_selected_row(key)
        for key in available:
            self._add_available_row(key)

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(12)

        back = QLabel("\u25c0  Back to Column Management", self)
        back.setObjectName("backLink")
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.mousePressEvent = lambda _e: self.back.emit()  # type: ignore[assignment]
        root.addWidget(back)

        self._title = QLabel("", self)
        self._title.setObjectName("editorTitle")
        root.addWidget(self._title)

        sep = QFrame(self)
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {BORDER}; background: {BORDER}; max-height: 1px;")
        root.addWidget(sep)

        lists_row = QHBoxLayout()
        lists_row.setSpacing(16)
        lists_row.addWidget(self._build_selected_frame())
        lists_row.addWidget(self._build_available_frame())
        root.addLayout(lists_row, 1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        btns.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._on_apply)
        btns.rejected.connect(self.back.emit)
        root.addWidget(btns)

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
        self._available_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._available_list.setSpacing(1)
        lay.addWidget(self._available_list, 1)
        return frame

    # ── Row population / movement ───────────────────────────────────────────

    def _add_selected_row(self, key: str) -> None:
        w = _make_row_widget(self._header_by_key.get(key, key), "remove", self._selected_list)
        w._action_btn.clicked.connect(    # type: ignore[attr-defined]
            lambda _=False, k=key: self._move_to_available(k))
        it = QListWidgetItem(self._selected_list)
        it.setData(Qt.ItemDataRole.UserRole, key)
        it.setSizeHint(w.sizeHint())
        self._selected_list.addItem(it)
        self._selected_list.setItemWidget(it, w)

    def _add_available_row(self, key: str) -> None:
        w = _make_row_widget(self._header_by_key.get(key, key), "add", self._available_list)
        w._action_btn.clicked.connect(    # type: ignore[attr-defined]
            lambda _=False, k=key: self._move_to_selected(k))
        it = QListWidgetItem(self._available_list)
        it.setData(Qt.ItemDataRole.UserRole, key)
        it.setSizeHint(w.sizeHint())
        self._available_list.addItem(it)
        self._available_list.setItemWidget(it, w)

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

    # ── Apply ───────────────────────────────────────────────────────────────

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
        self.applied.emit(self._kind, self._current_selected_keys())
        self.back.emit()


# ═══════════════════════════════════════════════════════════════════════════════
#  Columns page – the thing slotted into PreferencesDialog's QStackedWidget
# ═══════════════════════════════════════════════════════════════════════════════

class ColumnsPage(QWidget):
    """Owns an internal stack: overview → flat editor / summary editor.

    ``navigate_to(subview)`` jumps to ``"overview"``, ``"flat"``, or
    ``"summary"`` and refreshes the editor from the provider.
    """

    def __init__(self, columns_provider: ColumnsProvider | None,
                 parent=None):
        super().__init__(parent)
        self._provider = columns_provider

        self._stack = QStackedWidget(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._stack)

        self._overview = _ColumnsOverview(
            flat_summary=self._summary_line("flat"),
            summary_summary=self._summary_line("summary"),
            parent=self,
        )
        self._overview.rowSelected.connect(self.navigate_to)
        self._stack.addWidget(self._overview)      # 0

        self._editor_flat = _ColumnsEditor("flat", self)
        self._editor_flat.applied.connect(self._on_applied)
        self._editor_flat.back.connect(lambda: self.navigate_to("overview"))
        self._stack.addWidget(self._editor_flat)   # 1

        self._editor_summary = _ColumnsEditor("summary", self)
        self._editor_summary.applied.connect(self._on_applied)
        self._editor_summary.back.connect(lambda: self.navigate_to("overview"))
        self._stack.addWidget(self._editor_summary)  # 2

    # ── Public API ──────────────────────────────────────────────────────────

    def navigate_to(self, subview: str) -> None:
        if subview == SUBVIEW_FLAT:
            self._load_editor(self._editor_flat, "flat", "Trades Columns")
            self._stack.setCurrentIndex(1)
        elif subview == SUBVIEW_SUMMARY:
            self._load_editor(self._editor_summary, "summary",
                              "Summary of Trades Columns")
            self._stack.setCurrentIndex(2)
        else:
            self._refresh_overview_subtitles()
            self._stack.setCurrentIndex(0)

    # ── Internals ───────────────────────────────────────────────────────────

    def _load_editor(self, editor: _ColumnsEditor, kind: str, title: str) -> None:
        if self._provider is None:
            editor.load([], [], [], title=title)
            return
        all_keys, all_headers, visible = self._provider.column_state(kind)
        editor.load(all_keys, all_headers, visible, title=title)

    def _on_applied(self, kind: str, keys: list[str]) -> None:
        if self._provider is not None:
            self._provider.apply_columns(kind, keys)

    def _summary_line(self, kind: str) -> str:
        """First handful of header names, comma-joined, ending in ``...``."""
        if self._provider is None:
            return ""
        _, headers, visible = self._provider.column_state(kind)
        # Title-case the headers since the overview uses readable casing
        key_to_header = {}
        all_keys, all_hdrs, _ = self._provider.column_state(kind)
        for k, h in zip(all_keys, all_hdrs):
            key_to_header[k] = h.title()
        names = [key_to_header.get(k, k).title() for k in visible][:7]
        return ", ".join(names) + (", \u2026" if len(visible) > len(names) else "")

    def _refresh_overview_subtitles(self) -> None:
        # Rebuild the overview so the subtitle text reflects the current
        # visible-keys set (changes apply immediately on Apply).
        self._stack.removeWidget(self._overview)
        self._overview.deleteLater()
        self._overview = _ColumnsOverview(
            flat_summary=self._summary_line("flat"),
            summary_summary=self._summary_line("summary"),
            parent=self,
        )
        self._overview.rowSelected.connect(self.navigate_to)
        self._stack.insertWidget(0, self._overview)
