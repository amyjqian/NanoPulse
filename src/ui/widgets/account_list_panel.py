from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, QMimeData, QModelIndex, QRect, QTimer
from PySide6.QtGui import QDrag, QFont, QPainter, QPen, QColor, QPainterPath
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .accounts_model import (
    AccountsTableModel,
    COL_ACCOUNT,
    COL_ALIAS,
    COL_NET_LIQ,
    COL_TYPE,
)
from src.core import config_manager

log = logging.getLogger(__name__)

# ── Colours (matching order_entry_panel) ──────────────────────────────────────
BG_PANEL = "#1e2230"
BG_ITEM_ACTIVE = "#1a2040"
BG_ITEM_INACTIVE = "#242424"
BORDER_ACTIVE = "#2a3ab0"
BORDER_INACTIVE = "#333333"
BORDER = "#30363D"
TEXT = "#C9D1D9"
TEXT_DIM = "#8B949E"

CLR_FLATTEN = "#C2700A"
CLR_CLOSE = "#4a5160"
CLR_REVERSE = "#8957E5"
CLR_CANCEL = "#2b3140"

# Orange accent matches the Flatten button + trades control bar.
ACCENT = "#CC6600"
ACCENT_DIM = "#8A4500"

# Filter-strip chip palette — same blue-selected / grey-unselected
# pattern as the symbol chips in OrderEntryPanel so the two strips
# read as the same affordance.
_CHIP_SELECTED_BG    = "#0071e3"
_CHIP_SELECTED_HOVER = "#0077ed"
_CHIP_IDLE_BG        = "#3a3a4a"
_CHIP_IDLE_HOVER     = "#4a4a5a"


def _apply_filter_chip_style(btn: QPushButton, selected: bool) -> None:
    """Apply chip styling to a filter-strip button — blue when selected,
    grey otherwise.  Mirrors ``_apply_chip_style`` in order_entry_panel
    so the Accounts filter strip and the symbol chips look identical."""
    if selected:
        btn.setStyleSheet(
            "QPushButton{background:" + _CHIP_SELECTED_BG +
            ";color:white;border:none;border-radius:6px;"
            "padding:4px 12px;font-weight:bold;font-size:12px}"
            "QPushButton:hover{background:" + _CHIP_SELECTED_HOVER + "}")
    else:
        btn.setStyleSheet(
            "QPushButton{background:" + _CHIP_IDLE_BG +
            ";color:#ccc;border:none;border-radius:6px;"
            "padding:4px 12px;font-weight:600;font-size:12px}"
            "QPushButton:hover{background:" + _CHIP_IDLE_HOVER + "}")

_TITLE_FONT = QFont("Helvetica Neue, Arial", 11)

# Group-pill grid layout: 3 rows × 4 columns.  The first two slots
# (row 0, cols 0 and 1) are reserved for the built-in All / None
# quick-select pills, so user-defined groups get the remaining 10.
_GROUP_COLS = 4
_GROUP_ROWS = 3
_GROUP_DEFAULT_SLOTS = 2
_GROUP_MAX = _GROUP_COLS * _GROUP_ROWS - _GROUP_DEFAULT_SLOTS

# MIME type for drag-to-reorder of trading-group pills.
_GROUP_MIME = "application/x-nanopulse-group"

# Built-in quick-select groups rendered in the panel header.  Their
# names are reserved — user-defined groups may not shadow them.
# "All"  → every model account, every row checked.
# "None" → every model account, every row unchecked.
_DEFAULT_GROUPS: tuple[str, ...] = ("All", "None")
_TITLE_FONT.setWeight(QFont.Weight.Bold)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_btn(text: str, colour: str, min_h: int = 30) -> QPushButton:
    """Flat action button – similar to order_entry_panel but tuned
    tighter so the 2×3 action grid barely constrains the panel's
    minimum width.  Horizontal padding and font size are both pulled
    in step-by-step (orig 11/8, then 10/3, now 8/2) to let the
    AccountListPanel splitter drag further left without touching the
    button labels."""
    btn = QPushButton(text)
    btn.setMinimumHeight(min_h)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    btn.setStyleSheet(f"""
        QPushButton {{
            background: {colour};
            color: #ffffff;
            border: none;
            border-radius: 4px;
            padding: 4px 2px;
            font-weight: bold;
            font-size: 11px;
        }}
        QPushButton:hover {{ background: {colour}; border: 1px solid #ffffff30; }}
        QPushButton:pressed {{ background: {colour}; border: 1px solid #ffffff60; }}
    """)
    return btn


def _h_divider() -> QWidget:
    """Thin horizontal grey divider."""
    line = QWidget()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {BORDER};")
    return line


# ── Custom Checkbox (painted checkmark) ───────────────────────────────────────

class _CheckmarkBox(QWidget):
    """A custom-painted checkbox: unchecked = solid white square,
    checked = dark square with white checkmark."""

    toggled = Signal(bool)

    def __init__(self, checked: bool = True, parent=None):
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(22, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, v: bool) -> None:
        if self._checked != v:
            self._checked = v
            self.update()

    def mousePressEvent(self, event) -> None:
        self._checked = not self._checked
        self.update()
        self.toggled.emit(self._checked)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        sz = 18
        x = (self.width() - sz) // 2
        y = (self.height() - sz) // 2
        rect = QRect(x, y, sz, sz)
        radius = 4.0

        if self._checked:
            # Dark square background
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#1a1a2e"))
            p.drawRoundedRect(rect, radius, radius)

            # White checkmark
            pen = QPen(QColor("#ffffff"))
            pen.setWidth(2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)

            # Checkmark path: ✓ shape
            cx, cy = x + sz * 0.5, y + sz * 0.5
            path = QPainterPath()
            path.moveTo(x + sz * 0.22, cy + sz * 0.02)
            path.lineTo(x + sz * 0.42, cy + sz * 0.22)
            path.lineTo(x + sz * 0.78, cy - sz * 0.22)
            p.drawPath(path)
        else:
            # Solid white square with slight rounding
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#ffffff"))
            p.drawRoundedRect(rect, radius, radius)

        p.end()


# ── Account Row (Card) ────────────────────────────────────────────────────────

class _AccountRow(QFrame):
    """A single account card: checkbox | name | balance | × button."""

    toggled = Signal(str, bool)
    removed = Signal(str)

    def __init__(self, account: str, alias: str, balance: str,
                 acct_type: str, checked: bool = True, parent=None):
        super().__init__(parent)
        self._account = account
        self._checked = checked
        # Live TWS connection state for this row's account.  Defaults to
        # True so a brand-new row isn't dimmed before the first
        # ``set_connected()`` call.  Updated by AccountListPanel on
        # ``dataChanged`` from AccountsTableModel.
        self._connected: bool = True
        # Plain display name without the "⚠ " offline prefix; kept so
        # ``set_connected(True)`` can restore the original text.
        self._base_name: str = alias if alias else account
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 1, 8, 1)
        layout.setSpacing(10)

        # Custom painted checkbox
        self._cb = _CheckmarkBox(checked)
        self._cb.toggled.connect(self._on_toggle)
        layout.addWidget(self._cb)

        # Account name (bold white)
        self._name_lbl = QLabel(self._base_name)
        self._name_lbl.setFont(
            QFont("Helvetica Neue, Arial", 12, QFont.Weight.Bold))
        self._name_lbl.setStyleSheet(
            "color: #ffffff; background: transparent;")
        layout.addWidget(self._name_lbl, stretch=1)

        # Balance (right, light grey, monospace)
        self._balance_lbl = QLabel(self._format_balance(balance))
        self._balance_lbl.setFont(QFont("Menlo, Consolas, monospace", 11))
        self._balance_lbl.setStyleSheet(
            "color: #aaaaaa; background: transparent;")
        layout.addWidget(self._balance_lbl)

        # Remove (×) — flat, dark grey
        x_btn = QPushButton("\u00d7")
        x_btn.setFixedSize(22, 22)
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.setStyleSheet("""
            QPushButton {
                background: transparent; color: #666666;
                border: none; font-size: 15px; font-weight: bold;
            }
            QPushButton:hover { color: #F85149; }
        """)
        x_btn.clicked.connect(lambda: self.removed.emit(self._account))
        layout.addWidget(x_btn)

        self._apply_card_style()

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def account(self) -> str:
        return self._account

    @property
    def is_checked(self) -> bool:
        return self._checked

    def set_balance(self, balance: str) -> None:
        self._balance_lbl.setText(self._format_balance(balance))

    def set_name(self, alias: str) -> None:
        self._base_name = alias if alias else self._account
        self._refresh_name_label()

    def set_checked(self, checked: bool) -> None:
        self._checked = checked
        self._cb.setChecked(checked)
        self._apply_card_style()

    def set_connected(self, connected: bool) -> None:
        """Toggle the row's offline appearance.  When ``connected`` is
        False the name label is prefixed with ``⚠ ``, every label is
        dimmed, and a tooltip explains that orders will be skipped on
        this account.  When True, restores the live styling."""
        if self._connected == bool(connected):
            return
        self._connected = bool(connected)
        self._refresh_name_label()
        self._apply_card_style()
        if self._connected:
            self.setToolTip("")
        else:
            self.setToolTip(
                "Connection disconnected — orders will be skipped on this "
                "account until the connection is restored.")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _refresh_name_label(self) -> None:
        if self._connected:
            self._name_lbl.setText(self._base_name)
        else:
            self._name_lbl.setText(f"⚠ {self._base_name}")

    def _on_toggle(self, checked: bool) -> None:
        self._checked = checked
        self._apply_card_style()
        self.toggled.emit(self._account, checked)

    def _apply_card_style(self) -> None:
        if not self._connected:
            # Offline — desaturated frame and dim text on every label so
            # the row clearly looks inert, regardless of the checked
            # state.  Keeps borders distinguishable from idle rows.
            self.setStyleSheet(f"""
                _AccountRow {{
                    background-color: {BG_ITEM_INACTIVE};
                    border: 2px dashed #555555;
                    border-radius: 4px;
                }}
            """)
            self._name_lbl.setStyleSheet(
                "color: #777777; background: transparent;")
            self._balance_lbl.setStyleSheet(
                "color: #555555; background: transparent;")
            return
        # Connected — restore the standard label styling …
        self._name_lbl.setStyleSheet(
            "color: #ffffff; background: transparent;")
        self._balance_lbl.setStyleSheet(
            "color: #aaaaaa; background: transparent;")
        # … and the standard checked / unchecked card frame.
        if self._checked:
            self.setStyleSheet(f"""
                _AccountRow {{
                    background-color: {BG_ITEM_ACTIVE};
                    border: 2px solid {BORDER_ACTIVE};
                    border-radius: 4px;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                _AccountRow {{
                    background-color: {BG_ITEM_INACTIVE};
                    border: 2px solid {BORDER_INACTIVE};
                    border-radius: 4px;
                }}
            """)

    @staticmethod
    def _format_balance(raw: str) -> str:
        if not raw:
            return ""
        try:
            val = float(raw)
        except (ValueError, TypeError):
            return raw
        if abs(val) > 1e100:
            return "-"
        if abs(val) >= 1_000_000:
            return f"{val / 1_000_000:.1f}M"
        if abs(val) >= 1_000:
            return f"{val / 1_000:.0f}k"
        return f"{val:,.0f}"


# ── Group pill ────────────────────────────────────────────────────────────────

class _GroupPill(QPushButton):
    """Single named-group button in the filter strip.

    Left-click = activate group (emits ``activated``).
    Right-click = open a small context menu with Rename / Delete.
    Left-click + drag = reorder pills (invokes host-supplied ``on_drop``,
    mirroring the ``DraggableChip`` pattern in ``order_panel_parts.py``).

    Group membership auto-updates from individual checkbox toggles
    while the pill is active; there is no explicit "Update" action.
    """

    activated        = Signal(str)
    rename_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, name: str, parent=None, *, on_drop=None):
        super().__init__(name, parent)
        self._name = name
        self._active = False
        self._on_drop = on_drop
        self._press_pos = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.clicked.connect(lambda: self.activated.emit(self._name))
        self.setAcceptDrops(on_drop is not None)
        # Expand horizontally with the grid column so the pill scales
        # with the panel width rather than hugging its text.
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _apply_filter_chip_style(self, False)


    @property
    def group_name(self) -> str:
        return self._name

    def set_active(self, on: bool) -> None:
        if self._active == bool(on):
            return
        self._active = bool(on)
        _apply_filter_chip_style(self, self._active)

    def is_active(self) -> bool:
        return self._active

    def _show_menu(self, pos) -> None:
        menu = QMenu(self)
        rename_act = menu.addAction("Rename…")
        menu.addSeparator()
        delete_act = menu.addAction("Delete")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is rename_act:
            self.rename_requested.emit(self._name)
        elif chosen is delete_act:
            self.delete_requested.emit(self._name)

    # ── Drag-to-reorder ──────────────────────────────────────────────────────

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if (self._on_drop is not None
                and self._press_pos is not None
                and (event.buttons() & Qt.MouseButton.LeftButton)
                and (event.pos() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            self._press_pos = None
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_GROUP_MIME, self._name.encode("utf-8"))
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.MoveAction)
            return
        super().mouseMoveEvent(event)

    def dragEnterEvent(self, event):  # noqa: N802
        if (event.mimeData().hasFormat(_GROUP_MIME)
                and event.source() is not self):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802
        if (event.mimeData().hasFormat(_GROUP_MIME)
                and event.source() is not self):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        mime = event.mimeData()
        if not (mime.hasFormat(_GROUP_MIME) and event.source() is not self):
            super().dropEvent(event)
            return
        try:
            src = bytes(mime.data(_GROUP_MIME)).decode("utf-8")
        except UnicodeDecodeError:
            event.ignore()
            return
        event.acceptProposedAction()
        if self._on_drop is not None and src and src != self._name:
            self._on_drop(src, self._name)


# ── Main Panel ────────────────────────────────────────────────────────────────

class AccountListPanel(QWidget):
    """Live account list with action buttons – wired to AccountsTableModel."""

    # Emitted when an action button is clicked: (action_name, [account_ids])
    action_requested = Signal(str, list)
    # Emitted whenever the checked/selected set of accounts changes.
    # Payload: frozenset of currently-selected account ID strings.
    selection_changed = Signal(object)   # set[str]
    # Emitted when the user activates a named group (or None when the
    # current selection no longer matches any saved group).  Payload
    # is the group name string or None.
    active_group_changed = Signal(object)

    def __init__(self, accounts_model: AccountsTableModel, parent=None):
        super().__init__(parent)
        self._model = accounts_model
        self._account_rows: dict[str, _AccountRow] = {}
        self._unchecked: set[str] = set(
            config_manager.load_unchecked_accounts())
        self._hidden: set[str] = set(config_manager.load_hidden_accounts())
        # Named groups persisted to app_config.json.  List (not dict)
        # to preserve pill order in the filter strip.  Each group dict
        # is ``{name, accounts, selected}`` — ``accounts`` is the full
        # member set, ``selected`` is the subset currently checked
        # while the group is active.  Legacy configs lacking
        # ``selected`` are migrated to ``selected = accounts`` so the
        # prior "every member checked" behavior is preserved.
        self._groups: list[dict] = config_manager.load_account_groups()
        for _g in self._groups:
            if "selected" not in _g:
                _g["selected"] = list(_g.get("accounts", []))
        self._active_group: str | None = None
        self._group_pills: dict[str, _GroupPill] = {}
        # Built-in header pills.  Initialised during _setup_ui; the
        # ``is not None`` guards in _set_active_group cope with the
        # brief window before then.
        self._all_pill: QPushButton | None = None
        self._none_pill: QPushButton | None = None
        # One-shot flag: auto-activate the "Alph" trading group on
        # startup once the first batch of accounts has populated.
        # Not re-applied after the user switches away mid-session.
        self._default_group_applied: bool = False
        self._setup_ui()

        # React to model changes
        self._model.dataChanged.connect(self._on_model_data_changed)
        self._model.rowsInserted.connect(self._on_rows_changed)
        self._model.rowsRemoved.connect(self._on_rows_changed)

        self._sync_rows()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setObjectName("AccountListPanel")
        self.setStyleSheet(f"""
            QWidget#AccountListPanel {{
                background: {BG_PANEL};
                border: 1px solid {BORDER};
                border-radius: 4px;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # ── Header: title (left) + "+" button (right) ────────────────────────
        # "+" creates a new user-defined group seeded with every
        # account, all unchecked.  The built-in All / None quick-select
        # pills live in the first two slots of the 2×5 grid below.
        header = QHBoxLayout()
        header.setSpacing(4)

        title = QLabel("Account management")
        title.setFont(_TITLE_FONT)
        title.setStyleSheet("color: #ffffff; border: none;")
        header.addWidget(title)
        header.addStretch(1)

        self._add_group_btn = QPushButton("+")
        self._add_group_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_group_btn.setToolTip(
            "Create a new trading group seeded with every account "
            "(all unchecked).")
        _apply_filter_chip_style(self._add_group_btn, False)
        self._add_group_btn.clicked.connect(self._on_create_group)
        header.addWidget(self._add_group_btn)
        root.addLayout(header)

        # ── Trading-group chip grid: up to 2 rows × 5 chips ──────────────────
        self._filter_strip = self._build_filter_strip()
        root.addWidget(self._filter_strip)

        # ── Scrollable account rows ───────────────────────────────────────────
        self._rows_container = QWidget()
        self._rows_container.setStyleSheet(
            "background: transparent; border: none;")
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        self._rows_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(self._rows_container)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ background: transparent; width: 6px; }}
            QScrollBar::handle:vertical {{
                background: #3A3E48; border-radius: 3px; min-height: 20px;
            }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        """)
        root.addWidget(scroll, stretch=1)

        # ── Divider ───────────────────────────────────────────────────────────
        root.addWidget(_h_divider())

        # ── Action button grid ────────────────────────────────────────────────
        ag = QGridLayout()
        ag.setSpacing(4)
        ag.setContentsMargins(0, 4, 0, 0)

        for col, (txt, clr, act) in enumerate([
            ("25%",     CLR_CLOSE,   "close_25"),
            ("50%",     CLR_CLOSE,   "close_50"),
            ("Flatten", CLR_FLATTEN, "flatten"),
        ]):
            b = _make_btn(txt, clr)
            b.clicked.connect(lambda _, a=act: self._emit_action(a))
            ag.addWidget(b, 0, col)

        for col, (txt, clr, act) in enumerate([
            ("Reverse", CLR_REVERSE, "reverse"),
            ("75%",     CLR_CLOSE,   "close_75"),
            ("Cancel",  CLR_CANCEL,  "cancel_all"),
        ]):
            b = _make_btn(txt, clr)
            b.clicked.connect(lambda _, a=act: self._emit_action(a))
            ag.addWidget(b, 1, col)

        aw = QWidget()
        aw.setStyleSheet("border: none;")
        aw.setContentsMargins(0, 0, 0, 0)
        aw.setLayout(ag)
        root.addWidget(aw)

        # ── Divider ───────────────────────────────────────────────────────────
        root.addWidget(_h_divider())

        # ── Footer: Add Account dropdown ──────────────────────────────────────
        self._add_combo = QComboBox()
        self._add_combo.setPlaceholderText("+ Add Account")
        self._add_combo.setStyleSheet(f"""
            QComboBox {{
                background: {BG_ITEM_INACTIVE}; color: {TEXT};
                border: 1px solid {BORDER}; border-radius: 6px;
                padding: 2px 8px; font-size: 11px; min-height: 20px;
            }}
            QComboBox:hover {{ border-color: {TEXT_DIM}; }}
            QComboBox::drop-down {{
                subcontrol-origin: padding; subcontrol-position: center right;
                width: 24px; border: none;
            }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {TEXT_DIM};
                margin-right: 8px;
            }}
            QComboBox QAbstractItemView {{
                background: {BG_PANEL}; color: {TEXT};
                border: 1px solid {BORDER};
                selection-background-color: #253545;
                selection-color: {TEXT}; padding: 4px;
            }}
        """)
        self._add_combo.activated.connect(self._on_add_account)
        root.addWidget(self._add_combo)

    # ── Filter strip + group pills ────────────────────────────────────────────

    def _build_filter_strip(self) -> QWidget:
        """Grid container holding the trading-group chips.

        Layout (2 rows × 5 columns):
          * (0, 0) — built-in ``All``  pill
          * (0, 1) — built-in ``None`` pill
          * remaining 8 slots (row 0 cols 2–4, row 1 cols 0–4) — user-
            defined group pills.  User pills drag-to-reorder among
            themselves; the defaults always stay in their reserved
            corner and are not draggable.
        """
        strip = QWidget()
        strip.setObjectName("filterStrip")
        strip.setStyleSheet(
            "QWidget#filterStrip { background: transparent; border: none; }")

        grid = QGridLayout(strip)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        # Equal stretch on every column so the pills divide the
        # available width evenly and grow / shrink with the panel
        # instead of hugging their content size.
        for c in range(_GROUP_COLS):
            grid.setColumnStretch(c, 1)
        self._pills_layout = grid
        self._pills_container = strip

        # Built-in All / None pills occupy the first two slots of row 0.
        self._all_pill = QPushButton("All")
        self._all_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self._all_pill.setToolTip("Select every account.")
        self._all_pill.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _apply_filter_chip_style(self._all_pill, False)
        self._all_pill.clicked.connect(
            lambda: self._select_default_group("All"))
        grid.addWidget(self._all_pill, 0, 0)

        self._none_pill = QPushButton("None")
        self._none_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self._none_pill.setToolTip("Deselect every account.")
        self._none_pill.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _apply_filter_chip_style(self._none_pill, False)
        self._none_pill.clicked.connect(
            lambda: self._select_default_group("None"))
        grid.addWidget(self._none_pill, 0, 1)

        self._rebuild_pills()
        return strip

    def _rebuild_pills(self) -> None:
        """Clear and repopulate the user-group slots in the pill grid.
        The first two slots (row 0 cols 0 and 1) host the built-in
        All / None pills and are never touched by this method.  Only
        the first ``_GROUP_MAX`` user groups are rendered — creation is
        capped at that count so overflow is never visible here."""
        # Detach & delete existing user pills.  Defaults are untouched.
        for pill in self._group_pills.values():
            self._pills_layout.removeWidget(pill)
            pill.deleteLater()
        self._group_pills.clear()

        for i, group in enumerate(self._groups[:_GROUP_MAX]):
            slot = i + _GROUP_DEFAULT_SLOTS
            row = slot // _GROUP_COLS
            col = slot % _GROUP_COLS
            name = group["name"]
            pill = _GroupPill(
                name,
                self._pills_container,
                on_drop=self._on_group_drop,
            )
            pill.activated.connect(self._select_group)
            pill.rename_requested.connect(self._rename_group)
            pill.delete_requested.connect(self._delete_group)
            pill.set_active(name == self._active_group)
            self._pills_layout.addWidget(pill, row, col)
            self._group_pills[name] = pill

    def _on_group_drop(self, src_name: str, dst_name: str) -> None:
        """Pill ``src_name`` was dropped onto ``dst_name`` — reorder
        ``self._groups`` so ``src_name`` lands immediately before
        ``dst_name``, persist, and rebuild the grid.  Mirrors
        ``SymbolChipBar._on_drop``.
        """
        names = [g["name"] for g in self._groups]
        if src_name not in names or dst_name not in names:
            return
        src_idx = names.index(src_name)
        moved = self._groups.pop(src_idx)
        # Destination index must be computed AFTER the pop so the insert
        # lands immediately before the drop target in the new list.
        dst_idx = next(
            i for i, g in enumerate(self._groups) if g["name"] == dst_name)
        self._groups.insert(dst_idx, moved)
        config_manager.save_account_groups(self._groups)
        self._rebuild_pills()
        log.info("AccountList: reordered group %r before %r",
                 src_name, dst_name)

    def _set_active_group(self, name: str | None) -> None:
        """Update `_active_group`, refresh pill visual state, persist
        the new value so the next launch can restore it, and emit the
        signal.  Covers both user-defined pills and the built-in All /
        None pills."""
        if name == self._active_group:
            return
        self._active_group = name
        for pill_name, pill in self._group_pills.items():
            pill.set_active(pill_name == name)
        if self._all_pill is not None:
            _apply_filter_chip_style(self._all_pill, name == "All")
        if self._none_pill is not None:
            _apply_filter_chip_style(self._none_pill, name == "None")
        config_manager.save_last_account_group(name)
        self.active_group_changed.emit(name)

    def _find_group(self, name: str) -> dict | None:
        for g in self._groups:
            if g["name"] == name:
                return g
        return None

    # ── Group CRUD ────────────────────────────────────────────────────────────

    def _on_create_group(self) -> None:
        if len(self._groups) >= _GROUP_MAX:
            QMessageBox.information(
                self, "Group limit reached",
                f"The trading-group grid holds at most {_GROUP_MAX} "
                f"user-defined groups (the first two slots are reserved "
                "for the built-in All and None pills).  "
                "Delete an existing group before creating a new one.")
            return
        # Seed the new group with every account currently known to the
        # model.  The user then checks the ones they actually want
        # selected (or clicks × to drop an account from the group
        # entirely) — see ``_on_row_toggled`` and the × button for the
        # two kinds of mutation.
        all_accounts = sorted(
            rd[COL_ACCOUNT] for rd in self._model._table_data
            if rd[COL_ACCOUNT]
        )
        name = self._ask_group_name(
            "New account group",
            f"Name this group ({len(all_accounts)} account(s)):",
            "")
        if not name:
            return
        if name in _DEFAULT_GROUPS:
            QMessageBox.warning(
                self, "Reserved name",
                f"‘{name}’ is a built-in quick-select group — "
                "pick a different name.")
            return
        if self._find_group(name) is not None:
            QMessageBox.warning(self, "Name in use",
                                f"A group named ‘{name}’ already exists.")
            return
        # New group: every account is a member, none are selected.
        # The user then checks the ones they want (updates ``selected``)
        # and optionally clicks × to trim membership (``accounts``).
        self._groups.append(
            {"name": name, "accounts": all_accounts, "selected": []})
        config_manager.save_account_groups(self._groups)
        self._rebuild_pills()
        self._set_active_group(name)
        self._sync_rows()
        # _sync_rows only updates alias/balance on rows that already
        # exist — it doesn't touch their check state.  Apply the fresh
        # (all-unchecked) state explicitly so every visible row flips
        # in a single pass.
        for acct, r in self._account_rows.items():
            r.set_checked(self._is_account_checked(acct))
        self._emit_selection_changed()
        log.info("AccountList: created group %r seeded with %d account(s) "
                 "(all unchecked)", name, len(all_accounts))

    def _ask_group_name(self, title: str, label: str,
                        initial: str) -> str:
        """Prompt the user for a group name with a text-input dialog
        that is 80% of the Accounts panel width and centered over it.
        Returns the stripped name, or ``""`` on cancel / empty."""
        dlg = QInputDialog(self)
        dlg.setInputMode(QInputDialog.InputMode.TextInput)
        dlg.setWindowTitle(title)
        dlg.setLabelText(label)
        dlg.setTextValue(initial)
        dlg.setTextEchoMode(QLineEdit.EchoMode.Normal)

        width = max(220, int(self.width() * 0.8))

        # Defer the resize+center to the next event-loop tick so the
        # dialog's frame geometry (title bar, padding) is accurate.
        # Doing it before exec() puts the wrong number into move() on
        # macOS — the title bar offsets the visible center.
        def _center() -> None:
            dlg.resize(width, dlg.height())
            panel_center = self.mapToGlobal(self.rect().center())
            frame = dlg.frameGeometry()
            dlg.move(panel_center.x() - frame.width() // 2,
                     panel_center.y() - frame.height() // 2)
        QTimer.singleShot(0, _center)

        if dlg.exec() != QInputDialog.DialogCode.Accepted:
            return ""
        return dlg.textValue().strip()

    def _rename_group(self, old_name: str) -> None:
        if self._find_group(old_name) is None:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename group", "New name:",
            QLineEdit.EchoMode.Normal, old_name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == old_name:
            return
        if new_name in _DEFAULT_GROUPS:
            QMessageBox.warning(
                self, "Reserved name",
                f"‘{new_name}’ is a built-in quick-select group — "
                "pick a different name.")
            return
        if self._find_group(new_name) is not None:
            QMessageBox.warning(self, "Name in use",
                                f"A group named ‘{new_name}’ already exists.")
            return
        for g in self._groups:
            if g["name"] == old_name:
                g["name"] = new_name
                break
        if self._active_group == old_name:
            self._active_group = new_name
        config_manager.save_account_groups(self._groups)
        self._rebuild_pills()
        log.info("AccountList: renamed group %r -> %r", old_name, new_name)

    def _delete_group(self, name: str) -> None:
        if self._find_group(name) is None:
            return
        ans = QMessageBox.question(
            self, "Delete group",
            f"Delete the ‘{name}’ group? Account selections "
            "aren't changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if ans != QMessageBox.StandardButton.Yes:
            return
        was_active = self._active_group == name
        self._groups = [g for g in self._groups if g["name"] != name]
        config_manager.save_account_groups(self._groups)
        if was_active:
            # Selection itself is preserved; only the active-group
            # indicator clears.  Re-sync rows so the list falls back to
            # the all-accounts view (it was filtered to this group).
            self._set_active_group(None)
            self._sync_rows()
        self._rebuild_pills()
        log.info("AccountList: deleted group %r", name)

    def get_checked_accounts(self) -> set[str]:
        """Internal helper — a set of currently checked account IDs.
        Kept separate from the legacy ``get_selected_accounts`` which
        returns a list."""
        return {a for a, r in self._account_rows.items() if r.is_checked}

    # ── Model synchronisation ─────────────────────────────────────────────────

    def _sync_rows(self) -> None:
        """Rebuild / update account rows from the model.

        Visibility rules:
          * always hide accounts in ``self._hidden`` (global hide list)
          * when a trading group is active, show only that group's
            members — the list IS the group.  Scoped-delete via the ×
            button drops an account from the active group; the row
            disappears here because the account is no longer a member.
          * when no group is active, show every non-hidden account.
        """
        model_accounts: dict[str, dict] = {}
        for row_data in self._model._table_data:
            acct = row_data[COL_ACCOUNT]
            if acct:
                model_accounts[acct] = {
                    "alias": row_data[COL_ALIAS],
                    "balance": row_data[COL_NET_LIQ],
                    "type": row_data[COL_TYPE],
                }

        # Compute the set of accounts that should have a visible row.
        # Built-in All / None groups and the no-group view honor the
        # ``_hidden`` set (× hides the account from the default views).
        # User-defined groups ignore ``_hidden`` — an account that's
        # been hidden from All is still a member of any group that
        # includes it, and becomes visible again when the user
        # activates that group.
        if self._active_group in _DEFAULT_GROUPS:
            visible = {a for a in model_accounts if a not in self._hidden}
        elif self._active_group is not None:
            g = self._find_group(self._active_group)
            group_members = set(g.get("accounts", [])) if g else set()
            visible = {a for a in model_accounts if a in group_members}
        else:
            visible = {a for a in model_accounts if a not in self._hidden}

        # Add new / update existing.
        for acct in visible:
            info = model_accounts[acct]
            if acct not in self._account_rows:
                self._add_account_row(
                    acct, info["alias"], info["balance"], info["type"])
            else:
                r = self._account_rows[acct]
                r.set_name(info["alias"] if info["alias"] else acct)
                r.set_balance(info["balance"])

        # Remove rows whose account should no longer be visible (either
        # missing from the model entirely or filtered out by the active
        # group).
        for acct in [a for a in self._account_rows if a not in visible]:
            self._remove_account_row(acct)

        self._refresh_add_combo(model_accounts)
        self._reorder_rows()
        self._maybe_apply_default_group()

    def _maybe_apply_default_group(self) -> None:
        """One-shot: restore the last-active group on startup (or the
        built-in ``None`` pill on first launch) once the first batch
        of accounts populates the panel.  Subsequent sync calls are
        no-ops so the user is free to switch groups mid-session."""
        if self._default_group_applied:
            return
        if not self._account_rows:
            # Wait for the first non-empty population — accounts may
            # arrive a tick or two after the panel is constructed.
            return
        self._default_group_applied = True

        last = config_manager.load_last_account_group()
        if last in _DEFAULT_GROUPS:
            log.info("AccountList: restoring last-active default group %r",
                     last)
            self._select_default_group(last)
            return
        if last and self._find_group(last) is not None:
            log.info("AccountList: restoring last-active user group %r", last)
            self._select_group(last)
            return
        # First launch (or the saved name vanished): default to "None"
        # — every account visible, none selected.
        log.info("AccountList: first launch, applying default 'None' group")
        self._select_default_group("None")

    def _add_account_row(self, account: str, alias: str,
                         balance: str, acct_type: str) -> None:
        # Check state is resolved through _is_account_checked so each
        # context uses the right source of truth: user group's
        # ``selected`` list, built-in All / None semantics, or the
        # global ``_unchecked`` set when no group is active.
        checked = self._is_account_checked(account)
        row = _AccountRow(account, alias, balance, acct_type, checked=checked)
        row.toggled.connect(self._on_row_toggled)
        row.removed.connect(self._on_row_removed)
        # Apply the worker's current connection state so freshly added
        # rows for an offline label render dimmed from the start.
        row.set_connected(self._is_account_connected(account))
        idx = self._rows_layout.count() - 1   # insert before the stretch
        self._rows_layout.insertWidget(idx, row)
        self._account_rows[account] = row
        log.debug("AccountList: added row for %s (checked=%s)", account, checked)

    def _is_account_connected(self, account: str) -> bool:
        """Resolve the live TWS connection state for ``account`` via the
        shared model.  Defaults to True for accounts whose label hasn't
        reported a state yet (newly-added connection)."""
        label = self._model.connection_for_account(account)
        return self._model.is_label_connected(label)

    def _refresh_connected_states(self) -> None:
        """Push the current connection state of each visible row's worker
        back onto the row widget.  Called after ``dataChanged`` so flips
        from ``set_connection_status`` reach the UI in real time."""
        for account, row in self._account_rows.items():
            row.set_connected(self._is_account_connected(account))

    def _remove_account_row(self, account: str) -> None:
        row = self._account_rows.pop(account, None)
        if row:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
            log.debug("AccountList: removed row for %s", account)

    def _refresh_add_combo(self, current_accounts: dict | None = None) -> None:
        """Populate the Add Account dropdown with accounts NOT yet visible."""
        if current_accounts is None:
            current_accounts = {
                r[COL_ACCOUNT]: True
                for r in self._model._table_data if r[COL_ACCOUNT]
            }
        self._add_combo.blockSignals(True)
        self._add_combo.clear()
        for acct in current_accounts:
            if acct not in self._account_rows:
                alias = self._model._aliases.get(acct, "")
                label = f"{alias} ({acct})" if alias else acct
                self._add_combo.addItem(label, acct)
        self._add_combo.setCurrentIndex(-1)
        self._add_combo.blockSignals(False)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _on_model_data_changed(self, topLeft: QModelIndex,
                               bottomRight: QModelIndex, roles=None) -> None:
        net_liq_touched = False
        for row_idx in range(topLeft.row(), bottomRight.row() + 1):
            if row_idx >= len(self._model._table_data):
                continue
            rd = self._model._table_data[row_idx]
            r = self._account_rows.get(rd[COL_ACCOUNT])
            if r:
                r.set_balance(rd[COL_NET_LIQ])
                r.set_name(rd[COL_ALIAS] if rd[COL_ALIAS] else rd[COL_ACCOUNT])
                net_liq_touched = True
        # ``set_connection_status`` on the model emits ``dataChanged`` to
        # signal a label-wide flip; refresh every row's offline indicator
        # so the dim/⚠ state stays in sync with the worker's reality.
        self._refresh_connected_states()
        # Balances just changed — re-sort so the account with the most
        # money bubbles to the top (within its selected/unselected group).
        if net_liq_touched:
            self._reorder_rows()

    def _on_rows_changed(self, *args) -> None:
        self._sync_rows()
        self._refresh_connected_states()
        self._emit_selection_changed()

    def _on_row_toggled(self, account: str, checked: bool) -> None:
        log.debug("AccountList: %s toggled → %s (active=%s)",
                  account, checked, self._active_group)
        if (self._active_group is not None
                and self._active_group not in _DEFAULT_GROUPS):
            # Toggling within a user group updates that group's
            # ``selected`` subset and persists — membership
            # (``accounts``) is untouched, so the row stays visible.
            g = self._find_group(self._active_group)
            if g is not None:
                selected = list(g.get("selected", g.get("accounts", [])))
                if checked:
                    if account not in selected:
                        selected.append(account)
                else:
                    selected = [a for a in selected if a != account]
                g["selected"] = selected
                config_manager.save_account_groups(self._groups)
        else:
            # No user group active (or one of All / None) — fall back
            # to the global ``_unchecked`` tracking which survives
            # restarts via ``_persist_selection``.
            if checked:
                self._unchecked.discard(account)
            else:
                self._unchecked.add(account)
            self._persist_selection()
        self._emit_selection_changed()
        self._reorder_rows()

    def _on_row_removed(self, account: str) -> None:
        # When a user-defined group is active, the × button scopes to
        # that group: the account is taken out of the active group
        # only, every other group is left alone.  Virtual All / None
        # groups and the no-group view fall through to the "hide from
        # the default view" path: ``_hidden`` keeps the row out of
        # those views, but user groups still show the account when
        # activated (see ``_sync_rows``), so other groups are not
        # affected.
        if (self._active_group is not None
                and self._active_group not in _DEFAULT_GROUPS):
            self._remove_account_from_active_group(account)
            return

        self._remove_account_row(account)
        self._hidden.add(account)
        self._unchecked.discard(account)
        self._persist_selection()
        self._persist_hidden()
        self._refresh_add_combo()
        self._emit_selection_changed()
        log.info("AccountList: %s hidden from default views (groups untouched)",
                 account)

    def _remove_account_from_active_group(self, account: str) -> None:
        """Scoped delete: drop *account* from the currently-active group
        only, leaving every other saved group untouched.  The row is
        removed from the list because the active-group view shows only
        group members.  If the group becomes empty, delete it (matching
        prior behavior) and clear the active indicator so ``_sync_rows``
        falls back to the all-accounts view."""
        name = self._active_group
        g = self._find_group(name) if name else None
        if g is None:
            return
        if account not in g["accounts"]:
            return
        g["accounts"] = [a for a in g["accounts"] if a != account]
        # Keep ``selected`` consistent — a non-member can't be selected.
        if "selected" in g:
            g["selected"] = [a for a in g["selected"] if a != account]

        pills_need_rebuild = False
        if not g["accounts"]:
            # Empty group — drop it entirely.
            self._groups = [x for x in self._groups if x is not g]
            self._set_active_group(None)
            pills_need_rebuild = True

        config_manager.save_account_groups(self._groups)
        if pills_need_rebuild:
            self._rebuild_pills()

        # Re-sync rows: when the group still exists the deleted account
        # is no longer a member and its row is dropped; when the group
        # was deleted we fall back to the all-accounts view.
        self._sync_rows()
        self._emit_selection_changed()
        log.info("AccountList: removed %s from group %r (scoped delete)",
                 account, name)

    def _on_add_account(self, index: int) -> None:
        acct = self._add_combo.itemData(index)
        if not acct:
            return
        self._hidden.discard(acct)
        self._persist_hidden()

        # When a user group is active, adding an account re-joins it
        # as a checked member of that group.  Persisted alongside the
        # other group mutations so the change survives a restart.
        if (self._active_group is not None
                and self._active_group not in _DEFAULT_GROUPS):
            g = self._find_group(self._active_group)
            if g is not None:
                accounts = list(g.get("accounts", []))
                if acct not in accounts:
                    accounts.append(acct)
                    g["accounts"] = accounts
                selected = list(g.get("selected", []))
                if acct not in selected:
                    selected.append(acct)
                    g["selected"] = selected
                config_manager.save_account_groups(self._groups)
        else:
            # No user group active — make sure the freshly-visible row
            # starts checked by removing it from the global _unchecked.
            if acct in self._unchecked:
                self._unchecked.discard(acct)
                self._persist_selection()

        for rd in self._model._table_data:
            if rd[COL_ACCOUNT] == acct:
                self._add_account_row(
                    acct, rd[COL_ALIAS], rd[COL_NET_LIQ], rd[COL_TYPE])
                break
        self._refresh_add_combo()
        self._emit_selection_changed()
        self._reorder_rows()
        log.info("AccountList: user re-added %s", acct)

    def _is_account_checked(self, account: str) -> bool:
        """Resolve an account's checked state based on the active
        group:

          * ``"All"``  → every account is checked.
          * ``"None"`` → every account is unchecked.
          * user group → account is checked iff it's in ``selected``.
          * no active group → check state driven by the global
            ``_unchecked`` set.
        """
        if self._active_group == "All":
            return True
        if self._active_group == "None":
            return False
        if self._active_group is not None:
            g = self._find_group(self._active_group)
            if g is not None:
                return account in g.get("selected", g.get("accounts", []))
        return account not in self._unchecked

    def get_selected_count(self) -> int:
        """Return the number of currently checked (selected) account rows."""
        return sum(1 for r in self._account_rows.values() if r.is_checked)

    def get_selected_accounts(self) -> set[str]:
        """Return the set of currently checked account IDs."""
        return {a for a, r in self._account_rows.items() if r.is_checked}

    def _emit_selection_changed(self) -> None:
        """Broadcast the current selected-account set."""
        self.selection_changed.emit(self.get_selected_accounts())

    def active_group(self) -> str | None:
        """Currently-active group name, or None when the selection
        doesn't match any saved group."""
        return self._active_group

    def active_filter_label(self) -> str | None:
        """Top-bar chip label for the current selection state.

        Priority (most specific first):
          * active group name
          * ``"All"`` when every visible account is checked
          * ``"None"`` when nothing is checked
          * ``None`` for a hand-picked subset (the chip hides)
        """
        if self._active_group is not None:
            return self._active_group
        visible = set(self._account_rows.keys())
        if not visible:
            return None
        checked = self.get_checked_accounts()
        if checked == visible:
            return "All"
        if not checked:
            return "None"
        return None

    def _select_group(self, name: str) -> None:
        """Activate a saved group — the account list is filtered to
        that group's members (see ``_sync_rows``), and each row's
        checked state is restored from the group's ``selected`` list.
        Membership (``accounts``) and selection (``selected``) are
        both persisted per-group, so switching groups correctly
        restores per-group state.
        """
        g = self._find_group(name)
        if g is None:
            return
        # Update the active-group indicator before re-syncing rows so
        # _sync_rows filters to this group's members.
        self._set_active_group(name)
        self._sync_rows()
        # Push the group's saved ``selected`` subset onto the visible
        # rows so the user sees its per-group check state.
        for acct, r in self._account_rows.items():
            r.set_checked(self._is_account_checked(acct))
        self._emit_selection_changed()
        self._reorder_rows()
        log.info("AccountList: activated group %r (%d visible / %d selected)",
                 name, len(self._account_rows),
                 len(g.get("selected", g.get("accounts", []))))

    def _select_default_group(self, name: str) -> None:
        """Activate one of the built-in All / None pills in the header.

        * ``"All"``  — show every model account and check all of them
          (empty ``_unchecked``).
        * ``"None"`` — show every model account and uncheck all of them
          (``_unchecked`` = every account).

        These are virtual groups: nothing is added to ``self._groups``
        and they don't count against the user's 2×5 grid cap.  × on a
        row while one of these is active falls through to the classic
        global-hide path in ``_on_row_removed``.
        """
        if name not in _DEFAULT_GROUPS:
            return
        all_accounts = {rd[COL_ACCOUNT] for rd in self._model._table_data
                        if rd[COL_ACCOUNT]}
        if name == "All":
            self._unchecked = set()
        else:  # "None"
            self._unchecked = set(all_accounts)
        self._persist_selection()
        self._set_active_group(name)
        self._sync_rows()
        # _sync_rows only touches alias/balance on existing rows — push
        # the new checked state explicitly so every row flips.
        for acct, r in self._account_rows.items():
            r.set_checked(acct not in self._unchecked)
        self._emit_selection_changed()
        self._reorder_rows()
        log.info("AccountList: activated default group %r (%d account(s))",
                 name, len(all_accounts))

    # ── Row ordering ──────────────────────────────────────────────────────────

    # Priority buckets for the first-3-char prefix.  Lower integer →
    # higher priority → floats further up the list.  ``mgn`` is rank 0
    # (top), ``tfs`` rank 1, ``rsp`` and ``fhs`` share rank 2, and every
    # other prefix falls through to rank 3 where the usual alphabetical
    # tiebreak takes over.
    _PREFIX_PRIORITY: dict[str, int] = {
        "mgn": 0,
        "tfs": 1,
        "rsp": 2,
        "fhs": 2,
    }

    def _name_sort_key(self, account: str) -> tuple:
        """Sort key: priority-prefix buckets first (MGN > TFS > RSP/FHS
        > everything else), then alphabetical by first 3 chars of the
        display name, then by last 2 chars.  Display name = alias when
        set, else account id.  Comparison is case-insensitive so
        aliases like ``B-mgn0`` and ``b-MGN1`` land together.  Account
        id is the final tiebreaker to keep the sort deterministic when
        two accounts share both prefix and suffix."""
        alias = self._model._aliases.get(account, "") or ""
        name = alias if alias else account
        lower = name.lower()
        first3 = lower[:3]
        last2 = lower[-2:]
        priority = self._PREFIX_PRIORITY.get(first3, 3)
        return (priority, first3, last2, account)

    def _reorder_rows(self) -> None:
        """Re-arrange the account rows by display-name prefix priority
        (MGN > TFS > RSP/FHS > rest), then alphabetical.  Keeps the
        trailing ``addStretch`` entry at the end of the layout."""
        if not self._account_rows:
            return
        ordered = sorted(
            self._account_rows.items(),
            key=lambda kv: self._name_sort_key(kv[0]),
        )
        # Detach every row (without deleting) then re-insert in new order
        # just before the trailing addStretch slot.
        for _acct, row in ordered:
            self._rows_layout.removeWidget(row)
        # After removal the only child is the stretch (count() == 1).
        for i, (_acct, row) in enumerate(ordered):
            self._rows_layout.insertWidget(i, row)

    def _emit_action(self, action: str) -> None:
        selected = [a for a, r in self._account_rows.items() if r.is_checked]
        if action == "flatten":
            if not self._confirm_flatten(selected):
                return
        elif action == "reverse":
            if not self._confirm_reverse(selected):
                return
        log.info("AccountList: action '%s' on %d account(s): %s",
                 action, len(selected), selected)
        self.action_requested.emit(action, selected)

    def _confirm_flatten(self, accounts: list[str]) -> bool:
        """Show a styled confirmation dialog before flattening.

        Returns True if the user confirmed, False if they cancelled.
        """
        if not accounts:
            return True   # nothing to flatten; let the normal empty-accounts path handle it

        acct_list = "\n".join(f"  • {a}" for a in accounts)

        dlg = QMessageBox(self)
        dlg.setWindowTitle("Confirm Flatten")
        dlg.setIcon(QMessageBox.Icon.Warning)
        dlg.setText("<b>Flatten all positions?</b>")
        dlg.setInformativeText(
            f"This will cancel all open orders and close every open position "
            f"for the following account(s):\n\n{acct_list}\n\n"
            f"This action cannot be undone."
        )
        dlg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        dlg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dlg.button(QMessageBox.StandardButton.Yes).setText("Flatten")

        # Dark theme styling to match the rest of the app
        dlg.setStyleSheet("""
            QMessageBox {
                background-color: #1e2230;
                color: #C9D1D9;
            }
            QMessageBox QLabel {
                color: #C9D1D9;
                font-size: 12px;
            }
            QPushButton {
                min-width: 80px;
                min-height: 28px;
                border-radius: 4px;
                font-weight: bold;
                font-size: 11px;
                padding: 4px 12px;
                border: none;
            }
            QPushButton[text="Flatten"] {
                background: #cc6600;
                color: #ffffff;
            }
            QPushButton[text="Flatten"]:hover  { background: #d97a00; }
            QPushButton[text="Flatten"]:pressed { background: #b35800; }
            QPushButton[text="Cancel"] {
                background: #3A3A3C;
                color: #C9D1D9;
            }
            QPushButton[text="Cancel"]:hover  { background: #4a4a4e; }
            QPushButton[text="Cancel"]:pressed { background: #2a2a2c; }
        """)

        return dlg.exec() == QMessageBox.StandardButton.Yes

    def _confirm_reverse(self, accounts: list[str]) -> bool:
        """Show a styled confirmation dialog before reversing.

        Returns True if the user confirmed, False if they cancelled.
        """
        if not accounts:
            return True   # nothing to reverse; let the normal empty-accounts path handle it

        acct_list = "\n".join(f"  • {a}" for a in accounts)

        dlg = QMessageBox(self)
        dlg.setWindowTitle("Confirm Reverse")
        dlg.setIcon(QMessageBox.Icon.Warning)
        dlg.setText("<b>Reverse all positions?</b>")
        dlg.setInformativeText(
            f"This will cancel all open orders and flip every open position "
            f"(long → short, short → long) for the following account(s):\n\n"
            f"{acct_list}\n\n"
            f"This action cannot be undone."
        )
        dlg.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        dlg.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dlg.button(QMessageBox.StandardButton.Yes).setText("Reverse")

        dlg.setStyleSheet("""
            QMessageBox {
                background-color: #1e2230;
                color: #C9D1D9;
            }
            QMessageBox QLabel {
                color: #C9D1D9;
                font-size: 12px;
            }
            QPushButton {
                min-width: 80px;
                min-height: 28px;
                border-radius: 4px;
                font-weight: bold;
                font-size: 11px;
                padding: 4px 12px;
                border: none;
            }
            QPushButton[text="Reverse"] {
                background: #8E44AD;
                color: #ffffff;
            }
            QPushButton[text="Reverse"]:hover  { background: #9B59B6; }
            QPushButton[text="Reverse"]:pressed { background: #7D3C98; }
            QPushButton[text="Cancel"] {
                background: #3A3A3C;
                color: #C9D1D9;
            }
            QPushButton[text="Cancel"]:hover  { background: #4a4a4e; }
            QPushButton[text="Cancel"]:pressed { background: #2a2a2c; }
        """)

        return dlg.exec() == QMessageBox.StandardButton.Yes

    # ── Persistence helpers ───────────────────────────────────────────────────

    def _persist_selection(self) -> None:
        """Save unchecked accounts to config."""
        config_manager.save_unchecked_accounts(sorted(self._unchecked))

    def _persist_hidden(self) -> None:
        """Save hidden accounts to config."""
        config_manager.save_hidden_accounts(sorted(self._hidden))

    def get_selected_accounts(self) -> list[str]:
        """Return list of currently checked account IDs."""
        return [a for a, r in self._account_rows.items() if r.is_checked]
