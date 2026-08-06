"""Searchable add-symbol popup with live IBKR symbol lookup.

Shared by Book Trader's ticker "+" button and OrderEntryPanel's chip-bar
"+" button.  Two result sources merge into one list:

  * **Saved contracts** (ReferenceData) — shown instantly, works with no
    TWS connection.  This keeps futures/options chips addable exactly as
    before.
  * **Live IBKR matches** (``reqMatchingSymbols``) — as the user types,
    a debounced search runs on the highest-priority connected worker and
    STK matches stream in below the saved ones.  Picking one auto-saves
    the contract to the Contracts universe (SQLite) so the ladder /
    order paths can resolve it — the user never has to visit the
    Contracts tab first.

Keyboard: Up/Down navigate, Enter adds the highlighted row, Esc closes
(Popup window flag also closes on outside click).
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import (
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
_POPUP_WIDTH = 420
_LIST_MAX_HEIGHT = 320


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


def pick_front_expiries(chain: list[dict], count: int = 2,
                        today: date | None = None) -> list[dict]:
    """The *count* nearest unexpired USD futures from a browse chain,
    sorted by expiry.  Rows that aren't USD FUTs or whose expiry can't
    be parsed are dropped."""
    today = today or date.today()
    dated: list[tuple[date, dict]] = []
    for r in chain:
        if r.get("sec_type") != "FUT":
            continue
        if (r.get("currency") or "").upper() != "USD":
            continue
        d = _expiry_date(r.get("expiry", ""))
        if d is None or d < today:
            continue
        dated.append((d, r))
    dated.sort(key=lambda t: (t[0], str(t[1].get("local_symbol", ""))))
    return [r for _d, r in dated[:count]]


def save_future_chain(results: list[dict], root_name: str = "") -> str:
    """Persist a reqContractDetails month chain for a futures root.

    Keeps USD FUT rows only, dedupes by conId against the stored
    universe, and saves the whole chain — ``pick_active_contract``
    then resolves the root to the front month exactly like a chain
    added from the Contracts tab.  Returns the root symbol ("" when
    the chain had no usable rows).
    """
    rows = [r for r in results
            if r.get("sec_type") == "FUT"
            and (r.get("currency") or "").upper() == "USD"]
    if not rows:
        return ""
    sym = (rows[0].get("symbol") or "").strip().upper()
    existing = config_manager.load_contracts()
    have = {str(c.get("conid", "")) for c in existing if c.get("conid")}
    added = 0
    for r in rows:
        cid = str(r.get("conid", "") or "")
        if not cid or cid in have:
            continue
        have.add(cid)
        existing.append({**r, "name": root_name})
        added += 1
    if added:
        config_manager.save_contracts(existing)
        from src.core.reference_data import get_instance as _rd
        _rd().reload_contracts()
        log.info("SymbolSearch: saved %d %s futures contract(s) from "
                 "IBKR chain", added, sym)
    return sym


def display_exchange(code: str) -> str:
    """User-facing name for an IB exchange code ("" stays "")."""
    code = (code or "").strip()
    return _EXCHANGE_DISPLAY.get(code.upper(), code)


def merge_rows(local_entries: list[dict], remote: list[dict],
               exclude: set[str], query: str
               ) -> tuple[list[dict], list[dict], list[str]]:
    """Merge saved-contract rows with live IBKR matches for display.

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
    locals_shown = [
        e for e in local_entries
        if not q or q in e["symbol"].upper()
        or q in (e.get("name") or "").upper()
        or q in (e.get("detail") or "").upper()
    ]
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

    already_added = sorted(s for s in exclude
                           if q and s.startswith(q)
                           and s not in remote_fut_syms)
    return enriched, remote_shown, already_added


def replace_future_contract(entry: dict, root_name: str = "") -> str:
    """Swap the saved futures contract(s) for *entry*'s root with just
    *entry* — every stored FUT row for the root is dropped, so chips,
    hot buttons and subscriptions (all root-symbol-keyed, resolved via
    ReferenceData) re-point to the new expiration.  Returns the root
    symbol."""
    sym = (entry.get("symbol") or "").strip().upper()
    if not sym:
        return ""
    existing = config_manager.load_contracts()
    kept = [c for c in existing
            if c.get("sec_type") != "FUT"
            or c.get("symbol", "").upper() != sym]
    kept.append({**entry, "name": root_name})
    config_manager.save_contracts(kept)
    from src.core.reference_data import get_instance as _rd
    _rd().reload_contracts()
    log.info("SymbolSearch: replaced %s futures contract(s) with expiry "
             "%s", sym, entry.get("expiry", "?"))
    return sym


def apply_future_pick(parent, entry: dict) -> tuple[str, bool]:
    """Persist a futures-expiration pick from the search popup.

    Returns ``(root_symbol, replaced)``:

    * root not saved yet             → save the picked contract
    * picked contract already saved  → no change
    * root saved with a *different*
      expiration                     → ask (modal, parented to the host
      view) and replace on Yes; ``("", False)`` when the user declines.
    """
    sym = (entry.get("symbol") or "").strip().upper()
    if not sym:
        return "", False
    root_name = entry.get("name", "")
    existing = [c for c in config_manager.load_contracts()
                if c.get("sec_type") == "FUT"
                and c.get("symbol", "").upper() == sym]
    if not existing:
        save_future_chain([entry], root_name=root_name)
        _upsert_entry_meta(sym, entry)
        return sym, False
    if str(entry.get("conid", "")) in {str(c.get("conid", ""))
                                       for c in existing}:
        _upsert_entry_meta(sym, entry)   # heal chips missing metadata
        return sym, False  # same contract — nothing to do

    old_labels = [format_expiry(c.get("expiry", "")) for c in existing]
    old_desc = ", ".join(x for x in old_labels if x) or "another expiration"
    new_desc = format_expiry(entry.get("expiry", ""))
    from PySide6.QtWidgets import QMessageBox
    resp = QMessageBox.question(
        parent, "Replace future",
        f"{sym} is already saved as {old_desc}.\n"
        f"Replace it with {new_desc}?\n\n"
        f"This updates {sym} everywhere it is used — the ladder, "
        f"order entry and hot buttons.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No)
    if resp != QMessageBox.StandardButton.Yes:
        return "", False
    replace_future_contract(entry, root_name=root_name)
    _upsert_entry_meta(sym, entry)   # re-point existing chips/hot buttons
    return sym, True


def _upsert_entry_meta(sym: str, entry: dict) -> None:
    """Write *entry*'s contract metadata onto every existing chip /
    hot-button reference of *sym* (rollover keeps them in sync; the
    newly-added chip gets its metadata from the host's ``_add_symbol``)."""
    from src.core import instruments
    meta = instruments.contract_meta_from_result(entry)
    if meta:
        config_manager.upsert_instrument(sym, meta)


def ensure_contract_saved(result: dict) -> str:
    """Persist an IBKR search pick into the contracts universe.

    Returns ``(symbol, meta)`` — *meta* is the normalised contract
    metadata for the picked row (the host stores it on the new chip via
    ``_add_symbol``), or ``None`` when the row carries no conid.  The
    legacy contracts-DB write still runs for IBKR STK picks so the
    Contracts tab stays coherent during the transition (dropped in the
    final stage); futures are handled by ``apply_future_pick``.
    """
    from src.core import instruments
    sym = (result.get("symbol") or "").strip().upper()
    if not sym:
        return "", None
    meta = instruments.contract_meta_from_result(result)
    if (result.get("source") != "ibkr"
            or result.get("sec_type", "STK") != "STK"):
        return sym, meta

    existing = config_manager.load_contracts()
    conid = str(result.get("conid") or "")
    known = any(
        (conid and str(c.get("conid", "")) == conid)
        or (c.get("symbol", "").upper() == sym
            and c.get("sec_type") == result.get("sec_type", "STK"))
        for c in existing)
    if known:
        return sym, meta

    existing.append({
        "conid": result.get("conid", ""),
        "symbol": sym,
        "sec_type": result.get("sec_type", "STK"),
        "exchange": "SMART",
        "currency": result.get("currency", "USD") or "USD",
        "expiry": "",
        "strike": "",
        "right": "",
        "local_symbol": sym,
        "multiplier": "",
        "primary_exchange": result.get("primary_exchange", ""),
        "name": result.get("name", ""),
    })
    config_manager.save_contracts(existing)
    from src.core.reference_data import get_instance as _rd
    _rd().reload_contracts()
    log.info("SymbolSearch: saved %s (%s) from IBKR lookup",
             sym, result.get("primary_exchange", ""))
    return sym, meta


class SymbolSearchPopup(QDialog):
    """Spotlight-style popup: search field on top, merged results below."""

    symbol_selected = Signal(dict)  # row dict incl. "symbol" + "source"

    def __init__(self, parent: QWidget, workers: list,
                 exclude: set[str] | None = None) -> None:
        super().__init__(parent)
        self._workers = workers
        self._exclude = {s.upper() for s in (exclude or set())}
        self._wired: list = []

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

        self.setWindowFlags(Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFixedWidth(_POPUP_WIDTH)
        self.setStyleSheet(
            f"QDialog{{background:{Theme.BG_PANEL};"
            f"border:1px solid {Theme.BORDER};border-radius:8px}}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search symbol…")
        self._search.setStyleSheet(
            f"QLineEdit{{background:{Theme.BG_INPUT};color:{Theme.TEXT};"
            f"border:1px solid {Theme.BORDER};border-radius:6px;"
            f"padding:8px 10px;font-size:14px}}"
            f"QLineEdit:focus{{border-color:{Theme.ACCENT}}}")
        lay.addWidget(self._search)

        self._hdr = QLabel("Saved contracts")
        self._hdr.setStyleSheet(
            f"color:{Theme.TEXT_DIM};font-size:11px;padding:2px 2px 0 2px")
        lay.addWidget(self._hdr)

        self._lst = QListWidget()
        self._lst.setMaximumHeight(_LIST_MAX_HEIGHT)
        self._lst.setStyleSheet(
            f"QListWidget{{background:transparent;color:{Theme.TEXT};"
            f"border:none;outline:none}}"
            f"QListWidget::item{{border-radius:6px;padding:0px}}"
            f"QListWidget::item:selected{{background:#334058}}"
            f"QListWidget::item:hover{{background:#232A3B}}")
        self._lst.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay.addWidget(self._lst)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._fire_search)

        self._search.textChanged.connect(self._on_text_changed)
        self._search.returnPressed.connect(self._commit_current)
        self._lst.itemClicked.connect(lambda _it: self._commit_current())

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

    # ── Public ────────────────────────────────────────────────────────────────

    def open_below(self, anchor: QWidget) -> None:
        """Show the popup anchored under *anchor* and focus the search box."""
        self.move(anchor.mapToGlobal(anchor.rect().bottomLeft()))
        self.show()
        self._search.setFocus()

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
        return self._search.text().strip()

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

        locals_shown, remote_shown, already_added = merge_rows(
            self._local, self._remote, self._exclude, q)

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

    def _add_row(self, entry: dict) -> None:
        row = QWidget()
        row.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 7, 10, 7)
        h.setSpacing(10)

        sym = QLabel(entry["symbol"])
        sym.setStyleSheet(
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
        name.setStyleSheet(f"color:{Theme.TEXT_DIM};font-size:12px")
        fm = QFontMetrics(name.font())
        name.setText(fm.elidedText(
            name_text, Qt.TextElideMode.ElideRight, 230))
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
            f"color:{Theme.TEXT_DIM};font-size:11px;"
            f"border:1px solid {Theme.BORDER};border-radius:4px;"
            f"padding:2px 8px")
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
