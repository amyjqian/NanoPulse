from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, QModelIndex, QRect, QTimer
from PySide6.QtGui import QFont, QPainter, QPen, QColor, QPainterPath
from PySide6.QtWidgets import (
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

CLR_FLATTEN = "#cc6600"
CLR_CLOSE = "#8E8E93"
CLR_REVERSE = "#8E44AD"
CLR_CANCEL = "#3A3A3C"

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
_TITLE_FONT.setWeight(QFont.Weight.Bold)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_btn(text: str, colour: str, min_h: int = 30) -> QPushButton:
    """Flat action button – identical style to order_entry_panel."""
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
            padding: 4px 8px;
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
        display_name = alias if alias else account
        self._name_lbl = QLabel(display_name)
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
        self._name_lbl.setText(alias if alias else self._account)

    def set_checked(self, checked: bool) -> None:
        self._checked = checked
        self._cb.setChecked(checked)
        self._apply_card_style()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_toggle(self, checked: bool) -> None:
        self._checked = checked
        self._apply_card_style()
        self.toggled.emit(self._account, checked)

    def _apply_card_style(self) -> None:
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

    Group membership auto-updates from individual checkbox toggles
    while the pill is active; there is no explicit "Update" action.
    """

    activated        = Signal(str)
    rename_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, name: str, parent=None):
        super().__init__(name, parent)
        self._name = name
        self._active = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.clicked.connect(lambda: self.activated.emit(self._name))
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
        # to preserve pill order in the filter strip.
        self._groups: list[dict] = config_manager.load_account_groups()
        self._active_group: str | None = None
        # Suppresses the "manual toggle clears active group" behaviour
        # while we drive the checkbox state from _select_group itself.
        self._suspend_active_group_clear: bool = False
        self._group_pills: dict[str, _GroupPill] = {}
        # Built-in All / None chips — set during _build_filter_strip.
        self._all_chip: QPushButton | None = None
        self._none_chip: QPushButton | None = None
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
        # _sync_rows doesn't run through _emit_selection_changed in
        # this init path; refresh chips so the persisted check state
        # is reflected on first paint.
        self._refresh_builtin_chips()

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

        # ── Header: "Accounts" title only.  All / None / Mgn and the
        # group pills live in the filter strip below so long group
        # lists don't squeeze the title.
        header = QHBoxLayout()
        header.setSpacing(0)

        title = QLabel("Account management")
        title.setFont(_TITLE_FONT)
        title.setStyleSheet("color: #ffffff; border: none;")
        header.addWidget(title)
        header.addStretch(1)
        root.addLayout(header)

        # ── Filter strip: All / None / Mgn · [group pills] · + ────────────────
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
        """Container with [All] [None] · [group chips] · [+] .  Every
        button is rendered as a chip with the same blue-selected /
        grey-idle palette as the symbol chips in OrderEntryPanel.
        The `+` button stays pinned to the right; group chips scroll
        horizontally when they overflow the panel width."""
        strip = QWidget()
        strip.setObjectName("filterStrip")
        strip.setStyleSheet(f"""
            QWidget#filterStrip {{ background: transparent; border: none; }}
            QScrollArea#filterScroll {{
                background: transparent; border: none;
            }}
            QScrollArea#filterScroll > QWidget > QWidget {{
                background: transparent;
            }}
            QScrollBar:horizontal {{ background: transparent; height: 4px; }}
            QScrollBar::handle:horizontal {{
                background: #3A3E48; border-radius: 2px; min-width: 20px;
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0;
            }}
        """)

        lay = QHBoxLayout(strip)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # All / None — chip state mirrors the checked-account set:
        # ALL goes blue when every visible account is checked, NONE
        # goes blue when none are.  Updated centrally in
        # ``_refresh_builtin_chips``.
        self._all_chip = QPushButton("All")
        self._all_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        _apply_filter_chip_style(self._all_chip, False)
        self._all_chip.clicked.connect(self._select_all)
        lay.addWidget(self._all_chip)

        self._none_chip = QPushButton("None")
        self._none_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        _apply_filter_chip_style(self._none_chip, False)
        self._none_chip.clicked.connect(self._select_none)
        lay.addWidget(self._none_chip)

        # Group chips live in their own container so we can rebuild
        # them without touching All/None/+.
        self._pills_container = QWidget()
        self._pills_container.setStyleSheet(
            "QWidget { background: transparent; border: none; }")
        self._pills_layout = QHBoxLayout(self._pills_container)
        self._pills_layout.setContentsMargins(0, 0, 0, 0)
        self._pills_layout.setSpacing(4)

        scroll = QScrollArea()
        scroll.setObjectName("filterScroll")
        scroll.setWidget(self._pills_container)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Chip body = font 12 + padding 4×2 ≈ 24 px; reserve a bit more
        # so the optional 4-px scrollbar fits without cropping.
        scroll.setFixedHeight(32)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(scroll, stretch=1)

        plus = QPushButton("+")
        plus.setCursor(Qt.CursorShape.PointingHandCursor)
        plus.setToolTip(
            "Save the current checked-account selection as a named group.")
        _apply_filter_chip_style(plus, False)
        plus.clicked.connect(self._on_create_group)
        lay.addWidget(plus)

        self._rebuild_pills()
        return strip

    def _rebuild_pills(self) -> None:
        """Clear and repopulate the group-pill strip from self._groups.
        Called after every group mutation (create / rename / delete /
        update) and during initial setup."""
        # Detach & delete existing pills.
        for pill in self._group_pills.values():
            self._pills_layout.removeWidget(pill)
            pill.deleteLater()
        self._group_pills.clear()

        for group in self._groups:
            name = group["name"]
            pill = _GroupPill(name, self._pills_container)
            pill.activated.connect(self._select_group)
            pill.rename_requested.connect(self._rename_group)
            pill.delete_requested.connect(self._delete_group)
            pill.set_active(name == self._active_group)
            self._pills_layout.addWidget(pill)
            self._group_pills[name] = pill
        self._pills_layout.addStretch(1)

    def _set_active_group(self, name: str | None) -> None:
        """Update `_active_group`, refresh pill visual state, and emit
        the signal only when the value actually changes."""
        if name == self._active_group:
            return
        self._active_group = name
        for pill_name, pill in self._group_pills.items():
            pill.set_active(pill_name == name)
        self.active_group_changed.emit(name)

    def _find_group(self, name: str) -> dict | None:
        for g in self._groups:
            if g["name"] == name:
                return g
        return None

    # ── Group CRUD ────────────────────────────────────────────────────────────

    def _on_create_group(self) -> None:
        current = self.get_checked_accounts()
        if not current:
            QMessageBox.information(
                self, "No accounts selected",
                "Check at least one account first, then click "
                "‘+’ to save the selection as a group.")
            return
        name = self._ask_group_name(
            "New account group",
            f"Name this group ({len(current)} account(s)):",
            "")
        if not name:
            return
        if self._find_group(name) is not None:
            QMessageBox.warning(self, "Name in use",
                                f"A group named ‘{name}’ already exists.")
            return
        self._groups.append({"name": name, "accounts": sorted(current)})
        config_manager.save_account_groups(self._groups)
        self._rebuild_pills()
        self._set_active_group(name)
        log.info("AccountList: created group %r with %d account(s)",
                 name, len(current))

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
        self._groups = [g for g in self._groups if g["name"] != name]
        config_manager.save_account_groups(self._groups)
        if self._active_group == name:
            # Selection itself is preserved; only the active-group
            # indicator clears.
            self._set_active_group(None)
        self._rebuild_pills()
        log.info("AccountList: deleted group %r", name)

    def get_checked_accounts(self) -> set[str]:
        """Internal helper — a set of currently checked account IDs.
        Kept separate from the legacy ``get_selected_accounts`` which
        returns a list."""
        return {a for a, r in self._account_rows.items() if r.is_checked}

    # ── Model synchronisation ─────────────────────────────────────────────────

    def _sync_rows(self) -> None:
        """Rebuild / update account rows from the model."""
        model_accounts: dict[str, dict] = {}
        for row_data in self._model._table_data:
            acct = row_data[COL_ACCOUNT]
            if acct:
                model_accounts[acct] = {
                    "alias": row_data[COL_ALIAS],
                    "balance": row_data[COL_NET_LIQ],
                    "type": row_data[COL_TYPE],
                }

        # Add new / update existing — skip hidden accounts
        for acct, info in model_accounts.items():
            if acct in self._hidden:
                continue
            if acct not in self._account_rows:
                self._add_account_row(
                    acct, info["alias"], info["balance"], info["type"])
            else:
                r = self._account_rows[acct]
                r.set_name(info["alias"] if info["alias"] else acct)
                r.set_balance(info["balance"])

        # Remove stale
        for acct in [a for a in self._account_rows if a not in model_accounts]:
            self._remove_account_row(acct)

        self._refresh_add_combo(model_accounts)
        self._reorder_rows()
        self._maybe_apply_default_group()

    def _maybe_apply_default_group(self) -> None:
        """One-shot: auto-activate the ``Alph`` trading group on
        startup once the first batch of accounts populates the
        panel.  Match is case-insensitive so ``Alph`` / ``alph`` /
        ``ALPH`` all qualify.  Subsequent sync calls are no-ops so
        the user is free to switch groups mid-session."""
        if self._default_group_applied:
            return
        if not self._account_rows:
            # Wait for the first non-empty population — accounts may
            # arrive a tick or two after the panel is constructed.
            return
        self._default_group_applied = True
        target = next((g["name"] for g in self._groups
                       if g["name"].lower() == "alph"), None)
        if target is not None:
            log.info("AccountList: applying default group %r on startup",
                     target)
            self._select_group(target)

    def _add_account_row(self, account: str, alias: str,
                         balance: str, acct_type: str) -> None:
        # Active-group invariant: when a group is active, every
        # visible account's check state matches the group's
        # membership.  A new row arriving mid-session (e.g. an
        # account joins the worker after Alph was applied) must
        # honour that — otherwise the newcomer would default to
        # checked just because it wasn't in the persisted
        # _unchecked set, and the active group's selection would
        # silently grow.
        if self._active_group is not None:
            g = self._find_group(self._active_group)
            if g is not None:
                checked = account in g.get("accounts", [])
            else:
                checked = account not in self._unchecked
        else:
            checked = account not in self._unchecked
        row = _AccountRow(account, alias, balance, acct_type, checked=checked)
        row.toggled.connect(self._on_row_toggled)
        row.removed.connect(self._on_row_removed)
        idx = self._rows_layout.count() - 1   # insert before the stretch
        self._rows_layout.insertWidget(idx, row)
        self._account_rows[account] = row
        log.debug("AccountList: added row for %s (checked=%s)", account, checked)

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
        # Balances just changed — re-sort so the account with the most
        # money bubbles to the top (within its selected/unselected group).
        if net_liq_touched:
            self._reorder_rows()

    def _on_rows_changed(self, *args) -> None:
        self._sync_rows()
        self._emit_selection_changed()

    def _on_row_toggled(self, account: str, checked: bool) -> None:
        log.debug("AccountList: %s toggled → %s", account, checked)
        if checked:
            self._unchecked.discard(account)
        else:
            self._unchecked.add(account)
        self._persist_selection()
        # _select_group drives toggles itself; skip the user-edit path
        # so it doesn't loop / overwrite during programmatic activation.
        if not self._suspend_active_group_clear and self._active_group is not None:
            # Active-group auto-update: every check/uncheck rewrites
            # the active group's membership and persists immediately.
            # The active group is NEVER swapped for another group whose
            # membership happens to match — groups are independent
            # identities, two different groups may legitimately hold
            # the same set of accounts.
            current = sorted(self.get_checked_accounts())
            for g in self._groups:
                if g["name"] == self._active_group:
                    g["accounts"] = current
                    break
            config_manager.save_account_groups(self._groups)
        self._emit_selection_changed()
        self._reorder_rows()

    def _on_row_removed(self, account: str) -> None:
        self._remove_account_row(account)
        self._hidden.add(account)
        self._unchecked.discard(account)
        self._persist_selection()
        self._persist_hidden()
        # Cascade removal into every saved group; drop groups that
        # become empty so the pill strip doesn't carry useless entries.
        groups_changed = False
        for g in self._groups:
            if account in g["accounts"]:
                g["accounts"] = [a for a in g["accounts"] if a != account]
                groups_changed = True
        before = len(self._groups)
        self._groups = [g for g in self._groups if g["accounts"]]
        if before != len(self._groups):
            groups_changed = True
            # Active group may have been dropped — clear the indicator.
            if (self._active_group is not None
                    and self._find_group(self._active_group) is None):
                self._set_active_group(None)
        if groups_changed:
            config_manager.save_account_groups(self._groups)
            self._rebuild_pills()
        self._refresh_add_combo()
        self._emit_selection_changed()
        log.info("AccountList: user removed %s from visible list", account)

    def _on_add_account(self, index: int) -> None:
        acct = self._add_combo.itemData(index)
        if not acct:
            return
        self._hidden.discard(acct)
        self._persist_hidden()
        for rd in self._model._table_data:
            if rd[COL_ACCOUNT] == acct:
                self._add_account_row(
                    acct, rd[COL_ALIAS], rd[COL_NET_LIQ], rd[COL_TYPE])
                break
        self._refresh_add_combo()
        self._emit_selection_changed()
        self._reorder_rows()
        log.info("AccountList: user re-added %s", acct)

    def get_selected_count(self) -> int:
        """Return the number of currently checked (selected) account rows."""
        return sum(1 for r in self._account_rows.values() if r.is_checked)

    def get_selected_accounts(self) -> set[str]:
        """Return the set of currently checked account IDs."""
        return {a for a, r in self._account_rows.items() if r.is_checked}

    def _emit_selection_changed(self) -> None:
        """Broadcast the current selected-account set."""
        self._refresh_builtin_chips()
        self.selection_changed.emit(self.get_selected_accounts())

    def _refresh_builtin_chips(self) -> None:
        """Sync the All / None chip selected-state with the current
        checked-account set: All is blue when every visible account
        is checked, None is blue when none are, both grey otherwise."""
        if self._all_chip is None or self._none_chip is None:
            return
        visible = set(self._account_rows.keys())
        checked = self.get_checked_accounts()
        all_selected = bool(visible) and checked == visible
        none_selected = not checked
        # Edge case: zero visible accounts → checked is empty AND
        # equal to visible.  Pick None so we don't double-highlight.
        if all_selected and none_selected:
            all_selected = False
        _apply_filter_chip_style(self._all_chip, all_selected)
        _apply_filter_chip_style(self._none_chip, none_selected)

    def active_group(self) -> str | None:
        """Currently-active group name, or None when the selection
        doesn't match any saved group."""
        return self._active_group

    def _select_all(self) -> None:
        for r in self._account_rows.values():
            r.set_checked(True)
        self._unchecked.clear()
        self._persist_selection()
        # Pure selection action — never auto-promote into a saved
        # group whose membership happens to match.  Groups stay
        # independent identities; they don't get hijacked by All/None.
        self._set_active_group(None)
        self._emit_selection_changed()
        self._reorder_rows()

    def _select_none(self) -> None:
        for r in self._account_rows.values():
            r.set_checked(False)
        self._unchecked = set(self._account_rows.keys())
        self._persist_selection()
        self._set_active_group(None)
        self._emit_selection_changed()
        self._reorder_rows()

    def _select_group(self, name: str) -> None:
        """Activate a saved group — checks exactly its accounts, unchecks
        every other.  Skips accounts that no longer exist (deleted /
        hidden since the group was saved)."""
        g = self._find_group(name)
        if g is None:
            return
        wanted = set(g.get("accounts", []))
        new_unchecked: set[str] = set()
        self._suspend_active_group_clear = True
        try:
            for acct, r in self._account_rows.items():
                on = acct in wanted
                r.set_checked(on)
                if not on:
                    new_unchecked.add(acct)
        finally:
            self._suspend_active_group_clear = False
        self._unchecked = new_unchecked
        self._persist_selection()
        self._set_active_group(name)
        self._emit_selection_changed()
        self._reorder_rows()
        log.info("AccountList: activated group %r (%d/%d accounts)",
                 name, len(self.get_checked_accounts()), len(wanted))

    # ── Row ordering ──────────────────────────────────────────────────────────

    def _name_sort_key(self, account: str) -> tuple:
        """Sort key: MGN-prefix group pinned to top, then alphabetical
        by first 3 chars of the display name, then by last 2 chars.
        Display name = alias when set, else account id.  Comparison is
        case-insensitive so aliases like ``B-mgn0`` and ``b-MGN1`` land
        together.  Account id is the final tiebreaker to keep the sort
        deterministic when two accounts share both prefix and suffix."""
        alias = self._model._aliases.get(account, "") or ""
        name = alias if alias else account
        lower = name.lower()
        first3 = lower[:3]
        last2 = lower[-2:]
        # False sorts before True, so `not is_mgn` floats mgn rows up.
        is_mgn = first3 == "mgn"
        return (not is_mgn, first3, last2, account)

    def _reorder_rows(self) -> None:
        """Re-arrange the account rows by display-name prefix/suffix.
        MGN-prefixed accounts are pinned to the top regardless of
        check state.  Keeps the trailing ``addStretch`` entry at the
        end of the layout."""
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
