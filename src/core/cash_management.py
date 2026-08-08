"""Cash-management preferences (in-memory singleton).

Holds the **sweep symbols** — up to four stock symbols usable for
cash-sweep operations (the account right-click menu offers one sweep
action per configured symbol).  Loaded from the active user's
``app_config`` (``preferences.cash_management``) on first access / on
login, cached in memory so trade paths can read it without hitting
disk, and persisted on edit.  Legacy configs carry a single
``sweep_symbol`` string — read as a one-entry list.

Mirrors the ``trading_prefs`` pattern (thread-safe, ``reload()`` after the
active config changes).
"""
from __future__ import annotations

import logging
import threading

from src.core import config_manager

log = logging.getLogger(__name__)

MAX_SWEEP_SYMBOLS = 4


def _clean_symbols(symbols) -> list[str]:
    """Upper-cased, deduped (order-preserving), capped at the max."""
    out: list[str] = []
    for s in symbols or []:
        sym = str(s or "").strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return out[:MAX_SWEEP_SYMBOLS]


class CashManagementPreferences:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sweep_symbols: list[str] = []
        self._sweep_contracts: dict[str, dict] = {}
        self._loaded = False

    # ── bootstrap ─────────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        cm = config_manager.load_preferences().get("cash_management", {})
        raw = cm.get("sweep_symbols")
        if isinstance(raw, list):
            self._sweep_symbols = _clean_symbols(raw)
        else:
            # Legacy single-symbol config.
            legacy = str(cm.get("sweep_symbol", "") or "").strip().upper()
            self._sweep_symbols = [legacy] if legacy else []
        contracts = cm.get("sweep_contracts", {})
        self._sweep_contracts = {
            str(k or "").strip().upper(): dict(v)
            for k, v in contracts.items()
            if isinstance(v, dict) and str(k or "").strip()
        } if isinstance(contracts, dict) else {}
        self._loaded = True
        log.info("CashManagementPreferences loaded (sweep_symbols=%r)",
                 self._sweep_symbols)

    # ── read ──────────────────────────────────────────────────────────────────

    @property
    def sweep_symbols(self) -> list[str]:
        with self._lock:
            self._ensure_loaded()
            return list(self._sweep_symbols)

    @property
    def sweep_symbol(self) -> str:
        """Legacy single-symbol view: the first configured symbol."""
        with self._lock:
            self._ensure_loaded()
            return self._sweep_symbols[0] if self._sweep_symbols else ""

    @property
    def sweep_contracts(self) -> dict[str, dict]:
        """{SYMBOL: contract metadata} captured at pick-time in the
        Cash Management page — lets the resolver / market-data path
        treat sweep symbols like chips without them being chips."""
        with self._lock:
            self._ensure_loaded()
            return {k: dict(v) for k, v in self._sweep_contracts.items()}

    # ── write ─────────────────────────────────────────────────────────────────

    def set_sweep_symbols(self, symbols, contracts: dict | None = None) -> None:
        """Update the in-memory list AND persist it to the active config.
        The legacy ``sweep_symbol`` key is kept in sync (first entry) so
        older builds restoring this config still find their symbol.
        *contracts* ({SYM: metadata}) replaces the stored pick-time
        contract metadata when given; either way it's pruned to the
        configured symbols."""
        cleaned = _clean_symbols(symbols)
        with self._lock:
            self._ensure_loaded()
            if contracts is not None:
                self._sweep_contracts = {
                    str(k or "").strip().upper(): dict(v)
                    for k, v in contracts.items()
                    if isinstance(v, dict) and str(k or "").strip()
                }
            self._sweep_contracts = {
                k: v for k, v in self._sweep_contracts.items()
                if k in cleaned}
            self._sweep_symbols = cleaned
            kept_contracts = {k: dict(v)
                              for k, v in self._sweep_contracts.items()}
        prefs = config_manager.load_preferences()
        cm = dict(prefs.get("cash_management", {}))
        cm["sweep_symbols"] = cleaned
        cm["sweep_symbol"] = cleaned[0] if cleaned else ""
        cm["sweep_contracts"] = kept_contracts
        prefs["cash_management"] = cm
        config_manager.save_preferences(prefs)
        log.info("CashManagement: sweep symbols set to %r", cleaned)

    def set_sweep_symbol(self, symbol: str) -> None:
        """Legacy setter — replaces the list with a single symbol."""
        self.set_sweep_symbols([symbol] if symbol else [])

    # ── convenience ───────────────────────────────────────────────────────────

    def reload(self) -> None:
        """Force a fresh load from the active config (e.g. after login/restore)."""
        with self._lock:
            self._loaded = False
            self._ensure_loaded()


# ── Module-level singleton ────────────────────────────────────────────────────
cash_prefs = CashManagementPreferences()
