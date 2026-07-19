"""SQLite storage for the ``users`` table.

Database : ``<project-root>/data/users.db`` (kept alongside ``contracts.db``
and ``strategy_tiers.db`` for consistency with the rest of the app; the file
is gitignored because it holds password hashes).

Every column is created exactly as described in the implementation spec.
Connections are opened per-call and closed by the caller — the table is tiny
and only ever touched from the UI thread, so no pooling is needed.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = _PROJECT_ROOT / "data" / "users.db"

_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT UNIQUE NOT NULL,
    display_name   TEXT NOT NULL,
    role           TEXT NOT NULL CHECK (role IN ('admin', 'trader')),
    pw_hash        TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    must_change_pw INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    last_login     TEXT
)
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (creating parent dirs as needed) and return a connection with the
    ``users`` table guaranteed to exist and ``row_factory`` set to ``Row``."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    os.makedirs(path.parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(_DDL)
    conn.commit()
    return conn
