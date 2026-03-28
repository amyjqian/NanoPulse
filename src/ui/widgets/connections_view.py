from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QStyledItemDelegate,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core import config_manager
from src.core.ib_worker import IBWorkerThread
from .accounts_model import AccountsTableModel

log = logging.getLogger(__name__)

# ── Theme tokens ──────────────────────────────────────────────────────────────
BG_PRIMARY = "#1A1D24"
BG_SECONDARY = "#1E222A"
BG_HEADER = "#14171D"
BORDER = "#2A2E37"
TEXT = "#C9D1D9"
TEXT_DIM = "#8B949E"
GREEN = "#3FB950"
RED = "#F85149"
BLUE = "#58A6FF"

_TOOLBAR_BTN = """
    QPushButton {{
        background-color: {bg};
        color: {fg};
        border: 1px solid {border};
        border-radius: 4px;
        padding: 4px 14px;
        font-weight: 500;
        font-size: 12px;
    }}
    QPushButton:hover {{ background-color: {hover}; }}
    QPushButton:pressed {{ background-color: {pressed}; }}
"""

_CONNECT_SS = _TOOLBAR_BTN.format(bg="#1A3A2A", fg=GREEN, border="#2A4A3A", hover="#244A34", pressed="#1A3A2A")
_DISCONNECT_SS = _TOOLBAR_BTN.format(bg="#3A1A1A", fg=RED, border="#4A2A2A", hover="#4A2424", pressed="#3A1A1A")
_ADD_SS = _TOOLBAR_BTN.format(bg="#1A2440", fg=BLUE, border="#2A3450", hover="#24304A", pressed="#1A2440")
_DELETE_SS = _TOOLBAR_BTN.format(bg="#2A1A1A", fg=RED, border="#3A2A2A", hover="#3A2424", pressed="#2A1A1A")
_NEUTRAL_SS = _TOOLBAR_BTN.format(bg="#22262E", fg=TEXT, border=BORDER, hover="#2A2E38", pressed="#22262E")

# Connection config table columns
_CFG_COLS = ["Label", "Host", "Port", "Client ID", "Type", "Priority", "Auto Connect", "Status", "Action"]
_COL_LABEL = 0
_COL_HOST = 1
_COL_PORT = 2
_COL_CID = 3
_COL_TYPE = 4
_COL_PRIORITY = 5
_COL_AUTO = 6
_COL_STATUS = 7
_COL_ACTION = 8


# ── Shared dark stylesheet for QTableWidget / QTableView ──────────────────────
_TABLE_STYLE = f"""
    QTableWidget, QTableView {{
        background-color: {BG_PRIMARY};
        alternate-background-color: {BG_SECONDARY};
        color: {TEXT};
        gridline-color: {BORDER};
        border: 1px solid {BORDER};
        font-size: 12px;
        selection-background-color: #253545;
        selection-color: {TEXT};
    }}
    QHeaderView::section {{
        background-color: {BG_HEADER};
        color: {TEXT_DIM};
        border: none;
        border-bottom: 1px solid {BORDER};
        border-right: 1px solid {BORDER};
        padding: 5px 8px;
        font-weight: 600;
        font-size: 11px;
    }}
    QTableWidget::item, QTableView::item {{
        padding: 4px 8px;
    }}
    QTableWidget QLineEdit {{
        background-color: {BG_SECONDARY};
        color: {TEXT};
        border: 1px solid {BLUE};
        border-radius: 2px;
        padding: 2px 6px;
        selection-background-color: #253545;
        selection-color: {TEXT};
    }}
    QScrollBar:vertical {{
        background: {BG_PRIMARY};
        width: 8px;
    }}
    QScrollBar::handle:vertical {{
        background: #3A3E48;
        border-radius: 4px;
        min-height: 20px;
    }}
    QScrollBar:horizontal {{
        background: {BG_PRIMARY};
        height: 8px;
    }}
    QScrollBar::handle:horizontal {{
        background: #3A3E48;
        border-radius: 4px;
        min-width: 20px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        height: 0; width: 0;
    }}
"""


class _ToggleSwitch(QWidget):
    """iOS-style toggle switch with On/Off label."""

    toggled = Signal(bool)

    _TRACK_W = 36
    _TRACK_H = 20
    _KNOB_MARGIN = 3
    _LABEL_GAP = 6

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self._checked = checked
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_size()

    def _update_size(self) -> None:
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self.font())
        label_w = max(fm.horizontalAdvance("On"), fm.horizontalAdvance("Off"))
        total_w = label_w + self._LABEL_GAP + self._TRACK_W + 4
        self.setFixedSize(total_w, self._TRACK_H)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, on: bool) -> None:
        if self._checked != on:
            self._checked = on
            self.update()
            self.toggled.emit(on)

    def mousePressEvent(self, event) -> None:
        self.setChecked(not self._checked)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        h = self._TRACK_H
        tw = self._TRACK_W
        m = self._KNOB_MARGIN
        knob_d = h - m * 2

        # ── Label (left side) ─────────────────────────────────────────────
        label_text = "On" if self._checked else "Off"
        label_color = QColor(TEXT) if self._checked else QColor(TEXT_DIM)
        p.setPen(label_color)
        f = QFont("SF Pro Display, Helvetica Neue, Arial", 11)
        f.setWeight(QFont.Weight.Medium)
        p.setFont(f)
        fm = p.fontMetrics()
        label_x = 0
        label_y = (h + fm.ascent() - fm.descent()) // 2
        p.drawText(label_x, label_y, label_text)
        label_w = fm.horizontalAdvance(label_text)

        # ── Track (right side) ────────────────────────────────────────────
        track_x = label_w + self._LABEL_GAP
        r = h // 2

        track_color = QColor("#2F81F7") if self._checked else QColor("#3A3E48")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track_color))
        p.drawRoundedRect(track_x, 0, tw, h, r, r)

        # ── Knob ──────────────────────────────────────────────────────────
        knob_x = (track_x + tw - knob_d - m) if self._checked else (track_x + m)
        knob_y = m
        p.setBrush(QBrush(QColor("#FFFFFF")))
        p.drawEllipse(knob_x, knob_y, knob_d, knob_d)
        p.end()


class _CellComboBox(QComboBox):
    """QComboBox that drops its popup *below* the cell with a fixed min-width."""

    _MIN_POPUP_WIDTH = 110

    def showPopup(self) -> None:
        super().showPopup()
        popup = self.view().window()
        # Shift popup so it starts right below the combobox, not overlapping it.
        pos = self.mapToGlobal(self.rect().bottomLeft())
        w = max(popup.width(), self._MIN_POPUP_WIDTH)
        popup.setGeometry(pos.x(), pos.y() + 2, w, popup.height())


class ConnectionsView(QWidget):
    """
    Full Connections page.

    Parameters
    ----------
    accounts_model : AccountsTableModel
        Shared model owned by MainWindow so that all workers funnel into it.
    active_threads : dict[str, IBWorkerThread]
        Shared thread pool dict owned by MainWindow.
    """

    def __init__(
        self,
        accounts_model: AccountsTableModel,
        active_threads: dict[str, IBWorkerThread],
        parent=None,
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._model = accounts_model
        self._active_threads = active_threads
        self._setup_ui()
        self._load_config()

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        # Explicit background so the global QWidget rule doesn't bleed through
        self.setStyleSheet(f"ConnectionsView {{ background-color: {BG_PRIMARY}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.setHandleWidth(3)
        splitter.setChildrenCollapsible(False)
        splitter.setStyleSheet(f"""
            QSplitter {{
                background-color: {BG_PRIMARY};
            }}
            QSplitter::handle {{
                background-color: {BORDER};
            }}
        """)

        # ── Top pane: config table ────────────────────────────────────────────
        top_pane = QWidget()
        top_pane.setMinimumHeight(120)
        top_pane.setStyleSheet(f"QWidget {{ background-color: {BG_PRIMARY}; }}")
        top_layout = QVBoxLayout(top_pane)
        top_layout.setContentsMargins(12, 10, 12, 4)
        top_layout.setSpacing(6)

        # Section label
        lbl_conn = QLabel("Connections")
        lbl_conn.setFont(QFont("SF Pro Display, Helvetica Neue, Arial", 13, QFont.Weight.DemiBold))
        lbl_conn.setStyleSheet(f"QLabel {{ color: {TEXT}; background: transparent; padding: 0 0 2px 0; }}")
        top_layout.addWidget(lbl_conn)

        top_layout.addLayout(self._build_toolbar())
        self._cfg_table = self._build_config_table()
        top_layout.addWidget(self._cfg_table)

        # ── Bottom pane: accounts table view ──────────────────────────────────
        bottom_pane = QWidget()
        bottom_pane.setMinimumHeight(100)
        bottom_pane.setStyleSheet(f"QWidget {{ background-color: {BG_PRIMARY}; }}")
        bottom_layout = QVBoxLayout(bottom_pane)
        bottom_layout.setContentsMargins(12, 8, 12, 10)
        bottom_layout.setSpacing(6)

        # Section label
        lbl_acct = QLabel("Accounts")
        lbl_acct.setFont(QFont("SF Pro Display, Helvetica Neue, Arial", 13, QFont.Weight.DemiBold))
        lbl_acct.setStyleSheet(f"QLabel {{ color: {TEXT}; background: transparent; padding: 0 0 2px 0; }}")
        bottom_layout.addWidget(lbl_acct)

        self._accounts_view = QTableView(self)
        self._accounts_view.setModel(self._model)
        self._accounts_view.setAlternatingRowColors(True)
        self._accounts_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._accounts_view.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self._accounts_view.verticalHeader().setVisible(False)
        self._accounts_view.horizontalHeader().setStretchLastSection(True)
        self._accounts_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._accounts_view.setStyleSheet(_TABLE_STYLE)
        bottom_layout.addWidget(self._accounts_view)

        splitter.addWidget(top_pane)
        splitter.addWidget(bottom_pane)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)

        layout.addWidget(splitter)

        # When a connection row is selected, highlight its accounts below.
        self._cfg_table.itemSelectionChanged.connect(self._on_connection_selected)

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> QHBoxLayout:
        tb = QHBoxLayout()
        tb.setSpacing(6)

        btn_add = QPushButton("+ Add")
        btn_add.setStyleSheet(_ADD_SS)
        btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_add.clicked.connect(self._on_add_row)
        tb.addWidget(btn_add)

        btn_del = QPushButton("× Delete")
        btn_del.setStyleSheet(_DELETE_SS)
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.clicked.connect(self._on_delete_row)
        tb.addWidget(btn_del)

        btn_connect_all = QPushButton("Connect All")
        btn_connect_all.setStyleSheet(_CONNECT_SS)
        btn_connect_all.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_connect_all.clicked.connect(self._on_connect_all)
        tb.addWidget(btn_connect_all)

        btn_disconnect_all = QPushButton("Disconnect All")
        btn_disconnect_all.setStyleSheet(_DISCONNECT_SS)
        btn_disconnect_all.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_disconnect_all.clicked.connect(self._on_disconnect_all)
        tb.addWidget(btn_disconnect_all)

        tb.addStretch(1)
        return tb

    # ── Config table ──────────────────────────────────────────────────────────

    def _build_config_table(self) -> QTableWidget:
        t = QTableWidget(0, len(_CFG_COLS), self)
        t.setHorizontalHeaderLabels(_CFG_COLS)
        t.setAlternatingRowColors(True)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setStretchLastSection(True)

        # Column widths
        t.setColumnWidth(_COL_LABEL, 140)
        t.setColumnWidth(_COL_HOST, 130)
        t.setColumnWidth(_COL_PORT, 60)
        t.setColumnWidth(_COL_CID, 70)
        t.setColumnWidth(_COL_TYPE, 90)
        t.setColumnWidth(_COL_PRIORITY, 70)
        t.setColumnWidth(_COL_AUTO, 100)
        t.setColumnWidth(_COL_STATUS, 120)
        t.setColumnWidth(_COL_ACTION, 120)
        # Action column stretches (last)

        t.setStyleSheet(_TABLE_STYLE)

        # Auto-save on cell edits (only text cells fire this)
        t.cellChanged.connect(self._auto_save)
        return t

    # ── Row creation ──────────────────────────────────────────────────────────

    def _add_config_row(
        self,
        label: str = "",
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int | None = None,
        conn_type: str = "Paper",
        priority: int = 1,
        auto: bool = False,
    ) -> int:
        """Insert a new row into the config table and return its index."""
        if client_id is None:
            client_id = config_manager.get_next_client_id()

        self._cfg_table.blockSignals(True)  # prevent cellChanged during bulk set

        row = self._cfg_table.rowCount()
        self._cfg_table.insertRow(row)

        # Label
        self._cfg_table.setItem(row, _COL_LABEL, QTableWidgetItem(label))
        # Host
        self._cfg_table.setItem(row, _COL_HOST, QTableWidgetItem(host))
        # Port
        self._cfg_table.setItem(row, _COL_PORT, QTableWidgetItem(str(port)))
        # Client ID (read-only)
        cid_item = QTableWidgetItem(str(client_id))
        cid_item.setFlags(cid_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        cid_item.setForeground(QColor(TEXT_DIM))
        self._cfg_table.setItem(row, _COL_CID, cid_item)

        # Type dropdown (Paper / Live)
        type_combo = _CellComboBox()
        type_combo.addItems(["Paper", "Live"])
        type_combo.setCurrentText(conn_type if conn_type in ("Paper", "Live") else "Paper")

        # Force Qt-drawn items instead of native macOS rendering.
        type_combo.setItemDelegate(QStyledItemDelegate(type_combo))

        # Style the closed combo box.
        type_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {BG_SECONDARY};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 4px;
                padding: 3px 8px;
                font-size: 12px;
                min-width: 50px;
            }}
            QComboBox:hover {{
                border: 1px solid {BLUE};
            }}
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: center right;
                width: 20px;
                border: none;
            }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {TEXT_DIM};
                margin-right: 6px;
            }}
        """)

        # Style the popup view DIRECTLY — sub-selector stylesheets on QComboBox
        # are overridden by the parent QTableWidget stylesheet on macOS.
        popup_view = type_combo.view()
        popup_view.setStyleSheet(f"""
            QAbstractItemView {{
                background-color: {BG_SECONDARY};
                color: {TEXT};
                border: 1px solid {BORDER};
                padding: 4px 0;
                outline: 0;
                selection-background-color: #1A3A5C;
                selection-color: {TEXT};
            }}
            QAbstractItemView::item {{
                padding: 6px 12px;
                min-height: 22px;
            }}
            QAbstractItemView::item:hover,
            QAbstractItemView::item:selected {{
                background-color: #1A3A5C;
                color: {TEXT};
            }}
        """)
        # Also style the popup's container frame (the white border you see).
        popup_container = popup_view.window()
        popup_container.setWindowFlags(
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
        )
        popup_container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        popup_container.setStyleSheet(f"""
            QWidget {{
                background-color: {BG_SECONDARY};
                border: 1px solid {BORDER};
                border-radius: 4px;
            }}
        """)
        type_combo.currentTextChanged.connect(self._auto_save)
        self._cfg_table.setCellWidget(row, _COL_TYPE, type_combo)

        # Priority dropdown (1–4)
        pri_combo = _CellComboBox()
        pri_combo.addItems(["1", "2", "3", "4"])
        pri_combo.setCurrentText(str(priority) if priority in (1, 2, 3, 4) else "1")
        pri_combo.setItemDelegate(QStyledItemDelegate(pri_combo))
        pri_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {BG_SECONDARY};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 4px;
                padding: 3px 8px;
                font-size: 12px;
                min-width: 36px;
            }}
            QComboBox:hover {{
                border: 1px solid {BLUE};
            }}
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: center right;
                width: 20px;
                border: none;
            }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {TEXT_DIM};
                margin-right: 6px;
            }}
        """)
        pri_popup_view = pri_combo.view()
        pri_popup_view.setStyleSheet(f"""
            QAbstractItemView {{
                background-color: {BG_SECONDARY};
                color: {TEXT};
                border: 1px solid {BORDER};
                padding: 4px 0;
                outline: 0;
                selection-background-color: #1A3A5C;
                selection-color: {TEXT};
            }}
            QAbstractItemView::item {{
                padding: 6px 12px;
                min-height: 22px;
            }}
            QAbstractItemView::item:hover,
            QAbstractItemView::item:selected {{
                background-color: #1A3A5C;
                color: {TEXT};
            }}
        """)
        pri_popup_container = pri_popup_view.window()
        pri_popup_container.setWindowFlags(
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
        )
        pri_popup_container.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        pri_popup_container.setStyleSheet(f"""
            QWidget {{
                background-color: {BG_SECONDARY};
                border: 1px solid {BORDER};
                border-radius: 4px;
            }}
        """)
        pri_combo.currentTextChanged.connect(self._auto_save)
        self._cfg_table.setCellWidget(row, _COL_PRIORITY, pri_combo)

        # Auto Connect toggle switch (centered)
        toggle = _ToggleSwitch(checked=auto)
        toggle.toggled.connect(self._auto_save)
        container = QWidget()
        tgl_layout = QHBoxLayout(container)
        tgl_layout.addWidget(toggle)
        tgl_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tgl_layout.setContentsMargins(0, 0, 0, 0)
        self._cfg_table.setCellWidget(row, _COL_AUTO, container)

        # Action button (centered, same width as toolbar "Connect All")
        btn = QPushButton("Connect")
        btn.setStyleSheet(_CONNECT_SS)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFixedWidth(100)
        btn.clicked.connect(lambda checked, r=row: self._on_action_clicked(r))
        btn_container = QWidget()
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.addWidget(btn)
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        self._cfg_table.setCellWidget(row, _COL_ACTION, btn_container)

        # Status (read-only placeholder)
        status_item = QTableWidgetItem("")
        status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        status_item.setForeground(QColor(TEXT_DIM))
        self._cfg_table.setItem(row, _COL_STATUS, status_item)

        self._cfg_table.blockSignals(False)
        return row

    # ── Toolbar handlers ──────────────────────────────────────────────────────

    def _on_add_row(self) -> None:
        self._add_config_row()
        self._auto_save()
        log.info("Added new connection row (total=%d", self._cfg_table.rowCount())

    def _on_delete_row(self) -> None:
        row = self._cfg_table.currentRow()
        if row < 0:
            return
        label = self._row_label(row)
        log.info("Deleting connection row %d (label=%r)", row, label)

        # Block config-table signals during delete to prevent cellChanged
        # from _on_connection_status interfering mid-removal.
        self._cfg_table.blockSignals(True)

        # Disconnect if running
        if label and label in self._active_threads:
            self._disconnect_label(label)

        self._cfg_table.removeRow(row)
        self._cfg_table.blockSignals(False)

        # Re-index action button row properties
        self._reindex_action_buttons()
        self._auto_save()

    def _on_connect_all(self) -> None:
        log.info("Connect All requested (%d rows)", self._cfg_table.rowCount())
        for row in range(self._cfg_table.rowCount()):
            label = self._row_label(row)
            if label and label not in self._active_threads:
                self._connect_row(row)

    def _on_disconnect_all(self) -> None:
        log.info("Disconnect All requested (%d active)", len(self._active_threads))
        for row in range(self._cfg_table.rowCount()):
            label = self._row_label(row)
            if label and label in self._active_threads:
                self._disconnect_label(label)
                self._set_row_state(row, connected=False)

    # ── Per-row action button ─────────────────────────────────────────────────

    def _on_action_clicked(self, row: int) -> None:
        label = self._row_label(row)
        if not label:
            log.warning("Action clicked on row %d but label is empty – ignoring", row)
            return

        if label in self._active_threads:
            log.info("Disconnect requested for %r", label)
            self._disconnect_label(label)
            self._set_row_state(row, connected=False)
        else:
            self._connect_row(row)

    def _connect_row(self, row: int) -> None:
        label = self._row_label(row)
        host = self._row_text(row, _COL_HOST) or "127.0.0.1"
        try:
            port = int(self._row_text(row, _COL_PORT) or "7497")
        except ValueError:
            port = 7497
        try:
            client_id = int(self._row_text(row, _COL_CID) or "100")
        except ValueError:
            client_id = 100

        if not label:
            return

        worker = IBWorkerThread(
            connection_label=label,
            host=host,
            port=port,
            client_id=client_id,
        )
        # The Funnel: worker → shared model
        worker.summary_batch_updated.connect(self._model.update_batch)
        worker.connection_status_changed.connect(self._on_connection_status)
        self._active_threads[label] = worker
        worker.start()

        self._set_row_state(row, connected=True)
        log.info("Started worker for %s (%s:%d cid=%d)", label, host, port, client_id)

    def _disconnect_label(self, label: str) -> None:
        worker = self._active_threads.pop(label, None)
        if worker is None:
            return

        # Disconnect signals first so no stale updates arrive after row removal.
        try:
            worker.summary_batch_updated.disconnect(self._model.update_batch)
        except RuntimeError:
            pass
        try:
            worker.connection_status_changed.disconnect(self._on_connection_status)
        except RuntimeError:
            pass

        worker.stop()
        worker.wait(5000)

        # Flush any queued cross-thread signals so they don't fire later.
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()

        self._model.remove_rows_by_label(label)
        log.info("Stopped worker for %s", label)

    def _on_connection_status(self, label: str, connected: bool) -> None:
        """Update both Action button and Status cell for the matching row."""
        log.info("Connection status: %s → %s", label, "Connected" if connected else "Disconnected")
        for row in range(self._cfg_table.rowCount()):
            if self._row_label(row) == label:
                self._set_row_state(row, connected=connected)
                break

    # ── Connection ↔ Account highlight sync ───────────────────────────────────

    def _on_connection_selected(self) -> None:
        """When a connection row is selected, highlight its account rows below."""
        sel_model = self._accounts_view.selectionModel()
        sel_model.clearSelection()

        row = self._cfg_table.currentRow()
        if row < 0:
            return
        label = self._row_label(row)
        if not label:
            return

        # Find all account-model rows whose Connection column matches this label.
        from .accounts_model import COL_CONNECTION
        for acct_row, row_data in enumerate(self._model._table_data):
            if row_data[COL_CONNECTION] == label:
                idx = self._model.index(acct_row, 0)
                sel_model.select(
                    idx,
                    sel_model.SelectionFlag.Select | sel_model.SelectionFlag.Rows,
                )

    # ── Row state (Action + Status) ─────────────────────────────────────────────

    def _set_row_state(self, row: int, *, connected: bool) -> None:
        """Update both the Action button and Status cell for *row* in one place."""
        # Action button (inside a container widget)
        btn = self._get_action_button(row)
        if btn is not None:
            if connected:
                btn.setText("Disconnect")
                btn.setStyleSheet(_DISCONNECT_SS)
            else:
                btn.setText("Connect")
                btn.setStyleSheet(_CONNECT_SS)

        # Status cell
        status_item = self._cfg_table.item(row, _COL_STATUS)
        if status_item:
            if connected:
                status_item.setText("Connected")
                status_item.setForeground(QColor(GREEN))
            else:
                status_item.setText("Disconnected")
                status_item.setForeground(QColor(RED))

    def _reindex_action_buttons(self) -> None:
        """After row deletion, update each action button's captured row index."""
        for row in range(self._cfg_table.rowCount()):
            btn = self._get_action_button(row)
            if btn is not None:
                try:
                    btn.clicked.disconnect()
                except RuntimeError:
                    pass
                btn.clicked.connect(lambda checked, r=row: self._on_action_clicked(r))

    def _get_action_button(self, row: int) -> QPushButton | None:
        """Return the QPushButton inside the Action column's container widget."""
        container = self._cfg_table.cellWidget(row, _COL_ACTION)
        if container is None:
            return None
        for child in container.children():
            if isinstance(child, QPushButton):
                return child
        return None

    # ── Config persistence ────────────────────────────────────────────────────

    def _auto_save(self, *_args: Any) -> None:
        rows = self._collect_config_rows()
        config_manager.save_connections(rows)

    def _load_config(self) -> None:
        rows = config_manager.load_connections()
        log.info("Loading %d connection row(s) from config", len(rows))
        for r in rows:
            raw_port = r.get("port", 7497)
            raw_cid = r.get("client_id", "")
            try:
                port = int(raw_port) if str(raw_port).strip() else 7497
            except (ValueError, TypeError):
                port = 7497
            try:
                client_id = int(raw_cid) if str(raw_cid).strip() else config_manager.get_next_client_id()
            except (ValueError, TypeError):
                client_id = config_manager.get_next_client_id()
            self._add_config_row(
                label=r.get("label", ""),
                host=r.get("host", "127.0.0.1"),
                port=port,
                client_id=client_id,
                conn_type=r.get("type", "Paper"),
                priority=int(r.get("priority", 1)),
                auto=bool(r.get("auto", False)),
            )

    def _collect_config_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in range(self._cfg_table.rowCount()):
            auto_cb = self._get_auto_checkbox(row)
            type_combo = self._get_type_combo(row)
            pri_combo = self._get_priority_combo(row)
            rows.append({
                "label": self._row_text(row, _COL_LABEL),
                "host": self._row_text(row, _COL_HOST),
                "port": self._row_text(row, _COL_PORT),
                "client_id": self._row_text(row, _COL_CID),
                "type": type_combo.currentText() if type_combo else "Paper",
                "priority": int(pri_combo.currentText()) if pri_combo else 1,
                "auto": auto_cb.isChecked() if auto_cb else False,
            })
        return rows

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _row_label(self, row: int) -> str:
        return self._row_text(row, _COL_LABEL)

    def _row_text(self, row: int, col: int) -> str:
        item = self._cfg_table.item(row, col)
        return item.text().strip() if item else ""

    def _get_type_combo(self, row: int) -> QComboBox | None:
        widget = self._cfg_table.cellWidget(row, _COL_TYPE)
        return widget if isinstance(widget, QComboBox) else None

    def _get_priority_combo(self, row: int) -> QComboBox | None:
        widget = self._cfg_table.cellWidget(row, _COL_PRIORITY)
        return widget if isinstance(widget, QComboBox) else None

    def _get_auto_checkbox(self, row: int) -> _ToggleSwitch | None:
        container = self._cfg_table.cellWidget(row, _COL_AUTO)
        if container is None:
            return None
        for child in container.children():
            if isinstance(child, _ToggleSwitch):
                return child
        return None

    # ── Auto-connect on show ──────────────────────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # On first show, auto-connect rows with Auto checked
        if not hasattr(self, "_auto_connected"):
            self._auto_connected = True
            for row in range(self._cfg_table.rowCount()):
                cb = self._get_auto_checkbox(row)
                label = self._row_label(row)
                if cb and cb.isChecked() and label and label not in self._active_threads:
                    log.info("Auto-connecting %r on first show", label)
                    self._connect_row(row)

