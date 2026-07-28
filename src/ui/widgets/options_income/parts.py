"""
Options Income ticket — selector widgets.

* :class:`SymbolChipBar`  — preset symbol chips (with share counts), a
  type-to-search box (worker ``reqMatchingSymbols`` results arrive via
  ``set_search_results``), right-click chip removal, QSettings persistence.
* :class:`StrategyCards`  — the two mutually-exclusive CC / CSP cards with
  eligibility badges.  Ineligible cards stay visible, greyed, with the
  reason — never hidden.
* :class:`ExpirySelector` — next-four-weeklies chips plus a ``More ▾``
  dropdown for later expirations.

Pure view layer: all state changes surface as Qt signals.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.theme import Theme
from src.core.options_income import Expiry, StrategyStatus

log = logging.getLogger(__name__)

ACCENT_BLUE = "#0071e3"          # active-selection blue used app-wide
AMBER = "#e0a63a"                # ineligible badge
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


# ══════════════════════════════════════════════════════════════════════════════
#  SymbolChipBar
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_CHIPS = ["QQQ", "SPY", "NVDA", "TSLA", "NBIS"]
_SETTINGS_KEY = "OptionsTicket/symbolChips"


class SymbolChipBar(QWidget):
    """Chip row + search box (§4.1).

    Signals
    -------
    symbol_selected(str)          — a chip became active
    search_requested(int, str)    — debounced search text (tokened);
                                    host routes to the worker and calls
                                    :meth:`set_search_results`
    """

    symbol_selected = Signal(str)
    search_requested = Signal(int, str)

    def __init__(self, get_shares, parent=None):
        """*get_shares(symbol) -> float* — account share count for chips."""
        super().__init__(parent)
        self._get_shares = get_shares
        self._chips: list[str] = self._load_chips()
        self._active: str = self._chips[0] if self._chips else ""
        self._buttons: dict[str, QPushButton] = {}
        self._token = 0
        self._build_ui()
        self._rebuild_chips()

    # ── Persistence ─────────────────────────────────────────────────────────

    @staticmethod
    def _load_chips() -> list[str]:
        saved = QSettings("NanoPulse", "Dashboard").value(_SETTINGS_KEY)
        if isinstance(saved, str):
            saved = [s for s in saved.split(",") if s.strip()]
        if isinstance(saved, (list, tuple)) and saved:
            return [str(s).upper() for s in saved]
        return list(DEFAULT_CHIPS)

    def _save_chips(self) -> None:
        QSettings("NanoPulse", "Dashboard").setValue(
            _SETTINGS_KEY, ",".join(self._chips))

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

    def _rebuild_chips(self) -> None:
        while self._chip_row.count():
            item = self._chip_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._buttons.clear()
        for sym in self._chips:
            shares = float(self._get_shares(sym) or 0.0)
            count = (f"{int(shares):,} sh" if shares >= 1 else "no shares")
            btn = QPushButton(f"{sym}  {count}", self)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(_chip_style(sym == self._active))
            btn.clicked.connect(lambda _c=False, s=sym: self.select(s))
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda pos, s=sym, b=btn: self._chip_menu(s, b, pos))
            self._buttons[sym] = btn
            self._chip_row.addWidget(btn)

    def refresh_counts(self) -> None:
        """Re-render share counts (call after position data changes)."""
        self._rebuild_chips()

    # ── Chips API ───────────────────────────────────────────────────────────

    def active_symbol(self) -> str:
        return self._active

    def select(self, symbol: str) -> None:
        symbol = symbol.upper()
        if symbol not in self._chips:
            self._chips.append(symbol)
            self._save_chips()
        if symbol == self._active:
            self._rebuild_chips()
            return
        self._active = symbol
        self._rebuild_chips()
        self.symbol_selected.emit(symbol)

    def _chip_menu(self, symbol: str, btn: QPushButton, pos) -> None:
        menu = QMenu(self)
        remove = QAction(f"Remove {symbol}", menu)
        # Guard: never remove the last remaining chip.
        remove.setEnabled(len(self._chips) > 1)
        menu.addAction(remove)
        if menu.exec(btn.mapToGlobal(pos)) is remove and remove.isEnabled():
            self._chips.remove(symbol)
            self._save_chips()
            if symbol == self._active:
                # Removing the active chip selects the first remaining one
                # and reloads the ticket.
                self._active = self._chips[0]
                self._rebuild_chips()
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

class _StrategyCard(QFrame):
    clicked = Signal()

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(2)
        self._title = QLabel(title, self)
        self._detail = QLabel("", self)
        col.addWidget(self._title)
        col.addWidget(self._detail)
        lay.addLayout(col)
        lay.addStretch(1)
        self._badge = QLabel("", self)
        self._badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._badge)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self.clicked.emit()
        super().mousePressEvent(event)

    def apply(self, status: StrategyStatus, selected: bool) -> None:
        border = ACCENT_BLUE if selected else Theme.BORDER
        bg = "rgba(0,113,227,0.10)" if selected else "#1a1e2a"
        self.setStyleSheet(
            f"_StrategyCard, QFrame{{ background:{bg};"
            f" border:1px solid {border}; border-radius:8px; }}"
            "QLabel{ background:transparent; border:none; }")
        title_c = Theme.TEXT if status.eligible else Theme.TEXT_DIM
        self._title.setStyleSheet(
            f"color:{title_c}; font-size:12px; font-weight:bold;")
        self._detail.setStyleSheet(
            f"color:{Theme.TEXT_DIM}; font-size:10px;")
        self._detail.setText(status.detail)
        if status.eligible:
            badge_c, badge_bg = Theme.ACCENT, "rgba(63,185,80,0.12)"
        else:
            badge_c, badge_bg = AMBER, "rgba(224,166,58,0.12)"
        self._badge.setText(f"● {status.badge}")
        self._badge.setStyleSheet(
            f"color:{badge_c}; background:{badge_bg}; border:none;"
            " border-radius:8px; padding:3px 8px; font-size:9px;"
            " font-weight:bold; letter-spacing:0.5px;")


class StrategyCards(QWidget):
    """Two mutually-exclusive strategy cards (§4.2)."""

    strategy_changed = Signal(str)   # "CC" | "CSP"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected = "CC"
        self._status = {
            "CC": StrategyStatus(False, "", ""),
            "CSP": StrategyStatus(False, "", ""),
        }
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self._cc = _StrategyCard("Sell Covered Call", self)
        self._csp = _StrategyCard("Sell Cash-Secured Put", self)
        self._cc.clicked.connect(lambda: self._select("CC"))
        self._csp.clicked.connect(lambda: self._select("CSP"))
        row.addWidget(self._cc, stretch=1)
        row.addWidget(self._csp, stretch=1)
        self._restyle()

    def _select(self, key: str) -> None:
        if key == self._selected:
            return
        self._selected = key
        self._restyle()
        self.strategy_changed.emit(key)

    def selected(self) -> str:
        return self._selected

    def set_selected(self, key: str) -> None:
        if key in ("CC", "CSP") and key != self._selected:
            self._selected = key
            self._restyle()

    def set_status(self, cc: StrategyStatus, csp: StrategyStatus) -> None:
        self._status = {"CC": cc, "CSP": csp}
        self._restyle()

    def current_status(self) -> StrategyStatus:
        return self._status[self._selected]

    def _restyle(self) -> None:
        self._cc.apply(self._status["CC"], self._selected == "CC")
        self._csp.apply(self._status["CSP"], self._selected == "CSP")


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
