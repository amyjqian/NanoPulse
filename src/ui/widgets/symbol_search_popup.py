"""Searchable add-symbol popup with live IBKR symbol lookup.

Shared by Book Trader's ticker "+" button and OrderEntryPanel's chip-bar
"+" button.  Two result sources merge into one list:

  * **Known instruments** (chip / hot-button entries) — shown instantly,
    works with no TWS connection.
  * **Live IBKR matches** (``reqMatchingSymbols``) — as the user types,
    a debounced search runs on the highest-priority connected worker and
    STK matches stream in below the saved ones.  Picking one hands its
    qualified contract metadata to the host, which stores it on the new
    chip entry — the chip owns its contract identity.

Keyboard: Up/Down navigate, Enter adds the highlighted row, Esc closes
(Popup window flag also closes on outside click).
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime

from PySide6.QtCore import QEvent, QPoint, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.core import config_manager
from src.ui.theme import Theme

log = logging.getLogger(__name__)

# Debounce between the last keystroke and the reqMatchingSymbols call.
# IB rate-limits matching-symbol requests to ~1 per second.
_SEARCH_DEBOUNCE_MS = 500
_POPUP_WIDTH = 520
_AVATAR_PX = 30          # circular ticker monogram diameter
_VISIBLE_ROWS = 5        # dropdown shows at most this many rows
_CORNER_RADIUS = 14


def connected_workers_by_priority(active_threads: dict) -> list:
    """Connected IBWorkerThreads sorted by config priority (1 = highest)."""
    workers = []
    for label in config_manager.get_labels_by_priority():
        w = active_threads.get(label)
        if w is not None and w.isRunning() and w.is_ib_connected:
            workers.append(w)
    for _label, w in active_threads.items():
        if w not in workers and w.isRunning() and w.is_ib_connected:
            workers.append(w)
    return workers


# IB exchange codes → user-facing names.  IB still reports Nasdaq
# primary listings under its legacy ECN code "ISLAND".
_EXCHANGE_DISPLAY = {"ISLAND": "NASDAQ"}

# Only US / Canada listings are shown in the live results.  Listing
# currency is the cleanest country signal in the reqMatchingSymbols
# payload (NASDAQ/NYSE → USD, Toronto → CAD, Mexico → MXN, …).
_ALLOWED_CURRENCIES = frozenset({"USD", "CAD"})


def filter_stock_results(results: list[dict]) -> list[dict]:
    """Keep only US/Canada stock listings from a symbol-search batch."""
    return [
        r for r in results
        if r.get("sec_type") == "STK"
        and (r.get("currency") or "").upper() in _ALLOWED_CURRENCIES
    ]


# reqMatchingSymbols reports futures via their *underlying* (ES/NQ come
# back as an IND, CL/GC as a CMDTY) with "FUT" listed in the row's
# derivativeSecTypes.  Stock rows are excluded here on purpose — US
# single-stock futures are defunct and matching on them would add a
# noisy FUT twin under nearly every stock result.
_FUT_UNDERLYING_SECTYPES = frozenset({"IND", "CMDTY", "CONTFUT"})


def extract_future_results(results: list[dict]) -> list[dict]:
    """US futures roots from a symbol-search batch, as FUT-typed rows."""
    out: list[dict] = []
    seen: set[str] = set()
    for r in results:
        if (r.get("currency") or "").upper() != "USD":
            continue
        sec = r.get("sec_type", "")
        if sec != "FUT" and not (
                sec in _FUT_UNDERLYING_SECTYPES
                and "FUT" in (r.get("derivative_sec_types") or [])):
            continue
        sym = (r.get("symbol") or "").upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append({**r, "sec_type": "FUT"})
    return out


def _expiry_date(raw: str) -> date | None:
    """Parse an IB expiry ("YYYYMMDD" or contract month "YYYYMM") to a
    date — month-only values map to the month's last day so a contract
    stays selectable through its expiry month.  None when unparseable."""
    s = str(raw or "").strip()
    try:
        if len(s) >= 8:
            return datetime.strptime(s[:8], "%Y%m%d").date()
        if len(s) == 6:
            d = datetime.strptime(s, "%Y%m").date()
            return d.replace(day=calendar.monthrange(d.year, d.month)[1])
    except ValueError:
        pass
    return None


def format_expiry(raw: str) -> str:
    """Human-readable expiry for a results row ("Dec 26, 2025")."""
    s = str(raw or "").strip()
    try:
        if len(s) >= 8:
            return datetime.strptime(s[:8], "%Y%m%d").strftime("%b %d, %Y")
        if len(s) == 6:
            return datetime.strptime(s, "%Y%m").strftime("%b %Y")
    except ValueError:
        pass
    return s


def _venue_rank(r: dict) -> tuple:
    """Sort key for choosing ONE row per expiration.  Lower is better:
    IB's QBALGO algo-routing venue ranks last, and a row trading on its
    own primary listing venue beats one that doesn't."""
    exch = (r.get("exchange") or "").upper()
    primary = (r.get("primary_exchange") or "").upper()
    return (exch == "QBALGO",
            0 if (primary and exch == primary) else 1)


def pick_front_expiries(chain: list[dict], count: int = 2,
                        today: date | None = None) -> list[dict]:
    """The *count* nearest unexpired USD futures from a browse chain,
    sorted by expiry.  Rows that aren't USD FUTs or whose expiry can't
    be parsed are dropped.

    One row per expiration DATE: reqContractDetails lists the same
    expiry on several venues (e.g. ES on CME *and* QBALGO), and without
    dedup the two front slots would both be the same month — the user
    would see Sep twice and never Dec.  Within an expiry the primary
    venue wins (see :func:`_venue_rank`)."""
    today = today or date.today()
    best: dict[date, dict] = {}
    for r in chain:
        if r.get("sec_type") != "FUT":
            continue
        if (r.get("currency") or "").upper() != "USD":
            continue
        d = _expiry_date(r.get("expiry", ""))
        if d is None or d < today:
            continue
        cur = best.get(d)
        if cur is None or _venue_rank(r) < _venue_rank(cur):
            best[d] = r
    return [r for _d, r in sorted(best.items())[:count]]


def display_exchange(code: str) -> str:
    """User-facing name for an IB exchange code ("" stays "")."""
    code = (code or "").strip()
    return _EXCHANGE_DISPLAY.get(code.upper(), code)


def _match_rank(e: dict, q: str) -> int | None:
    """Relevance of row *e* for query *q* — lower is better:

    0. exact symbol match ("ES" → ES)
    1. symbol prefix match ("ES" → ESRX, ESLT)
    2. name/detail substring — but only for 3+ character queries, so a
       short query like "ES" can't drag in every "iShar**ES**" /
       "S**ERIES**" fund by name.
    ``None`` = no match (row is hidden).
    """
    sym = (e.get("symbol") or "").upper()
    if sym == q:
        return 0
    if sym.startswith(q):
        return 1
    if len(q) >= 3:
        hay = f"{e.get('name') or ''} {e.get('detail') or ''}".upper()
        if q in hay:
            return 2
    return None


def merge_rows(local_entries: list[dict], remote: list[dict],
               exclude: set[str], query: str
               ) -> tuple[list[dict], list[dict], list[str]]:
    """Merge saved-contract rows with live IBKR matches for display.

    Rows are relevance-ranked via :func:`_match_rank` — exact symbol
    matches first, then symbol prefixes, then name hits — and among
    equally-ranked remote rows futures lead (this is a trading app;
    typing "es" means the E-mini before Eversource Energy).

    Returns ``(locals_shown, remote_shown, already_added)``.  Remote
    rows are deduped against the shown locals by **conId** — never by
    bare symbol — so other-exchange listings of an already-saved stock
    (e.g. AMZN on TSE when NASDAQ AMZN is saved) still appear.  The
    symbol+sec_type fallback only kicks in for legacy local rows that
    carry no conid.  A remote row swallowed by that dedup backfills the
    matching local row's missing ``name`` / ``primary_exchange`` (as a
    copy — nothing is persisted) so contracts saved before those fields
    existed still display the company name and listing exchange.
    ``already_added`` lists chip-bar symbols matching *query* so the
    caller can explain why those rows are hidden.
    """
    q = query.strip().upper()
    if q:
        ranked = [(_match_rank(e, q), e) for e in local_entries]
        ranked = [(r, e) for r, e in ranked if r is not None]
        ranked.sort(key=lambda t: t[0])          # stable — keeps saved order
        locals_shown = [e for _r, e in ranked]
    else:
        locals_shown = list(local_entries)
    # Live futures roots always show their front expirations — even when
    # the root is already saved or on the chip bar — so the user can
    # switch the active expiration.  The saved per-expiry rows (and the
    # "already added" note) are superseded by that live expansion.
    remote_fut_syms = {r["symbol"].upper() for r in remote
                       if r.get("sec_type") == "FUT"}
    locals_shown = [e for e in locals_shown
                    if not (e.get("sec_type") == "FUT"
                            and e["symbol"].upper() in remote_fut_syms)]
    local_conids = {e["conid"] for e in locals_shown if e.get("conid")}
    local_keys = {(e["symbol"].upper(), e["sec_type"])
                  for e in locals_shown if not e.get("conid")}
    remote_shown: list[dict] = []
    dropped_by_conid: dict[str, dict] = {}
    dropped_by_key: dict[tuple[str, str], dict] = {}
    for r in remote:
        sym = r["symbol"].upper()
        if r.get("sec_type") == "FUT":
            remote_shown.append(r)
            continue
        if sym in exclude:
            continue
        cid = str(r.get("conid") or "")
        if cid and cid in local_conids:
            dropped_by_conid[cid] = r
            continue
        key = (sym, r.get("sec_type", ""))
        if key in local_keys:
            dropped_by_key[key] = r
            continue
        remote_shown.append(r)

    enriched: list[dict] = []
    for e in locals_shown:
        r = (dropped_by_conid.get(e.get("conid") or "")
             or dropped_by_key.get((e["symbol"].upper(), e["sec_type"])))
        if r is not None:
            e = dict(e)
            if not e.get("name"):
                e["name"] = r.get("name", "")
            if not e.get("primary_exchange"):
                e["primary_exchange"] = r.get("primary_exchange", "")
        enriched.append(e)

    # Relevance-order the remote rows: exact symbol > prefix > name hit
    # (IB's fuzzy matches without any visible relation rank last), and
    # futures before stocks within a rank.  Stable sort keeps IB's
    # relevance order inside each group.
    if q:
        remote_shown.sort(key=lambda r: (
            _match_rank(r, q) if _match_rank(r, q) is not None else 3,
            r.get("sec_type") != "FUT"))

    already_added = sorted(s for s in exclude
                           if q and s.startswith(q)
                           and s not in remote_fut_syms)
    return enriched, remote_shown, already_added


def _ask_future_action(parent, sym: str, old_desc: str, new_desc: str,
                       local: str) -> str:
    """Modal choice when picking an expiry that differs from the stored
    root contract.  Returns ``"replace"`` | ``"pin"`` | ``"cancel"``.
    Module-level so tests can monkeypatch the answer."""
    from PySide6.QtWidgets import QMessageBox
    box = QMessageBox(parent)
    box.setWindowTitle("Different expiration")
    box.setText(
        f"{sym} is currently {old_desc}.\n\n"
        f"Replace: roll {sym} to {new_desc} everywhere it is used —\n"
        f"the ladder, order entry and hot buttons.\n\n"
        f"Pin: keep {sym} on {old_desc} and add a separate {local} chip\n"
        f"frozen to {new_desc} (it never auto-rolls).")
    replace_btn = box.addButton(f"Replace {sym}",
                                QMessageBox.ButtonRole.YesRole)
    pin_btn = box.addButton(f"Add pinned {local}",
                            QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(replace_btn)
    box.exec()
    clicked = box.clickedButton()
    if clicked is replace_btn:
        return "replace"
    if clicked is pin_btn:
        return "pin"
    return "cancel"


def apply_future_pick(parent, entry: dict) -> tuple[str, bool]:
    """Apply a futures-expiration pick from the search popup.

    Returns ``(symbol_to_add, replaced)``:

    * root not stored yet            → write the pick's metadata onto
      any existing references (the new chip gets it via ``_add_symbol``)
    * picked contract already stored → heal-only, no change
    * root stored with a *different*
      expiration                     → ask (modal, parented to the host
      view):

      - **Replace** → re-point every root reference; returns
        ``(root, True)`` so hosts resubscribe.
      - **Pin** → nothing existing changes; returns the picked
        contract's LOCAL symbol (e.g. ``"NQZ6"``) as the symbol to add
        — an expiry-keyed chip frozen to that contract (same pattern as
        OPT chips keyed by their OCC symbol; never auto-rolls).
      - **Cancel** → ``("", False)``.
    """
    from src.core import instruments
    sym = (entry.get("symbol") or "").strip().upper()
    if not sym:
        return "", False
    stored = instruments.get_contract(sym)
    stored_conid = str(stored.get("conid", "") or "") if stored else ""
    if not stored_conid or stored_conid == str(entry.get("conid", "")):
        _upsert_entry_meta(sym, entry)   # first pick / heal — no prompt
        return sym, False

    old_desc = (format_expiry(stored.get("expiry", ""))
                or "another expiration")
    new_desc = format_expiry(entry.get("expiry", ""))
    local = (entry.get("local_symbol") or "").strip().upper() or sym
    action = _ask_future_action(parent, sym, old_desc, new_desc, local)
    if action == "replace":
        _upsert_entry_meta(sym, entry)   # re-point existing references
        log.info("SymbolSearch: replaced %s futures contract with expiry "
                 "%s", sym, entry.get("expiry", "?"))
        return sym, True
    if action == "pin":
        log.info("SymbolSearch: pinning %s (%s) as its own chip",
                 local, new_desc)
        return local, False
    return "", False


def _upsert_entry_meta(sym: str, entry: dict) -> None:
    """Write *entry*'s contract metadata onto every existing chip /
    hot-button reference of *sym* (rollover keeps them in sync; the
    newly-added chip gets its metadata from the host's ``_add_symbol``)."""
    from src.core import instruments
    meta = instruments.contract_meta_from_result(entry)
    if meta:
        config_manager.upsert_instrument(sym, meta)


def ensure_contract_saved(result: dict) -> tuple[str, dict | None]:
    """Resolve a search pick into chip metadata.

    Returns ``(symbol, meta)`` — *meta* is the normalised contract
    metadata for the picked row (the host stores it on the new chip via
    ``_add_symbol``), or ``None`` when the row carries no conid.
    Nothing is persisted here: the chip entry IS the store.
    """
    from src.core import instruments
    sym = (result.get("symbol") or "").strip().upper()
    if not sym:
        return "", None
    return sym, instruments.contract_meta_from_result(result)


class SymbolSearchPopup(QDialog):
    """Spotlight-style popup: search field on top, merged results below."""

    symbol_selected = Signal(dict)  # row dict incl. "symbol" + "source"

    def __init__(self, parent: QWidget, workers: list,
                 exclude: set[str] | None = None,
                 external_input=None,
                 stocks_only: bool = False) -> None:
        """*external_input* (a QLineEdit) switches the popup into
        dropdown mode: its own search box is hidden, the query is read
        live from that field, and the window never steals focus — the
        host keeps typing in its own field and forwards ↑/↓/Enter/Esc
        via :meth:`handle_key`.  The instance is reusable (``close()``
        hides instead of deleting).  *stocks_only* filters both saved
        contracts and IBKR results to STK rows (no futures roots, no
        option chips) — used where only a plain stock/ETF makes sense,
        e.g. the cash-sweep symbol pickers."""
        super().__init__(parent)
        self._workers = workers
        self._exclude = {s.upper() for s in (exclude or set())}
        self._ext_input = external_input
        self._stocks_only = bool(stocks_only)
        self._wired: list = []
        # Dropdown mode only: app-wide mouse-press watch (see
        # _install_outside_click_filter).  Tracked so install/remove
        # stay balanced across the popup's show → hide → show reuse.
        self._outside_filter_on = False

        # Remote-search state
        self._token = 0           # bumps on every debounce fire
        self._worker_idx = 0      # index into self._workers for retries
        self._searching = False   # a request is scheduled or in flight
        self._remote: list[dict] = []
        self._remote_ready = False
        self._error = ""

        # Futures chain state.  When a FUT root appears in the results
        # its month chain is fetched automatically (one reqContractDetails
        # at a time — browse signals carry no request token) and the root
        # expands into the two nearest expirations for the user to pick.
        self._fut_chains: dict[str, list[dict] | None] = {}  # None = failed
        self._chain_queue: list[str] = []
        self._chain_active: str | None = None
        self._chain_worker_idx = 0
        self._browse_wired: list = []

        self._local = self._collect_local()

        if self._ext_input is None:
            self.setWindowFlags(Qt.WindowType.Popup)
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        else:
            # Dropdown mode: a floating, non-activating tool window so
            # focus (and typing) stays in the host's own field.
            self.setWindowFlags(
                Qt.WindowType.Tool
                | Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # Truly rounded corners: the window is transparent and the card
        # is painted as a rounded rect in paintEvent — a stylesheet
        # radius alone leaves square window corners poking out.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(_POPUP_WIDTH)
        self.setStyleSheet("QDialog{background:transparent;border:none}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search symbol…")
        # No :focus accent — the green frame reads as a validation
        # state, and the box is focused whenever the popup is open
        # anyway, so the ring carries no information.
        self._search.setStyleSheet(
            f"QLineEdit{{background:{Theme.BG_INPUT};color:{Theme.TEXT};"
            f"border:1px solid {Theme.BORDER};border-radius:9px;"
            f"padding:8px 12px;font-size:14px}}")
        lay.addWidget(self._search)

        self._hdr = QLabel("Saved contracts")
        self._hdr.setStyleSheet(
            f"color:{Theme.TEXT_DIM};font-size:11px;padding:0 4px")
        lay.addWidget(self._hdr)

        self._lst = QListWidget()
        self._lst.setStyleSheet(
            f"QListWidget{{background:transparent;color:{Theme.TEXT};"
            f"border:none;outline:none}}"
            f"QListWidget::item{{border-radius:10px;padding:0px}}"
            # No ::item:selected rule: the keyboard-cursor highlight is
            # painted on the row widget (_update_row_highlight), which
            # renders identically in every context — ::item:selected
            # paints nothing when an ancestor (the Settings dialog)
            # carries a stylesheet.
            f"QListWidget::item:selected{{background:transparent}}"
            f"QListWidget::item:hover{{background:#232A3B}}")
        self._lst.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # No scrollbars — the list scrolls with the mouse wheel /
        # trackpad, and ↑/↓ step the selection (wired on the search box).
        self._lst.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._lst.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        from PySide6.QtWidgets import QAbstractItemView
        self._lst.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        lay.addWidget(self._lst)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._fire_search)

        self._search.textChanged.connect(self._on_text_changed)
        self._search.returnPressed.connect(self._commit_current)
        if self._ext_input is not None:
            # Dropdown mode: the host's field IS the search box.
            self._search.hide()
            self._ext_input.textChanged.connect(self._on_text_changed)
        self._lst.itemClicked.connect(lambda _it: self._commit_current())
        self._lst.currentRowChanged.connect(
            lambda _r: self._update_row_highlight())

        def _key(event) -> None:
            if event.key() == Qt.Key.Key_Down:
                self._step(+1)
                event.accept()
                return
            if event.key() == Qt.Key.Key_Up:
                self._step(-1)
                event.accept()
                return
            QLineEdit.keyPressEvent(self._search, event)
        self._search.keyPressEvent = _key  # type: ignore[method-assign]

        self._rebuild()
        # Dropdown mode is usually constructed lazily on the FIRST
        # typed character — that keystroke predates the textChanged
        # wiring above, so kick the search for it explicitly or the
        # list sits empty until the second character.
        if self._ext_input is not None and self._query():
            self._on_text_changed(self._query())

    def paintEvent(self, event) -> None:  # noqa: N802
        """Rounded card on a transparent window (see __init__)."""
        from PySide6.QtGui import QPainter, QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(Theme.BORDER), 1))
        p.setBrush(QColor(Theme.BG_PANEL))
        p.drawRoundedRect(
            self.rect().adjusted(0, 0, -1, -1),
            _CORNER_RADIUS, _CORNER_RADIUS)
        p.end()

    # ── Public ────────────────────────────────────────────────────────────────

    def attach_input(self, edit) -> None:
        """Dropdown mode: (re)point the live query at *edit*.  Used by
        hosts where several fields share one dropdown (the sweep-symbol
        pickers) — rewires the textChanged feed and kicks a fresh
        search for the new field's current text, so the results never
        belong to the previously-attached field."""
        if self._ext_input is edit:
            return
        if self._ext_input is not None:
            try:
                self._ext_input.textChanged.disconnect(self._on_text_changed)
            except (RuntimeError, TypeError):
                pass
        self._ext_input = edit
        edit.textChanged.connect(self._on_text_changed)
        self._on_text_changed(edit.text())

    def open_below(self, anchor: QWidget) -> None:
        """Show the popup anchored under *anchor*; standalone mode also
        focuses the internal search box (dropdown mode leaves focus in
        the host's field).  Dropdown mode matches the anchor's width
        (with a floor so rows stay legible) and tucks in flush under
        it, so field + list read as ONE combobox-style control."""
        if self._ext_input is not None:
            self.setFixedWidth(max(anchor.width(), 240))
        pos = anchor.mapToGlobal(anchor.rect().bottomLeft())
        if self._ext_input is not None:
            # Tuck 1px UNDER the field's bottom edge — any downward
            # offset reads as a detached second component.
            pos += QPoint(0, -1)
        self.move(pos)
        self.show()
        # A reused (hidden) Tool window can re-show BELOW its modal
        # host on macOS — force it back to the top of the stack.
        self.raise_()
        if self._ext_input is None:
            self._search.setFocus()
        else:
            self._install_outside_click_filter()

    # ── Dropdown mode: dismiss on a click anywhere else ───────────────────────

    def _install_outside_click_filter(self) -> None:
        """Watch application-wide mouse presses while the dropdown is up.

        A ``Qt.Popup`` window grabs the mouse and self-dismisses on an
        outside click, but dropdown mode is a non-activating ``Qt.Tool``
        window (so typing stays in the host's field) and gets no such
        grab.  The host's FocusOut hook only fires when the click lands
        on another focusable widget — clicking blank dialog background,
        a label, or the page header left the list hanging open."""
        app = QApplication.instance()
        if app is not None and not self._outside_filter_on:
            app.installEventFilter(self)
            self._outside_filter_on = True

    def _remove_outside_click_filter(self) -> None:
        app = QApplication.instance()
        if app is not None and self._outside_filter_on:
            app.removeEventFilter(self)
        self._outside_filter_on = False

    def eventFilter(self, obj, event):  # noqa: N802 – Qt override
        if (self._outside_filter_on
                and event.type() == QEvent.Type.MouseButtonPress
                and self.isVisible()):
            try:
                gp = event.globalPosition().toPoint()
            except AttributeError:      # non-mouse event on this type
                gp = None
            if gp is not None and not self._contains_global(gp):
                self.close()
        return super().eventFilter(obj, event)

    def _contains_global(self, gp) -> bool:
        """True when *gp* (screen coords) is over the dropdown itself or
        over the field it is attached to.  The attached field is exempt
        because the host reopens the dropdown on its mouse press — this
        filter runs first, so closing here would make a click on the
        field flicker the list shut and straight back open."""
        if self.frameGeometry().contains(gp):
            return True
        edit = self._ext_input
        if edit is not None:
            try:
                if edit.rect().contains(edit.mapFromGlobal(gp)):
                    return True
            except RuntimeError:        # field deleted out from under us
                pass
        return False

    def handle_key(self, event) -> bool:
        """Dropdown mode: the host forwards key events from its own
        field.  Returns True when the event was consumed (↑/↓ move the
        selection, Enter commits, Esc dismisses)."""
        key = event.key()
        if key == Qt.Key.Key_Down:
            self._step(+1)
            return True
        if key == Qt.Key.Key_Up:
            self._step(-1)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._commit_current()
            return True
        if key == Qt.Key.Key_Escape:
            self.close()
            return True
        return False

    # ── Local (saved-contract) entries ────────────────────────────────────────

    def _collect_local(self) -> list[dict]:
        from src.core import instruments
        seen: set[str] = set()
        entries: list[dict] = []
        for c in sorted(instruments.all_contracts(),
                        key=lambda x: x.get("symbol", "")):
            sym = c.get("symbol", "")
            if not sym or sym.upper() in self._exclude:
                continue
            sec = c.get("sec_type", "")
            local = c.get("local_symbol", "")
            exch = c.get("exchange", "")
            key = f"{sym}_{local}_{sec}"
            if key in seen:
                continue
            seen.add(key)
            detail = local if local and local != sym else exch
            entries.append({
                "symbol": sym,
                "sec_type": sec,
                "conid": str(c.get("conid", "") or ""),
                "name": c.get("name", ""),
                "detail": detail,
                "primary_exchange": c.get("primary_exchange", ""),
                "badge": sec or "?",
                "source": "local",
            })
        return entries

    # ── Live search plumbing ──────────────────────────────────────────────────

    def _on_text_changed(self, _text: str) -> None:
        self._remote = []
        self._remote_ready = False
        self._error = ""
        self._searching = False
        self._debounce.stop()
        if self._query() and self._workers:
            self._searching = True
            self._debounce.start()
        self._rebuild()

    def _query(self) -> str:
        src = self._ext_input if self._ext_input is not None else self._search
        return src.text().strip()

    def _fire_search(self) -> None:
        pattern = self._query()
        if not pattern or not self._workers:
            self._searching = False
            self._rebuild()
            return
        self._token += 1
        self._worker_idx = 0
        self._send_to_worker(self._workers[0], pattern)

    def _send_to_worker(self, worker, pattern: str) -> None:
        if worker not in self._wired:
            worker.symbol_search_results.connect(self._on_results)
            worker.symbol_search_failed.connect(self._on_failed)
            self._wired.append(worker)
        worker.request_symbol_search(self._token, pattern)

    def _on_results(self, token: int, results: list) -> None:
        if token != self._token:
            return
        self._searching = False
        self._remote = (filter_stock_results(results)
                        + extract_future_results(results))
        self._remote_ready = True
        # Queue a month-chain fetch for each new futures root so the
        # row can expand into pickable expirations.
        for r in self._remote:
            sym = r["symbol"].upper()
            if (r.get("sec_type") == "FUT"
                    and sym not in self._fut_chains
                    and sym not in self._chain_queue
                    and sym != self._chain_active):
                self._chain_queue.append(sym)
        self._pump_chain_queue()
        self._rebuild()

    def _on_failed(self, token: int, msg: str) -> None:
        if token != self._token:
            return
        self._worker_idx += 1
        if self._worker_idx < len(self._workers):
            self._send_to_worker(self._workers[self._worker_idx],
                                 self._query())
            return
        self._searching = False
        self._error = msg
        self._rebuild()

    def closeEvent(self, event) -> None:  # noqa: N802 – Qt override
        self._remove_outside_click_filter()
        self._debounce.stop()
        self._chain_queue.clear()
        self._chain_active = None
        for w in self._wired:
            try:
                w.symbol_search_results.disconnect(self._on_results)
                w.symbol_search_failed.disconnect(self._on_failed)
            except (RuntimeError, TypeError):
                pass
        self._wired.clear()
        for w in self._browse_wired:
            try:
                w.browse_results.disconnect(self._on_browse_results)
                w.browse_failed.disconnect(self._on_browse_failed)
            except (RuntimeError, TypeError):
                pass
        self._browse_wired.clear()
        super().closeEvent(event)

    # ── List building ─────────────────────────────────────────────────────────

    def _rebuild(self) -> None:
        self._lst.clear()
        q = self._query().upper()

        local_pool, remote_pool = self._local, self._remote
        if self._stocks_only:
            local_pool = [e for e in local_pool
                          if e.get("sec_type", "STK") == "STK"]
            remote_pool = [r for r in remote_pool
                           if r.get("sec_type", "STK") == "STK"]
        locals_shown, remote_shown, already_added = merge_rows(
            local_pool, remote_pool, self._exclude, q)

        self._hdr.setText("Results" if q else "Saved contracts")

        for e in locals_shown:
            self._add_row(e)
        for r in remote_shown:
            if r.get("sec_type") == "FUT":
                self._add_fut_rows(r)
            else:
                self._add_row({**r, "badge": "STK", "source": "ibkr"})
        for sym in already_added:
            self._add_note(f"{sym} — already added")

        if q and self._searching:
            self._add_note("Searching IBKR…")
        elif q and self._error:
            self._add_note(f"IBKR search failed: {self._error}")
        elif q and not self._workers:
            self._add_note("No TWS connection — saved contracts only")
        elif (q and self._remote_ready and not remote_shown
                and not locals_shown and not already_added):
            self._add_note("No matches")
        elif not q and not locals_shown:
            self._add_note("Type to search IBKR…" if self._workers
                           else "(no saved symbols — connect to search)")

        for i in range(self._lst.count()):
            if self._lst.item(i).data(Qt.ItemDataRole.UserRole) is not None:
                self._lst.setCurrentRow(i)
                break
        # Explicit call: setCurrentRow won't fire currentRowChanged
        # when the index is unchanged across a rebuild, but the row
        # widgets are new and start unhighlighted.
        self._update_row_highlight()

        # Size the list to at most _VISIBLE_ROWS rows, using the rows'
        # real heights so a partial row never pokes out at the bottom;
        # more results scroll (wheel / ↑↓) behind the cut.
        total = sum(self._lst.sizeHintForRow(i)
                    for i in range(min(self._lst.count(), _VISIBLE_ROWS)))
        self._lst.setFixedHeight(total)
        self.adjustSize()

    def _add_fut_rows(self, root: dict) -> None:
        """Render a live futures root: its two nearest expirations as
        pickable rows once the chain has loaded, a status note before
        (loading) or when the chain came back unusable."""
        sym = root["symbol"].upper()
        if sym not in self._fut_chains:
            self._add_note(f"{sym} futures — loading expirations…")
            return
        chain = self._fut_chains[sym]
        if chain is None:
            self._add_note(f"{sym} futures — couldn't load expirations")
            return
        if not chain:
            self._add_note(f"{sym} — no active US futures")
            return
        for cr in chain:
            self._add_row({
                **cr,
                "name": root.get("name", ""),
                "primary_exchange": (cr.get("exchange")
                                     or root.get("primary_exchange", "")),
                "source": "ibkr",
                "badge": "FUT",
            })

    @staticmethod
    def _make_avatar(symbol: str) -> QLabel:
        """Circular ticker monogram (the reference design's logo slot —
        we have no logo assets, so the ticker itself fills the circle,
        font-sized down for longer tickers)."""
        text = (symbol or "?")[:4].upper()
        size = {1: 12, 2: 12, 3: 10}.get(len(text), 8)
        av = QLabel(text)
        av.setFixedSize(_AVATAR_PX, _AVATAR_PX)
        av.setAlignment(Qt.AlignmentFlag.AlignCenter)
        av.setStyleSheet(
            f"background:#2A3245;color:{Theme.TEXT};"
            f"border:none;border-radius:{_AVATAR_PX // 2}px;"
            f"font-size:{size}px;font-weight:700")
        return av

    _ROW_SS_SELECTED = "QWidget#srow{background:#2D4F80;border-radius:10px}"

    def _update_row_highlight(self) -> None:
        """Paint the keyboard-cursor row's background on the row widget
        itself.  ``::item:selected`` renders fine when the popup lives
        under a stylesheet-free ancestor (Book Trader) but paints
        NOTHING when an ancestor carries a stylesheet (the Settings
        dialog) — the row-widget background works in both contexts.
        Every child label pins ``background:transparent`` so styling
        the container can't surface opaque palette fills behind the
        text/pill (the nested-boxes artifact)."""
        cur = self._lst.currentRow()
        for i in range(self._lst.count()):
            w = self._lst.itemWidget(self._lst.item(i))
            if w is None:
                continue
            w.setStyleSheet(self._ROW_SS_SELECTED if i == cur else "")

    def _add_row(self, entry: dict) -> None:
        row = QWidget()
        row.setObjectName("srow")
        row.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        h = QHBoxLayout(row)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(10)

        h.addWidget(self._make_avatar(entry["symbol"]))

        sym = QLabel(entry["symbol"])
        sym.setStyleSheet(
            f"background:transparent;"
            f"color:{Theme.TEXT};font-size:13px;font-weight:bold")
        h.addWidget(sym)

        # Company/fund name preferred; legacy saved rows without one fall
        # back to their local-symbol/exchange detail.  Futures
        # expirations lead with their expiry date.
        name_text = entry.get("name") or entry.get("detail") or ""
        if (entry.get("source") == "ibkr"
                and entry.get("sec_type") == "FUT"
                and entry.get("expiry")):
            exp = format_expiry(entry["expiry"])
            name_text = f"{exp}  ·  {name_text}" if name_text else exp
        name = QLabel()
        name.setStyleSheet(
            f"background:transparent;color:{Theme.TEXT_DIM};font-size:12px")
        fm = QFontMetrics(name.font())
        # Elide to the space actually left beside the avatar, symbol
        # and exchange pill at the CURRENT popup width — a fixed elide
        # width pushes the pill off the edge in narrow dropdown mode.
        avail = max(60, self.width() - 240)
        name.setText(fm.elidedText(
            name_text, Qt.TextElideMode.ElideRight, avail))
        h.addWidget(name, 1)

        # Listing exchange preferred over the generic sec-type badge;
        # live futures roots keep an explicit FUT marker so they can't
        # be mistaken for the same-symbol stock.
        exch = display_exchange(entry.get("primary_exchange", ""))
        if entry.get("source") == "ibkr" and entry.get("sec_type") == "FUT":
            badge_text = f"FUT · {exch}" if exch else "FUT"
        else:
            badge_text = exch or entry.get("badge", "")
        badge = QLabel(badge_text)
        badge.setStyleSheet(
            f"background:transparent;"
            f"color:{Theme.TEXT_DIM};font-size:11px;"
            f"border:1px solid {Theme.BORDER};border-radius:10px;"
            f"padding:3px 11px")
        h.addWidget(badge, 0, Qt.AlignmentFlag.AlignRight)

        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, entry)
        item.setSizeHint(row.sizeHint())
        self._lst.addItem(item)
        self._lst.setItemWidget(item, row)

    def _add_note(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setForeground(QColor(Theme.TEXT_DIM))
        self._lst.addItem(item)

    # ── Selection / commit ────────────────────────────────────────────────────

    def _step(self, delta: int) -> None:
        n = self._lst.count()
        if n == 0:
            return
        row = self._lst.currentRow()
        for _ in range(n):
            row = (row + delta) % n
            it = self._lst.item(row)
            if it is not None and it.data(Qt.ItemDataRole.UserRole):
                self._lst.setCurrentRow(row)
                return

    def _commit_current(self) -> None:
        it = self._lst.currentItem()
        if it is None:
            return
        entry = it.data(Qt.ItemDataRole.UserRole)
        if not entry:
            return
        # Persistence happens in the host view's handler
        # (ensure_contract_saved / apply_future_pick) — a replacement
        # confirmation dialog can't be shown over a Qt.Popup window.
        self.close()
        self.symbol_selected.emit(entry)

    # ── Futures chain fetching (auto, one root at a time) ─────────────────────

    def _pump_chain_queue(self) -> None:
        if (self._chain_active is not None or not self._chain_queue
                or not self._workers):
            return
        self._chain_active = self._chain_queue.pop(0)
        self._chain_worker_idx = 0
        self._send_browse(self._workers[0], self._chain_active)

    def _send_browse(self, worker, symbol: str) -> None:
        if worker not in self._browse_wired:
            worker.browse_results.connect(self._on_browse_results)
            worker.browse_failed.connect(self._on_browse_failed)
            self._browse_wired.append(worker)
        worker.request_browse({
            "sec_type": "FUT",
            "symbol": symbol,
            "currency": "USD",
        })

    def _on_browse_results(self, results: list) -> None:
        sym = self._chain_active
        if sym is None:
            return
        # browse_results carries no token — another consumer's lookup
        # could land here.  Only accept a batch for our root symbol.
        if results and not any(
                (r.get("symbol") or "").upper() == sym for r in results):
            return
        self._chain_active = None
        self._fut_chains[sym] = pick_front_expiries(results)
        self._rebuild()
        self._pump_chain_queue()

    def _on_browse_failed(self, msg: str) -> None:
        sym = self._chain_active
        if sym is None:
            return
        self._chain_worker_idx += 1
        if self._chain_worker_idx < len(self._workers):
            self._send_browse(self._workers[self._chain_worker_idx], sym)
            return
        self._chain_active = None
        self._fut_chains[sym] = None  # failed — rendered as a note
        log.warning("SymbolSearch: %s futures chain fetch failed: %s",
                    sym, msg)
        self._rebuild()
        self._pump_chain_queue()
