"""
reference_data.py – App-wide in-memory cache for strategy tiers.

Loaded once at startup; refreshed after any user-initiated save via
``reload_strategy_tiers()``.  (Contract identity lives on the chip /
hot-button entries — see ``src/core/instruments.py``.)

Tier fallback lookup order
---------------------------
1. Exact symbol match in strategy_tiers.db  (e.g. "NQ")
2. Asset-class catch-all row in strategy_tiers.db:
       symbol = "FUT"  when sec_type is FUT
       symbol = "STK"  when sec_type is STK or OPT
3. None  →  caller falls back to preferences-based tiers
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from PySide6.QtCore import QObject, Signal

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STRATEGY_DB  = _PROJECT_ROOT / "data" / "strategy_tiers.db"


_ORDER_TYPES: tuple[str, ...] = ("take_profit", "stop_loss", "stop", "trailing")

# sec_type  →  fallback symbol row in strategy_tiers.db
_FALLBACK_SYM: dict[str, str] = {"FUT": "FUT", "STK": "STK", "OPT": "STK"}


class ReferenceData(QObject):
    """Process-wide singleton caching strategy tiers.

    Attributes
    ----------
    strategy_tiers : dict[str, dict]
        symbol → {(order_type, tier_level, "offset"|"value"): float}
        Includes exact symbols AND the "FUT" / "STK" fallback rows.
    """

    strategy_tiers_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.strategy_tiers: dict[str, dict] = {}

    # ── Strategy Tiers ────────────────────────────────────────────────────────

    def reload_strategy_tiers(self) -> None:
        """Load all rows from strategy_tiers.db into the in-memory pivot."""
        pivot: dict[str, dict] = {}

        if _STRATEGY_DB.exists():
            try:
                conn = sqlite3.connect(_STRATEGY_DB)
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute(
                        "SELECT symbol, order_type, tier_level, offset, value "
                        "FROM strategy_tiers"
                    ).fetchall()
                    for r in rows:
                        sym  = r["symbol"]
                        ot   = r["order_type"]
                        tier = int(r["tier_level"])
                        if ot in _ORDER_TYPES and 1 <= tier <= 4:
                            pivot.setdefault(sym, {})
                            pivot[sym][(ot, tier, "offset")] = float(r["offset"] or 0)
                            pivot[sym][(ot, tier, "value")]  = float(r["value"]  or 0)
                    log.info(
                        "ReferenceData: strategy tiers loaded for %d symbol(s)",
                        len(pivot),
                    )
                finally:
                    conn.close()
            except Exception:
                log.exception("ReferenceData: failed to load strategy_tiers.db")

        self.strategy_tiers = pivot
        self.strategy_tiers_changed.emit()

    # ── Tier lookup with STK / FUT fallback ───────────────────────────────────

    def get_tiers_for_symbol(
        self,
        symbol: str,
        order_type: str,
    ) -> list[dict] | None:
        """Return per-tier dicts for *symbol* / *order_type*, with fallback.

        Lookup order
        ------------
        1. Exact symbol (e.g. "NQ")
        2. Asset-class fallback row — "FUT" for futures, "STK" for stocks/opts
        3. Returns ``None`` — caller should use preferences-based tiers

        Each returned dict::

            tier   : int    1..4
            offset : float  raw tick/price offset stored in DB
            value  : float  same as offset (used as price-offset in orders)
            dollar : float  offset × multiplier (dollar value per contract)
            label  : str    chip label, e.g. "T1  $200"
            unit   : str    "ticks"
        """
        sym_upper = symbol.upper()

        # 1. Exact symbol
        tiers = self._extract_tiers(sym_upper, order_type)
        if tiers:
            return tiers

        # 2. Asset-class fallback ("FUT" or "STK") — sec_type resolved
        # from the chip / hot-button instrument entries.
        from src.core import instruments
        sec      = instruments.get_sec_type(symbol)
        fallback = _FALLBACK_SYM.get(sec, "STK")
        tiers    = self._extract_tiers(fallback, order_type)
        if tiers:
            log.debug(
                "ReferenceData: %s/%s — using '%s' fallback tiers",
                symbol, order_type, fallback,
            )
            return tiers

        return None

    def _extract_tiers(self, symbol: str, order_type: str) -> list[dict]:
        """Build a tier list from the pivot for *symbol* / *order_type*.

        Returns plain ``label`` values ("T1" … "T4") so the caller can
        format them with the live qty without needing to re-parse.
        """
        d = self.strategy_tiers.get(symbol, {})
        result: list[dict] = []
        for tier in range(1, 5):
            offset = d.get((order_type, tier, "offset"), 0.0)
            if offset <= 0:
                continue
            value = d.get((order_type, tier, "value"), 0.0)
            result.append({
                "tier":   tier,
                "offset": offset,
                "value":  offset,   # price-offset used in order submission
                "dollar": value,    # per-contract dollar (offset × multiplier)
                "unit":   "price",  # offset is in price points, not ticks
                                    # (StrategyEditor stores val = offset * mult)
                "label":  f"T{tier}",   # caller renders "${dollar*qty}-{offset}"
            })
        return result


# ── Module-level singleton ────────────────────────────────────────────────────

_instance: ReferenceData | None = None


def get_instance() -> ReferenceData:
    """Return (and lazily create) the process-wide ReferenceData singleton."""
    global _instance
    if _instance is None:
        _instance = ReferenceData()
    return _instance

