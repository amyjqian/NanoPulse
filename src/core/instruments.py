"""Instrument resolver — contract identity read from chip / hot-button entries.

NanoPulse persists only *user workspace state*: each symbol chip
(``order_entry_symbols`` / ``book_trader_symbols`` in the per-user
``app_config-<username>.json``) and each hot button may carry its own
contract metadata inline (see :data:`META_KEYS`), captured at add-time
from the live TWS symbol search.  This module is the single read path
for that metadata — every consumer that used to go through
``ReferenceData``'s contract cache resolves here instead.

Thread-safety: pure functions, **no Qt imports** — the IB worker thread
calls into this module (``_contract_to_dict``, ``_do_subscribe_depth``).
Reads go through an mtime-keyed cache behind a module lock; config
writes are atomic (``os.replace`` in ``config_manager._write_json``), so
a concurrent reader sees the old or the new file, never a partial one.

The legacy ``data/contracts.db`` is read exactly once per config, by
:func:`migrate_active_config_from_db`, which enriches bare string chip
entries with the metadata the DB still holds.  It is never written.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Metadata keys persisted on a chip / hot-button entry (superset of the
# legacy contracts.db columns; ``min_tick`` is new — the DB never stored
# tick sizes).  ``conid`` and ``strike`` are coerced to str to match the
# shape the rest of the app has always used.
META_KEYS = (
    "conid", "sec_type", "exchange", "currency", "expiry", "strike",
    "right", "local_symbol", "multiplier", "primary_exchange", "name",
    "min_tick",
)

_SEC_ORDER = {"STK": 0, "FUT": 1, "OPT": 2}


# ── Normalisers (shared with config_manager) ─────────────────────────────────


def normalise_contract_meta(d: Any) -> dict[str, Any] | None:
    """Whitelist *d* down to :data:`META_KEYS`, coercing types.  Returns
    ``None`` when *d* is not a dict or carries no metadata at all."""
    if not isinstance(d, dict):
        return None
    out: dict[str, Any] = {}
    for k in META_KEYS:
        v = d.get(k)
        if v in (None, ""):
            continue
        if k in ("conid", "strike"):
            out[k] = str(v)
        elif k == "min_tick":
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0:
                out[k] = f
        else:
            out[k] = str(v)
    return out or None


def normalise_symbol_entry(e: Any) -> dict[str, Any] | None:
    """Coerce a chip-list entry (legacy bare string OR dict) into
    ``{"symbol": …, **metadata}``.  Returns ``None`` for junk."""
    if isinstance(e, str):
        sym = e.strip()
        return {"symbol": sym} if sym else None
    if isinstance(e, dict):
        sym = str(e.get("symbol", "") or "").strip()
        if not sym:
            return None
        out = {"symbol": sym}
        meta = normalise_contract_meta(e)
        if meta:
            out.update(meta)
        return out
    return None


def entry_has_meta(e: dict[str, Any]) -> bool:
    """True when the entry carries a resolvable contract (has a conid)."""
    return bool(str(e.get("conid", "") or ""))


def _parse_expiry_date(expiry: str) -> date | None:
    """Parse IB's ``YYYYMMDD`` / ``YYYYMM`` expiry into a date (monthly
    form → last day of the month).  ``None`` for empty/unparseable."""
    if not expiry:
        return None
    s = str(expiry).strip()
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    if len(s) == 6 and s.isdigit():
        try:
            year, month = int(s[0:4]), int(s[4:6])
            if month == 12:
                return date(year, 12, 31)
            from calendar import monthrange
            return date(year, month, monthrange(year, month)[1])
        except ValueError:
            return None
    return None


# ── Cached read of the active config's instrument entries ────────────────────

_lock = threading.Lock()
# (path, mtime_ns) → (oe_entries, bt_entries, hot_metas)
_cache_key: tuple[str, int] | None = None
_cache_val: tuple[list[dict], list[dict], list[dict]] = ([], [], [])


def invalidate_cache() -> None:
    """Drop the cached entry snapshot (config write / user switch)."""
    global _cache_key
    with _lock:
        _cache_key = None


def _load_state() -> tuple[list[dict], list[dict], list[dict]]:
    """(order-entry entries, book-trader entries, hot-button metas) for
    the active config, mtime-cached.  Never raises."""
    global _cache_key, _cache_val
    from src.core import config_manager as _cm
    p: Path = _cm._default_path()
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        mtime = -1
    key = (str(p), mtime)
    with _lock:
        if key == _cache_key:
            return _cache_val
    # Parse outside the lock — worst case two threads parse concurrently
    # and the last one wins with identical data.
    oe: list[dict] = []
    bt: list[dict] = []
    hot: list[dict] = []
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = {}
    for raw in data.get("order_entry_symbols", []) or []:
        e = normalise_symbol_entry(raw)
        if e is not None:
            oe.append(e)
    for raw in data.get("book_trader_symbols", []) or []:
        e = normalise_symbol_entry(raw)
        if e is not None:
            bt.append(e)
    for b in data.get("hot_buttons", []) or []:
        if not isinstance(b, dict):
            continue
        meta = normalise_contract_meta(b.get("contract"))
        if meta is None:
            continue
        sym = str(b.get("symbol", "") or "").strip()
        if sym:
            hot.append({"symbol": sym, **meta})
    with _lock:
        _cache_key = key
        _cache_val = (oe, bt, hot)
    return _cache_val


# ── Resolver API ─────────────────────────────────────────────────────────────


def all_contracts() -> list[dict[str, Any]]:
    """Every known instrument entry (order-entry chips, book-trader
    chips, hot buttons), deduped by symbol — a chip symbol maps to ONE
    instrument in this design (OPT chips are keyed by their OCC local
    symbol, so roots never collide across sec types) — with entries
    that carry a conid winning over bare ones.  Sorted STK < FUT < OPT
    then symbol, the presentation order the contracts DB used."""
    oe, bt, hot = _load_state()
    merged: dict[str, dict] = {}
    for e in [*oe, *bt, *hot]:
        k = e["symbol"].upper()
        cur = merged.get(k)
        if cur is None or (not entry_has_meta(cur) and entry_has_meta(e)):
            merged[k] = e
    out = list(merged.values())
    out.sort(key=lambda c: (
        _SEC_ORDER.get(str(c.get("sec_type", "") or ""), 9),
        c["symbol"],
    ))
    return out


def get_contract(query: str) -> dict[str, Any] | None:
    """Resolve *query* (root symbol, OCC key, or local symbol) to one
    entry.  An exact ``local_symbol`` match wins (pin-to-expiry); then
    symbol matches in order order-entry → book-trader → hot buttons,
    preferring entries that carry a conid."""
    if not query:
        return None
    q = query.upper().strip()
    oe, bt, hot = _load_state()
    ordered = [*oe, *bt, *hot]
    for e in ordered:
        if str(e.get("local_symbol", "") or "").upper().strip() == q:
            return e
    fallback: dict | None = None
    for e in ordered:
        if e["symbol"].upper() != q:
            continue
        if entry_has_meta(e):
            return e
        if fallback is None:
            fallback = e
    return fallback


def get_sec_type(symbol: str) -> str:
    """Best-effort sec_type — priority STK > FUT > OPT > '?'.  Mirrors
    ``ReferenceData.get_sec_type`` including the prefix fallback (a
    local-symbol query like "MNQM6" matches the "MNQ" entry)."""
    if not symbol:
        return "?"
    q = symbol.upper().strip()
    matched: set[str] = set()
    for e in all_contracts():
        if e["symbol"].upper() == q:
            matched.add(str(e.get("sec_type", "") or "?") or "?")
    if not matched:
        for e in all_contracts():
            if e["symbol"].upper().startswith(q) or q.startswith(
                    e["symbol"].upper()):
                matched.add(str(e.get("sec_type", "") or "?") or "?")
    for pref in ("STK", "FUT", "OPT"):
        if pref in matched:
            return pref
    return matched.pop() if matched else "?"


def get_multiplier(symbol: str) -> float:
    e = get_contract(symbol)
    if e is None:
        return 1.0
    try:
        return float(e.get("multiplier") or 1.0)
    except (TypeError, ValueError):
        return 1.0


def get_primary_exchange(symbol: str) -> str:
    e = get_contract(symbol)
    return str(e.get("primary_exchange", "") or "") if e else ""


def get_min_tick(symbol: str) -> float | None:
    e = get_contract(symbol)
    if e is None:
        return None
    v = e.get("min_tick")
    try:
        f = float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def find_by_conid(conid: Any) -> dict[str, Any] | None:
    """Entry whose conid matches (string compare), or None."""
    want = str(conid or "")
    if not want:
        return None
    for e in all_contracts():
        if str(e.get("conid", "") or "") == want:
            return e
    return None


def build_md_params(symbol: str) -> dict[str, Any] | None:
    """The param dict ``IBWorkerThread.subscribe_background_symbols``
    expects, or ``None`` when *symbol* has no conid (L1 can't be
    subscribed regardless)."""
    e = get_contract(symbol)
    if e is None or not entry_has_meta(e):
        return None
    try:
        conid = int(str(e["conid"]))
    except (TypeError, ValueError):
        return None
    return {
        "conid":        conid,
        "symbol":       symbol,
        "sec_type":     str(e.get("sec_type", "") or ""),
        "exchange":     str(e.get("exchange", "") or "SMART"),
        "currency":     str(e.get("currency", "") or "USD"),
        "local_symbol": str(e.get("local_symbol", "") or ""),
        "expiry":       str(e.get("expiry", "") or ""),
        "multiplier":   str(e.get("multiplier", "") or ""),
    }


def contract_meta_from_result(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Search-popup / browse result row → normalised metadata dict.
    STK results default ``exchange="SMART"`` and ``local_symbol=symbol``
    (mirrors the shape the legacy ``ensure_contract_saved`` wrote)."""
    if not isinstance(entry, dict):
        return None
    d = dict(entry)
    if str(d.get("sec_type", "") or "").upper() == "STK":
        d.setdefault("exchange", "SMART")
        d.setdefault("currency", "USD")
        if not d.get("local_symbol"):
            d["local_symbol"] = str(d.get("symbol", "") or "").strip().upper()
    return normalise_contract_meta(d)


# ── One-time migration from the legacy contracts.db ──────────────────────────

_LEGACY_DB = Path(__file__).resolve().parents[2] / "data" / "contracts.db"


def _pick_from_rows(query: str, rows: list[dict]) -> dict | None:
    """``ReferenceData.pick_active_contract`` over a plain row list:
    exact local_symbol > root symbol; FUT multi-expiry → soonest
    non-expired, else latest."""
    q = query.upper().strip()
    for c in rows:
        if str(c.get("local_symbol", "") or "").upper().strip() == q:
            return c
    matches = [c for c in rows
               if str(c.get("symbol", "") or "").upper().strip() == q]
    if not matches:
        return None
    if len(matches) == 1 or (
            str(matches[0].get("sec_type", "") or "").upper() != "FUT"):
        return matches[0]
    today = datetime.now(_ET).date()
    dated = [(_parse_expiry_date(c.get("expiry", "")), c) for c in matches]
    future = sorted((d, c) for d, c in dated if d is not None and d >= today)
    if future:
        return future[0][1]
    past = sorted(((d, c) for d, c in dated if d is not None), reverse=True)
    return past[0][1] if past else matches[0]


def migrate_active_config_from_db() -> int:
    """Enrich the active config's chip / hot-button entries with contract
    metadata from the legacy ``contracts.db``.  Read-only on the DB,
    idempotent (entries that already carry a conid are skipped), and a
    no-op when the DB file is absent.  Returns the number of entries
    updated."""
    if not _LEGACY_DB.exists():
        return 0
    from src.core import config_manager as _cm
    import sqlite3
    try:
        conn = sqlite3.connect(_LEGACY_DB)
        conn.row_factory = sqlite3.Row
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(contracts)")]
            rows = [dict(r) for r in conn.execute(
                f"SELECT {', '.join(cols)} FROM contracts")]
        finally:
            conn.close()
    except sqlite3.Error:
        log.exception("instruments: could not read legacy contracts.db")
        return 0
    if not rows:
        return 0

    updated = 0

    def _enrich(entries: list[dict]) -> bool:
        nonlocal updated
        changed = False
        for e in entries:
            if entry_has_meta(e):
                continue
            row = _pick_from_rows(e["symbol"], rows)
            meta = normalise_contract_meta(row) if row else None
            if meta and meta.get("conid"):
                e.update(meta)
                updated += 1
                changed = True
        return changed

    oe = _cm.load_order_entry_entries()
    if _enrich(oe):
        _cm.save_order_entry_entries(oe)
    bt = _cm.load_book_trader_entries()
    if _enrich(bt):
        _cm.save_book_trader_entries(bt)

    buttons = _cm.load_hot_buttons()
    hot_changed = False
    for b in buttons:
        if normalise_contract_meta(b.get("contract")) is not None:
            continue
        sym = str(b.get("symbol", "") or "").strip()
        if not sym:
            continue
        row = _pick_from_rows(sym, rows)
        meta = normalise_contract_meta(row) if row else None
        if meta and meta.get("conid"):
            b["contract"] = meta
            updated += 1
            hot_changed = True
    if hot_changed:
        _cm.save_hot_buttons(buttons)

    if updated:
        log.info("instruments: migrated %d entr%s from contracts.db",
                 updated, "y" if updated == 1 else "ies")
    return updated
