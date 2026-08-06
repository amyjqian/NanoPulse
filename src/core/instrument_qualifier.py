"""Reconnect re-qualification — self-healing chip/hot-button metadata.

Chips own their contract identity (see ``src/core/instruments.py``), so
an entry that lost its metadata (legacy config, interrupted save) or
holds an **expired futures contract** would silently stop streaming and
ordering.  This object watches every IB worker's
``connection_status_changed`` and, on each transition to connected,
re-qualifies whatever needs it via the worker's ``request_browse``:

* no conid            → STK qualify (or FUT root chain for known
                        futures roots — bare entries carry no sec_type,
                        so roots in the shared futures tick table are
                        treated as futures)
* FUT expiry in past  → root chain browse → front month
                        (``pick_front_expiries(count=1)``)

Results land via ``config_manager.upsert_instrument`` — every chip /
hot-button reference of the symbol updates in one write — and
``contract_requalified`` fires so the host can resubscribe market data
(same path as a manual future replacement).

OPT chips are skipped: OCC keys can't be re-qualified through a symbol
browse; they heal through position clicks, as always.

The worker's ``browse_results`` signal carries no request token, so
browses are strictly serialized, results are filtered by the expected
symbol, and the queue pauses while a ``SymbolSearchPopup`` exists (the
popup runs its own chain fetches on the same signal).
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, QTimer, Signal

from src.core import config_manager, instruments
from src.utils.helpers import _FUT_TICK_SIZES

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Delay between a worker reporting connected and the first browse — the
# worker is busy with account summary / positions right after connect.
_CONNECT_SETTLE_MS = 3000
# Retry cadence while paused (search popup open / worker busy).
_RETRY_MS = 2000


def needs_qualification(entry: dict) -> bool:
    """True when *entry* can't subscribe/order as stored: no conid, or
    a futures contract whose expiry has passed (ET).  OPT entries are
    never qualified here."""
    sec = str(entry.get("sec_type", "") or "").upper()
    if sec == "OPT":
        return False
    sym = str(entry.get("symbol", "") or "")
    # OCC-shaped chip keys are options even when sec_type is missing.
    from src.utils.helpers import format_option_symbol
    if format_option_symbol(sym) != sym:
        return False
    # Expiry-keyed (pinned) futures chips are frozen to their contract —
    # "re-qualifying" one to the front month would silently un-pin it.
    if instruments.is_pinned_fut(entry):
        return False
    if not str(entry.get("conid", "") or ""):
        return True
    if sec == "FUT":
        d = instruments._parse_expiry_date(str(entry.get("expiry", "") or ""))
        if d is not None and d < datetime.now(_ET).date():
            return True
    return False


def guess_browse_params(entry: dict) -> dict:
    """Browse params for *entry*.  Bare entries carry no sec_type — a
    root present in the shared futures tick table is treated as FUT
    (MNQ, ES, CL, …), anything else as a SMART US stock."""
    sym = str(entry.get("symbol", "") or "").upper()
    sec = str(entry.get("sec_type", "") or "").upper()
    if not sec:
        sec = "FUT" if sym in _FUT_TICK_SIZES else "STK"
    if sec == "FUT":
        return {"sec_type": "FUT", "symbol": sym,
                "exchange": str(entry.get("exchange", "") or ""),
                "currency": "USD"}
    return {"sec_type": "STK", "symbol": sym,
            "exchange": "SMART", "currency": "USD"}


class InstrumentQualifier(QObject):
    """Serialized, popup-aware re-qualification queue over IB workers."""

    contract_requalified = Signal(str)   # symbol — host resubscribes

    def __init__(self, active_threads: dict, parent=None) -> None:
        super().__init__(parent)
        self._active_threads = active_threads
        self._attached: set[int] = set()      # id(worker) already wired
        self._attempted: set[str] = set()     # symbols tried this session
        self._queue: list[str] = []
        self._busy_symbol: str | None = None
        self._busy_worker = None

    # ── Worker discovery (host calls periodically) ───────────────────────────

    def scan_workers(self) -> None:
        """Attach to any worker not yet wired.  Cheap; the host calls it
        from an existing periodic timer so dynamically-created workers
        (Connect page) are picked up without lifecycle plumbing."""
        for worker in list(self._active_threads.values()):
            if id(worker) in self._attached:
                continue
            try:
                worker.connection_status_changed.connect(self._on_status)
            except Exception:
                continue
            self._attached.add(id(worker))
            if getattr(worker, "is_ib_connected", False):
                self._schedule_batch()

    def _on_status(self, _label: str, connected: bool) -> None:
        if connected:
            self._schedule_batch()

    # ── Batch building / pumping ─────────────────────────────────────────────

    def _schedule_batch(self) -> None:
        QTimer.singleShot(_CONNECT_SETTLE_MS, self._build_batch)

    def _build_batch(self) -> None:
        for e in instruments.all_contracts():
            sym = e["symbol"].upper()
            if sym in self._attempted or sym in self._queue:
                continue
            if needs_qualification(e):
                self._queue.append(sym)
        if self._queue:
            log.info("InstrumentQualifier: %d symbol(s) need "
                     "qualification: %s", len(self._queue), self._queue)
            self._pump()

    def _connected_worker(self):
        for w in self._active_threads.values():
            if w.isRunning() and getattr(w, "is_ib_connected", False):
                return w
        return None

    def _popup_open(self) -> bool:
        try:
            from PySide6.QtWidgets import QApplication
            from src.ui.widgets.symbol_search_popup import SymbolSearchPopup
            # Only a VISIBLE popup owns the browse signals — dropdown-
            # mode instances persist hidden between shows.
            return any(isinstance(w, SymbolSearchPopup) and w.isVisible()
                       for w in QApplication.topLevelWidgets())
        except Exception:
            return False

    def _pump(self) -> None:
        if self._busy_symbol is not None or not self._queue:
            return
        if self._popup_open():
            # The search popup shares the untokenized browse signals —
            # stand down until it closes.
            QTimer.singleShot(_RETRY_MS, self._pump)
            return
        worker = self._connected_worker()
        if worker is None:
            self._queue.clear()   # rebuilt on the next connect
            return
        sym = self._queue.pop(0)
        entry = instruments.get_contract(sym) or {"symbol": sym}
        self._busy_symbol = sym
        self._busy_worker = worker
        self._attempted.add(sym)
        worker.browse_results.connect(self._on_results)
        worker.browse_failed.connect(self._on_failed)
        params = guess_browse_params(entry)
        log.info("InstrumentQualifier: qualifying %s as %s via %s",
                 sym, params["sec_type"], getattr(worker, "_label", "?"))
        worker.request_browse(params)

    def _release_worker(self) -> None:
        if self._busy_worker is not None:
            for sig, slot in ((self._busy_worker.browse_results,
                               self._on_results),
                              (self._busy_worker.browse_failed,
                               self._on_failed)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self._busy_worker = None
        self._busy_symbol = None

    # ── Results ──────────────────────────────────────────────────────────────

    def _on_results(self, results: list) -> None:
        sym = self._busy_symbol
        if not sym:
            return
        rows = [r for r in results
                if str(r.get("symbol", "")).upper() == sym]
        if not rows:
            # Cross-talk from another consumer's browse — ignore and
            # keep waiting for ours (the timeout path is browse_failed).
            return
        self._release_worker()
        if rows[0].get("sec_type") == "FUT":
            from src.ui.widgets.symbol_search_popup import pick_front_expiries
            front = pick_front_expiries(rows, count=1)
            row = front[0] if front else None
        else:
            row = rows[0]
        meta = instruments.contract_meta_from_result(row) if row else None
        if meta and meta.get("conid"):
            config_manager.upsert_instrument(sym, meta)
            log.info("InstrumentQualifier: %s → conid %s (%s %s)",
                     sym, meta["conid"], meta.get("sec_type", "?"),
                     meta.get("local_symbol", ""))
            self.contract_requalified.emit(sym)
        else:
            log.warning("InstrumentQualifier: no usable contract for %s",
                        sym)
        self._pump()

    def _on_failed(self, msg: str) -> None:
        sym = self._busy_symbol
        self._release_worker()
        log.warning("InstrumentQualifier: qualification failed for %s: %s",
                    sym, msg)
        self._pump()
