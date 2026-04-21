from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, QModelIndex, QRect
from PySide6.QtGui import QFont, QPainter, QPen, QColor, QPainterPath
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
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


# ── Main Panel ────────────────────────────────────────────────────────────────

class AccountListPanel(QWidget):
    """Live account list with action buttons – wired to AccountsTableModel."""

    # Emitted when an action button is clicked: (action_name, [account_ids])
    action_requested = Signal(str, list)
    # Emitted whenever the checked/selected set of accounts changes.
    # Payload: frozenset of currently-selected account ID strings.
    selection_changed = Signal(object)   # set[str]

    def __init__(self, accounts_model: AccountsTableModel, parent=None):
        super().__init__(parent)
        self._model = accounts_model
        self._account_rows: dict[str, _AccountRow] = {}
        self._unchecked: set[str] = set(
            config_manager.load_unchecked_accounts())
        self._hidden: set[str] = set(config_manager.load_hidden_accounts())
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

        # ── Header: "Accounts" + All / None ───────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(0)

        title = QLabel("Accounts")
        title.setFont(_TITLE_FONT)
        title.setStyleSheet("color: #ffffff; border: none;")
        header.addWidget(title)
        header.addStretch(1)

        for label_text, slot in [
            ("All", self._select_all),
            ("None", self._select_none),
            ("Mgn", self._select_mgn),
        ]:
            b = QPushButton(label_text)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {TEXT_DIM};
                    border: none; font-size: 11px; font-weight: 600;
                    padding: 2px 8px;
                }}
                QPushButton:hover {{ color: #ffffff; }}
            """)
            b.clicked.connect(slot)
            header.addWidget(b)

        root.addLayout(header)

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

    def _add_account_row(self, account: str, alias: str,
                         balance: str, acct_type: str) -> None:
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
        self._emit_selection_changed()
        self._reorder_rows()

    def _on_row_removed(self, account: str) -> None:
        self._remove_account_row(account)
        self._hidden.add(account)
        self._unchecked.discard(account)
        self._persist_selection()
        self._persist_hidden()
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
        self.selection_changed.emit(self.get_selected_accounts())

    def _select_all(self) -> None:
        for r in self._account_rows.values():
            r.set_checked(True)
        self._unchecked.clear()
        self._persist_selection()
        self._emit_selection_changed()
        self._reorder_rows()

    def _select_none(self) -> None:
        for r in self._account_rows.values():
            r.set_checked(False)
        self._unchecked = set(self._account_rows.keys())
        self._persist_selection()
        self._emit_selection_changed()
        self._reorder_rows()

    def _select_mgn(self) -> None:
        """Check every row whose alias (or account id, if no alias)
        contains ``mgn`` (case-insensitive); uncheck all others."""
        needle = "mgn"
        new_unchecked: set[str] = set()
        for acct, r in self._account_rows.items():
            alias = self._model._aliases.get(acct, "") or ""
            name = alias if alias else acct
            match = needle in name.lower()
            r.set_checked(match)
            if not match:
                new_unchecked.add(acct)
        self._unchecked = new_unchecked
        self._persist_selection()
        self._emit_selection_changed()
        self._reorder_rows()

    # ── Row ordering ──────────────────────────────────────────────────────────

    def _net_liq_for(self, account: str) -> float:
        """Parse the raw NetLiquidation string for *account* into a float;
        returns 0.0 when the value is missing or unparseable."""
        for rd in self._model._table_data:
            if rd[COL_ACCOUNT] == account:
                raw = rd[COL_NET_LIQ] or ""
                try:
                    return float(str(raw).replace(",", ""))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def _reorder_rows(self) -> None:
        """Re-arrange the account rows: selected accounts first, then by
        descending Net Liquidation, then alphabetically by account id as
        a tiebreaker.  Keeps the trailing ``addStretch`` entry at the end."""
        if not self._account_rows:
            return
        ordered = sorted(
            self._account_rows.items(),
            key=lambda kv: (
                not kv[1].is_checked,          # False (selected) sorts first
                -self._net_liq_for(kv[0]),     # larger balance first
                kv[0],                         # stable tiebreaker
            ),
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
