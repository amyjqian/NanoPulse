"""
Options Income ticket — selector widgets.

* :class:`SymbolChipBar`  — preset symbol chips (with share counts), a
  type-to-search box (worker ``reqMatchingSymbols`` results arrive via
  ``set_search_results``), right-click chip removal, QSettings persistence.
* :class:`RightToggle`    — the Calls / Puts segmented toggle shown left
  of the EXPIRY chips (the order side lives on the rail's Buy / Sell
  toggle).
* :class:`ExpirySelector` — next-four-weeklies chips plus a ``More ▾``
  dropdown for later expirations.

Pure view layer: all state changes surface as Qt signals.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QWidget,
)

from src.ui.theme import Theme
from src.ui.widgets.order_panel_parts import DraggableChip
from src.core.options_income import Expiry

log = logging.getLogger(__name__)

ACCENT_BLUE = "#0071e3"          # active-selection blue used app-wide
_CHIP_QSS = (
    "QPushButton{{ background:{bg}; color:{fg};"
    " border:1px solid {border}; border-radius:6px;"
    " padding:5px 12px; font-size:11px; {extra} }}"
    "QPushButton:hover{{ border-color:#4A4E58; }}"
)


def _chip_style(active: bool) -> str:
    if active:
        return _CHIP_QSS.format(
            bg="rgba(0,113,227,0.16)", fg=Theme.TEXT,
            border=ACCENT_BLUE, extra="font-weight:bold;")
    return _CHIP_QSS.format(
        bg="#262a36", fg=Theme.TEXT_DIM, border=Theme.BORDER, extra="")


def _symbol_chip_style(active: bool) -> str:
    """Solid-blue active / grey inactive — matches the Book Trader and
    order-entry symbol chips (order_panel_parts._apply_chip_style).
    Duplicated verbatim by convention to avoid a cross-panel import."""
    if active:
        return ("QPushButton{background:#0071e3;color:white;border:none;"
                "border-radius:6px;padding:4px 12px;font-weight:bold;"
                "font-size:12px}"
                "QPushButton:hover{background:#0077ed}")
    return ("QPushButton{background:#3a3a4a;color:#ccc;border:none;"
            "border-radius:6px;padding:4px 12px;font-weight:600;"
            "font-size:12px}"
            "QPushButton:hover{background:#4a4a5a}")


# ══════════════════════════════════════════════════════════════════════════════
#  SymbolChipBar
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_CHIPS = ["QQQ", "SPY", "NVDA", "TSLA", "NBIS"]
_SETTINGS_KEY = "OptionsTicket/symbolChips"

_POS_CHIP_MAX = 5        # top-N account positions shown as chips
_POS_CHIP_MIN_SHARES = 100  # only CC-capable holdings become chips


class SymbolChipBar(QWidget):
    """Chip row + search box (§4.1).

    Two chip groups: the account's existing positions (top
    ``_POS_CHIP_MAX`` STK holdings with ≥ ``_POS_CHIP_MIN_SHARES``
    shares, by market value — derived, not persisted) and searched
    symbols (persisted to QSettings, appended to the right).  Chips are
    drag-reorderable like the Book Trader ladder chips.

    Signals
    -------
    symbol_selected(str)          — a chip became active
    search_requested(int, str)    — debounced search text (tokened);
                                    host routes to the worker and calls
                                    :meth:`set_search_results`
    """

    symbol_selected = Signal(str)
    search_requested = Signal(int, str)

    def __init__(self, get_shares, get_positions=None, parent=None):
        """*get_shares(symbol) -> float* — account share count for chips.
        *get_positions() -> list[dict]* — the account's STK positions
        (``symbol``, ``position``, ``market_value``)."""
        super().__init__(parent)
        self._get_shares = get_shares
        self._get_positions = get_positions or (lambda: [])
        self._custom: list[str] = self._load_chips()   # searched, persisted
        self._pos_syms: list[str] = []                 # derived positions
        self._order: list[str] = []                    # display order
        self._buttons: dict[str, QPushButton] = {}
        self._token = 0
        self._build_ui()
        self._sync_order()
        # No chip starts active — opening the ticket at account level
        # shows the bar unselected; the host selects a chip only when
        # it was launched with a specific symbol.
        self._active: str = ""
        self._rebuild_chips()

    # ── Persistence ─────────────────────────────────────────────────────────

    @staticmethod
    def _load_chips() -> list[str]:
        saved = QSettings("NanoPulse", "Dashboard").value(_SETTINGS_KEY)
        if isinstance(saved, str):
            saved = [s for s in saved.split(",") if s.strip()]
        if isinstance(saved, (list, tuple)) and saved:
            return [str(s).upper() for s in saved]
        return []

    def _save_chips(self) -> None:
        QSettings("NanoPulse", "Dashboard").setValue(
            _SETTINGS_KEY, ",".join(self._custom))

    # ── Chip groups ─────────────────────────────────────────────────────────

    def _position_symbols(self) -> list[str]:
        """Top account holdings that can back a covered call."""
        try:
            positions = list(self._get_positions() or [])
        except Exception:
            positions = []
        cands = []
        for p in positions:
            try:
                if float(p.get("position") or 0.0) < _POS_CHIP_MIN_SHARES:
                    continue
                sym = str(p.get("symbol") or "").upper()
                if not sym:
                    continue
                cands.append((abs(float(p.get("market_value") or 0.0)), sym))
            except (TypeError, ValueError):
                continue
        cands.sort(reverse=True)
        out: list[str] = []
        for _mv, sym in cands:
            if sym not in out:
                out.append(sym)
            if len(out) >= _POS_CHIP_MAX:
                break
        return out

    def _sync_order(self) -> None:
        """Refresh the display order: position chips keep the left block,
        searched chips sit to their right; user drag-order survives."""
        self._pos_syms = self._position_symbols()
        custom = [s for s in self._custom if s not in self._pos_syms]
        shown = set(self._pos_syms) | set(custom)
        order = [s for s in self._order if s in shown]
        for s in self._pos_syms:
            if s not in order:
                last = max((order.index(x) for x in self._pos_syms
                            if x in order), default=-1)
                order.insert(last + 1, s)
        for s in custom:
            if s not in order:
                order.append(s)
        self._order = order

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._chip_row = QHBoxLayout()
        self._chip_row.setSpacing(6)
        row.addLayout(self._chip_row)
        row.addStretch(1)

        self._search = QLineEdit(self)
        self._search.setPlaceholderText("Search symbol")
        self._search.setFixedWidth(180)
        self._search.setStyleSheet(
            f"QLineEdit{{ background:#262a36; color:{Theme.TEXT};"
            f" border:1px solid {Theme.BORDER}; border-radius:6px;"
            " padding:5px 10px; font-size:11px; }"
            f"QLineEdit:focus{{ border-color:{ACCENT_BLUE}; }}")
        self._search.textEdited.connect(self._on_search_edited)
        self._search.returnPressed.connect(self._pick_first_result)
        row.addWidget(self._search)

        # Results popup — child of the bar, floated under the search box.
        self._popup = QListWidget(self)
        self._popup.setWindowFlags(Qt.WindowType.ToolTip)
        self._popup.setStyleSheet(
            f"QListWidget{{ background:#262a36; color:{Theme.TEXT};"
            f" border:1px solid {Theme.BORDER}; border-radius:6px;"
            " font-size:11px; }"
            "QListWidget::item{ padding:5px 10px; }"
            f"QListWidget::item:selected{{ background:{ACCENT_BLUE};"
            " color:#ffffff; }")
        self._popup.itemClicked.connect(self._on_result_clicked)
        self._popup.hide()

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(350)
        self._debounce.timeout.connect(self._fire_search)

    def _chip_width(self) -> int:
        """Uniform chip width: 5-char ticker + space + 4-digit count,
        measured in the chip's bold 12-px font (+ the 12-px QSS padding
        each side)."""
        f = QFont(self.font())
        f.setPixelSize(12)
        f.setBold(True)
        return QFontMetrics(f).horizontalAdvance("XXXXX 8,888") + 24 + 4

    def _rebuild_chips(self) -> None:
        self._sync_order()
        while self._chip_row.count():
            item = self._chip_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._buttons.clear()
        width = self._chip_width()
        for sym in self._order:
            shares = float(self._get_shares(sym) or 0.0)
            # Chips with no position show just the ticker — a share-count
            # suffix only adds noise across a whole bar of flat symbols.
            label = f"{sym} {int(shares):,}" if shares >= 1 else sym
            # One drag group ("OPTINC") so any chip can be dropped onto
            # any other — same mechanics as the ladder chips.
            btn = DraggableChip(sym, "OPTINC", self._on_chip_drop, self)
            btn.setText(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(_symbol_chip_style(sym == self._active))
            # Fixed width keeps the bar tidy: every chip identical, and
            # the row can never crush a chip into clipping its label.
            btn.setFixedWidth(width)
            btn.clicked.connect(lambda _c=False, s=sym: self.select(s))
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda _pos, s=sym: self._remove_chip(s))
            self._buttons[sym] = btn
            self._chip_row.addWidget(btn)

    def refresh_counts(self) -> None:
        """Re-render chips (call after position data changes)."""
        self._rebuild_chips()

    # ── Chips API ───────────────────────────────────────────────────────────

    def active_symbol(self) -> str:
        return self._active

    def select(self, symbol: str) -> None:
        symbol = symbol.upper()
        self._sync_order()
        if symbol not in self._order and symbol not in self._custom:
            # Searched symbols become persistent chips on the right.
            self._custom.append(symbol)
            self._save_chips()
        if symbol == self._active:
            self._rebuild_chips()
            return
        self._active = symbol
        self._rebuild_chips()
        self.symbol_selected.emit(symbol)

    def _on_chip_drop(self, src: str, dst: str) -> None:
        """Ladder-style drag reorder: insert *src* at *dst*'s slot."""
        if (src == dst or src not in self._order
                or dst not in self._order):
            return
        self._order.remove(src)
        self._order.insert(self._order.index(dst), src)
        # Persist the searched chips' relative order (position chips
        # are derived per account, so their order is session-only).
        custom = set(self._custom)
        self._custom = [s for s in self._order if s in custom]
        self._save_chips()
        self._rebuild_chips()

    def _remove_chip(self, symbol: str) -> None:
        """Right-click removes a searched chip outright (ladder-chip
        behavior).  Position chips are derived from the account and
        stay; the last remaining chip also stays."""
        if symbol not in self._custom or len(self._order) <= 1:
            return
        self._custom.remove(symbol)
        self._save_chips()
        if symbol == self._active:
            # Removing the active chip selects the first remaining one
            # and reloads the ticket.
            self._sync_order()
            self._active = self._order[0] if self._order else ""
            self._rebuild_chips()
            if self._active:
                self.symbol_selected.emit(self._active)
        else:
            self._rebuild_chips()

    # ── Search ──────────────────────────────────────────────────────────────

    def _on_search_edited(self, text: str) -> None:
        if len(text.strip()) < 1:
            self._popup.hide()
            return
        self._debounce.start()

    def _fire_search(self) -> None:
        text = self._search.text().strip()
        if not text:
            return
        self._token += 1
        self.search_requested.emit(self._token, text)

    def set_search_results(self, token: int, results: list[dict]) -> None:
        """Worker search results.  Stale tokens are dropped; max 6 shown,
        each as ``TICKER · Company Name · position``."""
        if token != self._token or not self._search.hasFocus():
            return
        self._popup.clear()
        shown = 0
        for r in results:
            if shown >= 6:
                break
            sym = str(r.get("symbol", "")).upper()
            if not sym:
                continue
            shares = float(self._get_shares(sym) or 0.0)
            pos_txt = f"{int(shares):,} sh" if shares >= 1 else "no position"
            desc = r.get("description", "")
            item = QListWidgetItem(
                f"{sym} · {desc} · {pos_txt}" if desc
                else f"{sym} · {pos_txt}")
            item.setData(Qt.ItemDataRole.UserRole, sym)
            self._popup.addItem(item)
            shown += 1
        if not shown:
            self._popup.hide()
            return
        below = self._search.mapToGlobal(self._search.rect().bottomLeft())
        self._popup.setFixedWidth(max(260, self._search.width()))
        self._popup.setFixedHeight(min(6, shown) * 30 + 8)
        self._popup.move(below.x(), below.y() + 4)
        self._popup.setCurrentRow(0)
        self._popup.show()

    def _pick_first_result(self) -> None:
        if self._popup.isVisible() and self._popup.count():
            self._on_result_clicked(self._popup.item(0))

    def _on_result_clicked(self, item: QListWidgetItem) -> None:
        sym = str(item.data(Qt.ItemDataRole.UserRole) or "")
        self._popup.hide()
        self._search.clear()
        if sym:
            self.select(sym)   # appends as a chip + activates

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.key() == Qt.Key.Key_Escape and self._popup.isVisible():
            self._popup.hide()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._popup.hide()
        super().hideEvent(event)


# ══════════════════════════════════════════════════════════════════════════════
#  StrategyCards
# ══════════════════════════════════════════════════════════════════════════════

class RightToggle(QWidget):
    """Call / Put segmented toggle — sits left of the EXPIRY chips."""

    right_changed = Signal(str)   # "C" | "P"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._right = "C"
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self._btns: dict[str, QPushButton] = {}
        for key, label in (("C", "Call"), ("P", "Put")):
            b = QPushButton(label, self)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=key: self._pick(k))
            self._btns[key] = b
            row.addWidget(b)
        self._restyle()

    def right(self) -> str:
        return self._right

    def _pick(self, key: str) -> None:
        if key == self._right:
            return
        self._right = key
        self._restyle()
        self.right_changed.emit(key)

    def _restyle(self) -> None:
        for k, b in self._btns.items():
            b.setStyleSheet(_symbol_chip_style(k == self._right))


# ══════════════════════════════════════════════════════════════════════════════
#  ExpirySelector
# ══════════════════════════════════════════════════════════════════════════════

class ExpirySelector(QWidget):
    """Four weekly chips + a ``More ▾`` dropdown (§4.3)."""

    expiry_changed = Signal(str)   # YYYYMMDD

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chips: list[Expiry] = []
        self._more: list[Expiry] = []
        self._selected: Expiry | None = None
        self._buttons: list[QPushButton] = []

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        cap = QLabel("EXPIRY", self)
        cap.setStyleSheet(
            f"color:{Theme.TEXT_DIM}; font-size:10px; font-weight:bold;"
            " letter-spacing:1px;")
        row.addWidget(cap)
        self._chip_row = QHBoxLayout()
        self._chip_row.setSpacing(6)
        row.addLayout(self._chip_row)
        self._more_btn = QPushButton("More ▾", self)
        self._more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more_btn.clicked.connect(self._show_more_menu)
        row.addWidget(self._more_btn)
        row.addStretch(1)
        self._restyle()

    def reset_selection(self) -> None:
        """Forget the remembered expiry so the next :meth:`set_expiries`
        defaults to the nearest weekly chip (used on symbol change —
        the memory should only span reloads of the *same* symbol)."""
        self._selected = None

    def set_expiries(self, chips: list[Expiry], more: list[Expiry]) -> None:
        self._chips = list(chips)
        self._more = list(more)
        old = self._selected.ymd if self._selected else None
        self._selected = None
        for e in self._chips + self._more:
            if e.ymd == old:
                self._selected = e
                break
        if self._selected is None and self._chips:
            self._selected = self._chips[0]
        self._rebuild()

    def selected(self) -> Expiry | None:
        return self._selected

    def _rebuild(self) -> None:
        while self._chip_row.count():
            item = self._chip_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._buttons.clear()
        for e in self._chips:
            btn = QPushButton(self)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, x=e: self._pick(x))
            self._chip_row.addWidget(btn)
            self._buttons.append(btn)
        self._more_btn.setVisible(bool(self._more))
        self._restyle()

    def _pick(self, e: Expiry) -> None:
        if self._selected is not None and e.ymd == self._selected.ymd:
            return
        self._selected = e
        self._restyle()
        self.expiry_changed.emit(e.ymd)

    def _show_more_menu(self) -> None:
        if not self._more:
            return
        menu = QMenu(self)
        for e in self._more:
            act = QAction(e.long_label, menu)
            act.setCheckable(True)
            act.setChecked(self._selected is not None
                           and e.ymd == self._selected.ymd)
            act.triggered.connect(lambda _c=False, x=e: self._pick(x))
            menu.addAction(act)
        menu.exec(self._more_btn.mapToGlobal(
            self._more_btn.rect().bottomLeft()))

    def _restyle(self) -> None:
        sel = self._selected.ymd if self._selected else None
        for e, btn in zip(self._chips, self._buttons):
            active = e.ymd == sel
            fg_dte = ACCENT_BLUE if active else Theme.TEXT_DIM
            btn.setText(f"{e.chip_label} {e.dte}d")
            btn.setStyleSheet(_chip_style(active)
                              + f"QPushButton{{ color:{Theme.TEXT}; }}"
                              if active else _chip_style(False))
            _ = fg_dte  # dte tint folded into chip style
        # When a dropdown expiry is selected, surface it on the More button
        # so the selection stays visible.
        more_sel = (self._selected is not None
                    and self._selected.ymd not in
                    {c.ymd for c in self._chips})
        if more_sel:
            self._more_btn.setText(
                f"{self._selected.chip_label} {self._selected.dte}d ▾")
            self._more_btn.setStyleSheet(_chip_style(True))
        else:
            self._more_btn.setText("More ▾")
            self._more_btn.setStyleSheet(_chip_style(False))
