"""
SQLite-backed contract universe storage.

Database : <project-root>/data/contracts.db

Table ``contracts`` mirrors the dict shape used throughout the app::

    conid, symbol, sec_type, exchange, currency,
    expiry, strike, right, local_symbol, multiplier

All columns are TEXT (IB returns everything as strings anyway).
No explicit PRIMARY KEY – saves always do a full DELETE + re-insert, so
duplicate-conid edge cases are never a problem.

Public API (intentionally identical to the old config_manager functions):

    load_contracts()  -> list[dict]
    save_contracts(contracts: list[dict]) -> None

One-time JSON migration
-----------------------
The first time ``load_contracts()`` is called on a fresh install the function
checks whether ``app_config.json`` still has a ``"contracts"`` key and, if so,
imports those rows into SQLite automatically.  The JSON file is left intact so
that a rollback is possible.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DB_PATH      = _PROJECT_ROOT / "data" / "contracts.db"
_JSON_PATH    = _PROJECT_ROOT / "app_config.json"

# Sort order used when returning contracts (mirrors the original JSON sort)
_SEC_ORDER: dict[str, int] = {"STK": 0, "FUT": 1, "OPT": 2}

# ── DDL ───────────────────────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS contracts (
    conid            TEXT NOT NULL DEFAULT '',
    symbol           TEXT NOT NULL DEFAULT '',
    sec_type         TEXT NOT NULL DEFAULT '',
    exchange         TEXT NOT NULL DEFAULT '',
    currency         TEXT NOT NULL DEFAULT '',
    expiry           TEXT NOT NULL DEFAULT '',
    strike           TEXT NOT NULL DEFAULT '',
    right            TEXT NOT NULL DEFAULT '',
    local_symbol     TEXT NOT NULL DEFAULT '',
    multiplier       TEXT NOT NULL DEFAULT '',
    primary_exchange TEXT NOT NULL DEFAULT '',
    name             TEXT NOT NULL DEFAULT ''
)
"""

_COLS = (
    "conid", "symbol", "sec_type", "exchange", "currency",
    "expiry", "strike", "right", "local_symbol", "multiplier",
    "primary_exchange", "name",
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _open() -> sqlite3.Connection:
    os.makedirs(_DB_PATH.parent, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)
    # One-time column upgrades for DBs created before these columns
    # existed (also covers DBs dropped in by Backup & Restore).
    cols = {row[1] for row in conn.execute("PRAGMA table_info(contracts)")}
    for col in ("primary_exchange", "name"):
        if col not in cols:
            conn.execute(f"ALTER TABLE contracts ADD COLUMN "
                         f"{col} TEXT NOT NULL DEFAULT ''")
    conn.commit()


def _row_tuple(c: dict[str, Any]) -> tuple:
    return (
        str(c.get("conid", "")),
        c.get("symbol", ""),
        c.get("sec_type", ""),
        c.get("exchange", ""),
        c.get("currency", ""),
        c.get("expiry", ""),
        str(c.get("strike", "")) if c.get("strike") else "",
        c.get("right", ""),
        c.get("local_symbol", ""),
        c.get("multiplier", ""),
        c.get("primary_exchange", ""),
        c.get("name", ""),
    )


def _bulk_insert(conn: sqlite3.Connection,
                 contracts: list[dict[str, Any]]) -> None:
    placeholders = ", ".join(["?"] * len(_COLS))
    conn.executemany(
        f"INSERT INTO contracts ({', '.join(_COLS)}) VALUES ({placeholders})",
        [_row_tuple(c) for c in contracts],
    )
    conn.commit()


# ── One-time JSON migration ───────────────────────────────────────────────────

def _cleanup_json() -> None:
    """Atomically remove the legacy ``"contracts"`` key from app_config.json.

    Safe to call repeatedly – does nothing when the key is already absent.
    """
    if not _JSON_PATH.exists():
        return
    try:
        with open(_JSON_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if "contracts" not in data:
            return                     # already clean – nothing to do
        data.pop("contracts")
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=_JSON_PATH.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, _JSON_PATH)
        except BaseException:
            os.unlink(tmp)
            raise
        log.info("contracts_db: removed 'contracts' key from %s", _JSON_PATH)
    except Exception:
        log.warning("contracts_db: could not clean up JSON 'contracts' key",
                    exc_info=True)


def _maybe_migrate_json(conn: sqlite3.Connection) -> None:
    """Import contracts from app_config.json when the SQLite table is empty,
    then remove the ``"contracts"`` key from JSON unconditionally.
    """
    count = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    if count == 0:
        # Table is empty – try to seed from the legacy JSON key
        if _JSON_PATH.exists():
            try:
                with open(_JSON_PATH, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                contracts: list[dict] = data.get("contracts", [])
                if contracts:
                    _bulk_insert(conn, contracts)
                    log.info(
                        "contracts_db: migrated %d contract(s) from %s",
                        len(contracts), _JSON_PATH,
                    )
            except Exception:
                log.exception(
                    "contracts_db: JSON migration failed – starting empty")

    # Always scrub the key from JSON so it is never written back
    _cleanup_json()


# ── Public API ────────────────────────────────────────────────────────────────

def load_contracts() -> list[dict[str, Any]]:
    """Return all saved contracts sorted by sec_type then symbol.

    Automatically creates the table on first call and runs the one-time
    JSON migration if the table is empty.
    """
    conn = _open()
    try:
        _ensure(conn)
        _maybe_migrate_json(conn)
        rows = conn.execute(
            f"SELECT {', '.join(_COLS)} FROM contracts"
        ).fetchall()
        contracts = [dict(r) for r in rows]
        contracts.sort(key=lambda c: (
            _SEC_ORDER.get(c.get("sec_type", ""), 9),
            c.get("symbol", ""),
        ))
        if contracts:
            log.info("contracts_db: loaded %d contract(s)", len(contracts))
        return contracts
    finally:
        conn.close()


def save_contracts(contracts: list[dict[str, Any]]) -> None:
    """Replace the entire contracts table with *contracts*.

    Performs a DELETE + bulk INSERT inside a single transaction so the table
    is never left in a partial state.
    """
    conn = _open()
    try:
        _ensure(conn)
        conn.execute("DELETE FROM contracts")
        _bulk_insert(conn, contracts)
        log.info("contracts_db: saved %d contract(s)", len(contracts))
    finally:
        conn.close()


