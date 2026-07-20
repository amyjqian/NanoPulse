"""Cash-management preferences (in-memory singleton).

Currently holds the **sweep symbol** — the stock used for cash-sweep
operations.  Loaded from the active user's ``app_config`` (``preferences.
cash_management``) on first access / on login, cached in memory so trade
paths can read it without hitting disk, and persisted on edit.

Mirrors the ``trading_prefs`` pattern (thread-safe, ``reload()`` after the
active config changes).
"""
from __future__ import annotations

import logging
import threading

from src.core import config_manager

log = logging.getLogger(__name__)

_DEFAULT_SWEEP_SYMBOL = ""


class CashManagementPreferences:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sweep_symbol = _DEFAULT_SWEEP_SYMBOL
        self._loaded = False

    # ── bootstrap ─────────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        cm = config_manager.load_preferences().get("cash_management", {})
        self._sweep_symbol = str(cm.get("sweep_symbol", _DEFAULT_SWEEP_SYMBOL) or "")
        self._loaded = True
        log.info("CashManagementPreferences loaded (sweep_symbol=%r)",
                 self._sweep_symbol)

    # ── read ──────────────────────────────────────────────────────────────────

    @property
    def sweep_symbol(self) -> str:
        with self._lock:
            self._ensure_loaded()
            return self._sweep_symbol

    # ── write ─────────────────────────────────────────────────────────────────

    def set_sweep_symbol(self, symbol: str) -> None:
        """Update the in-memory value AND persist it to the active config."""
        symbol = (symbol or "").strip().upper()
        with self._lock:
            self._sweep_symbol = symbol
            self._loaded = True
        prefs = config_manager.load_preferences()
        cm = dict(prefs.get("cash_management", {}))
        cm["sweep_symbol"] = symbol
        prefs["cash_management"] = cm
        config_manager.save_preferences(prefs)
        log.info("CashManagement: sweep symbol set to %r", symbol)

    # ── convenience ───────────────────────────────────────────────────────────

    def reload(self) -> None:
        """Force a fresh load from the active config (e.g. after login/restore)."""
        with self._lock:
            self._loaded = False
            self._ensure_loaded()


# ── Module-level singleton ────────────────────────────────────────────────────
cash_prefs = CashManagementPreferences()
