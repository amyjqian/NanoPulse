"""
TradingPreferencesManager — in-memory singleton for active trading preferences.

Loads ``preferences`` from ``app_config.json`` at first access and keeps the
*Futures*, *Stocks* and *Order guards* sections in memory.  The
PreferencesDialog calls :meth:`update` on every save so the very next trade
always uses fresh values.

Usage
-----
    from src.core.trading_prefs import trading_prefs

    # Read (never hits disk after first load)
    fut = trading_prefs.futures          # dict
    stk = trading_prefs.stocks           # dict
    grd = trading_prefs.guards           # dict
    ok = trading_prefs.allow_all_accounts_orders()   # bool

    # Write (called by PreferencesDialog._save)
    trading_prefs.update(futures_dict, stocks_dict, guards_dict)

All public access is through the module-level ``trading_prefs`` singleton.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from src.core import config_manager

log = logging.getLogger(__name__)

# ── Default values (mirrors _DEFAULTS in preferences_dialog.py) ──────────────

_FUTURES_DEFAULTS: dict[str, Any] = {
    "quantity": 1,
    "quantity_unit": "Contracts",
    "limit_cap": 0,
    "limit_cap_unit": "ticks",
    "mid_price_behavior": "Normal",
    "buy_bid_behavior": "Patient",
    "sell_ask_behavior": "Patient",
}

_STOCKS_DEFAULTS: dict[str, Any] = {
    "quantity": 100,
    "quantity_unit": "Shares",
    "limit_cap": 0,
    "limit_cap_unit": "ticks",
    "mid_price_behavior": "Normal",
    "buy_bid_offset": 0,
    "buy_bid_unit": "ticks",
    "sell_ask_offset": 0,
    "sell_ask_unit": "ticks",
}

# Order-safety switches that aren't tied to an asset class.
_GUARDS_DEFAULTS: dict[str, Any] = {
    # False blocks a Buy/Sell whose account fan-out covers every visible
    # account (the built-in "All" pill).  Defaults to True = the classic
    # behaviour, so an existing config sees no change.
    "allow_all_group_orders": True,
}


class TradingPreferencesManager:
    """Singleton-style in-memory cache for Futures, Stocks & guard prefs.

    Thread-safe: a lock guards every read / write so IBWorkerThread
    coroutines can safely call :meth:`get` while the UI thread calls
    :meth:`update`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._futures: dict[str, Any] = {}
        self._stocks: dict[str, Any] = {}
        self._guards: dict[str, Any] = {}
        self._loaded = False

    # ── Bootstrap ─────────────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        """Lazy-load from disk on first access."""
        if self._loaded:
            return
        prefs = config_manager.load_preferences()
        self._futures = {**_FUTURES_DEFAULTS,
                         **prefs.get("trading_futures", {})}
        self._stocks = {**_STOCKS_DEFAULTS, **prefs.get("trading_stocks", {})}
        self._guards = {**_GUARDS_DEFAULTS, **prefs.get("trading_guards", {})}
        self._loaded = True
        log.info(
            "TradingPreferencesManager loaded  "
            "(futures: qty=%s, limit_cap=%s %s | "
            "stocks: qty=%s, limit_cap=%s %s)",
            self._futures.get("quantity"),
            self._futures.get("limit_cap"),
            self._futures.get("limit_cap_unit"),
            self._stocks.get("quantity"),
            self._stocks.get("limit_cap"),
            self._stocks.get("limit_cap_unit"),
        )

    # ── Read ──────────────────────────────────────────────────────────────────

    @property
    def futures(self) -> dict[str, Any]:
        """Return a *snapshot copy* of the Futures prefs dict."""
        with self._lock:
            self._ensure_loaded()
            return dict(self._futures)

    @property
    def stocks(self) -> dict[str, Any]:
        """Return a *snapshot copy* of the Stocks prefs dict."""
        with self._lock:
            self._ensure_loaded()
            return dict(self._stocks)

    @property
    def guards(self) -> dict[str, Any]:
        """Return a *snapshot copy* of the order-guard prefs dict."""
        with self._lock:
            self._ensure_loaded()
            return dict(self._guards)

    def allow_all_accounts_orders(self) -> bool:
        """False when the user disabled orders that hit every account.

        Coerced to bool so a hand-edited / ``null`` value in the config
        can't read as "blocked" while the Settings toggle shows On.
        """
        return bool(self.guards.get("allow_all_group_orders", True))

    def get(self, symbol_type: str) -> dict[str, Any]:
        """Return prefs for *symbol_type* (``"FUT"`` → futures, ``"STK"`` → stocks).

        Falls back to futures for unrecognised types.
        """
        if symbol_type == "STK":
            return self.stocks
        return self.futures

    # ── Write ─────────────────────────────────────────────────────────────────

    def update(
        self,
        futures: dict[str, Any] | None = None,
        stocks: dict[str, Any] | None = None,
        guards: dict[str, Any] | None = None,
    ) -> None:
        """Replace one or more in-memory pref dicts.

        Called by :meth:`PreferencesDialog._save` right after it writes to
        disk, so the in-memory state is always in sync.

        Loads first: a partial update (say ``update(guards=...)`` alone)
        must not leave the untouched sections empty forever — ``_loaded``
        is flipped below, so ``_ensure_loaded`` would never repair them.
        """
        with self._lock:
            self._ensure_loaded()
            if futures is not None:
                self._futures = {**_FUTURES_DEFAULTS, **futures}
            if stocks is not None:
                self._stocks = {**_STOCKS_DEFAULTS, **stocks}
            if guards is not None:
                self._guards = {**_GUARDS_DEFAULTS, **guards}
            self._loaded = True
        log.debug("TradingPreferencesManager updated in memory")

    # ── Convenience ───────────────────────────────────────────────────────────

    def reload(self) -> None:
        """Force a fresh load from disk (e.g. after external config edit)."""
        with self._lock:
            self._loaded = False
            self._ensure_loaded()


# ── Module-level singleton ────────────────────────────────────────────────────
trading_prefs = TradingPreferencesManager()
