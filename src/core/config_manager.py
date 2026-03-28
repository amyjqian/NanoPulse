"""
Connection configuration persistence using JSON.

Loads / saves connection rows and the next_client_id counter
to ``app_config.json`` at the project root.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "app_config.json"

# Class-level auto-incrementing client ID (starting at 100).
_next_client_id: int = 100


def _default_path() -> Path:
    return _DEFAULT_PATH


# ── Public API ────────────────────────────────────────────────────────────────


def get_next_client_id() -> int:
    """Return the current next_client_id and bump the counter."""
    global _next_client_id
    cid = _next_client_id
    _next_client_id += 1
    return cid


def peek_next_client_id() -> int:
    """Return the current next_client_id without incrementing."""
    return _next_client_id


def load_connections(path: Path | str | None = None) -> list[dict[str, Any]]:
    """
    Silently load connection rows from *path* (defaults to ``app_config.json``).

    Returns an empty list if the file is missing or malformed.
    Also restores ``_next_client_id`` from the persisted value.
    """
    global _next_client_id
    p = Path(path) if path else _default_path()
    if not p.exists():
        log.debug("Config file not found: %s", p)
        return []
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _next_client_id = int(data.get("next_client_id", _next_client_id))
        rows = list(data.get("connections", []))
        log.info("Loaded %d connection(s) from %s  (next_client_id=%d)", len(rows), p, _next_client_id)
        return rows
    except Exception:
        log.exception("Failed to load config from %s", p)
        return []


def save_connections(
    rows: list[dict[str, Any]],
    path: Path | str | None = None,
) -> None:
    """
    Silently persist *rows* and the current ``next_client_id`` to JSON.
    Merges with existing data so other keys (e.g. aliases) are preserved.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["next_client_id"] = _next_client_id
    data["connections"] = rows
    _write_json(p, data)
    log.debug("Saved %d connection(s) to %s", len(rows), p)


def get_default_connection_label(path: Path | str | None = None) -> str:
    """Return the label of the connection marked as *default*, or ``""``."""
    rows = load_connections(path)
    for r in rows:
        if r.get("default"):
            return r.get("label", "")
    return ""


def get_labels_by_priority(path: Path | str | None = None) -> list[str]:
    """Return connection labels sorted by priority (1 = highest).

    Connections without a ``priority`` key default to ``99`` (lowest).
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    rows = list(data.get("connections", []))
    rows.sort(key=lambda r: int(r.get("priority", 99)))
    return [r.get("label", "") for r in rows if r.get("label")]


# ── Alias persistence ─────────────────────────────────────────────────────────


def load_aliases(path: Path | str | None = None) -> dict[str, str]:
    """
    Load the ``account_number → alias`` mapping from config JSON.

    Returns an empty dict if missing or malformed.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    aliases = data.get("aliases", {})
    if aliases:
        log.info("Loaded %d alias(es) from %s", len(aliases), p)
    return dict(aliases)


def save_aliases(
    aliases: dict[str, str],
    path: Path | str | None = None,
) -> None:
    """
    Persist the ``account_number → alias`` mapping into the config JSON.
    Merges with existing data so connections are preserved.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["aliases"] = aliases
    _write_json(p, data)
    log.debug("Saved %d alias(es) to %s", len(aliases), p)


# ── Contract universe persistence ─────────────────────────────────────────────


def load_contracts(path: Path | str | None = None) -> list[dict[str, Any]]:
    """
    Load the saved-contracts list from config JSON.

    Each dict: ``{conid, symbol, sec_type, exchange, currency, expiry,
    strike, right, local_symbol, multiplier}``.
    Returns an empty list if missing or malformed.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    contracts = list(data.get("contracts", []))
    if contracts:
        log.info("Loaded %d contract(s) from %s", len(contracts), p)
    return contracts


def save_contracts(
    contracts: list[dict[str, Any]],
    path: Path | str | None = None,
) -> None:
    """
    Persist *contracts* into the config JSON.
    Merges with existing data so connections/aliases are preserved.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["contracts"] = contracts
    _write_json(p, data)
    log.debug("Saved %d contract(s) to %s", len(contracts), p)


# ── Layout persistence ────────────────────────────────────────────────────────


def load_layout(path: Path | str | None = None) -> dict[str, Any]:
    """
    Load the ``layout`` dict from config JSON.

    Returns an empty dict if missing or malformed.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    return dict(data.get("layout", {}))


def save_layout(
    layout: dict[str, Any],
    path: Path | str | None = None,
) -> None:
    """
    Persist *layout* dict into the config JSON.
    Merges with existing data so connections/aliases/contracts are preserved.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["layout"] = layout
    _write_json(p, data)
    log.debug("Saved layout to %s", p)


# ── Order-entry symbol list persistence ───────────────────────────────────────


def load_order_entry_symbols(path: Path | str | None = None) -> list[str]:
    """Load the list of local_symbol strings pinned to the Order Entry panel."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    return list(data.get("order_entry_symbols", []))


def save_order_entry_symbols(
    symbols: list[str],
    path: Path | str | None = None,
) -> None:
    """Persist Order Entry symbol list. Merges with existing config."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["order_entry_symbols"] = symbols
    _write_json(p, data)
    log.debug("Saved %d order-entry symbol(s) to %s", len(symbols), p)


# ── Account-list panel state persistence ──────────────────────────────────────


def load_unchecked_accounts(path: Path | str | None = None) -> list[str]:
    """Load account IDs the user has un-checked in the Account List panel."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    return list(data.get("unchecked_accounts", []))


def save_unchecked_accounts(
    accounts: list[str],
    path: Path | str | None = None,
) -> None:
    """Persist unchecked account IDs. Merges with existing config."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["unchecked_accounts"] = accounts
    _write_json(p, data)
    log.debug("Saved %d unchecked account(s) to %s", len(accounts), p)


def load_hidden_accounts(path: Path | str | None = None) -> list[str]:
    """Load account IDs the user removed (×) from the Account List panel."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    return list(data.get("hidden_accounts", []))


def save_hidden_accounts(
    accounts: list[str],
    path: Path | str | None = None,
) -> None:
    """Persist hidden account IDs. Merges with existing config."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["hidden_accounts"] = accounts
    _write_json(p, data)
    log.debug("Saved %d hidden account(s) to %s", len(accounts), p)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _read_json(p: Path) -> dict[str, Any]:
    """Read existing JSON or return empty dict."""
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write_json(p: Path, data: dict[str, Any]) -> None:
    """Atomically write *data* as JSON (write to temp, then rename)."""
    import os
    import tempfile
    try:
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, p)
        except BaseException:
            os.unlink(tmp)
            raise
    except Exception:
        log.exception("Failed to write config to %s", p)

