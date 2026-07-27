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

# Per-user config selection.  When a user signs in, ``set_active_config`` points
# this at ``app_config-<username>.json`` so every load/save helper below (all of
# which fall back to ``_default_path()``) reads and writes that user's own file.
# ``None`` = the shared base ``app_config.json`` (used before login / as the
# seed template for a brand-new user's config).
_active_path: Path | None = None


def _default_path() -> Path:
    return _active_path if _active_path is not None else _DEFAULT_PATH


def _sanitize_username(username: str) -> str:
    """Filesystem-safe filename fragment for *username*."""
    cleaned = "".join(
        c if (c.isalnum() or c in "-_.") else "_" for c in username.strip()
    )
    return cleaned or "user"


def user_config_path(username: str) -> Path:
    """Return the config-file path for *username* (not necessarily existing)."""
    return _DEFAULT_PATH.parent / f"app_config-{_sanitize_username(username)}.json"


def set_active_config(username: str | None) -> None:
    """Switch the active config file to *username*'s own ``app_config-*.json``.

    Pass ``None`` (e.g. on sign-out) to fall back to the shared base file.
    A brand-new user's file is seeded from the base ``app_config.json`` so they
    start with the default connections/layout rather than an empty app.  The
    per-user ``next_client_id`` is reloaded from the newly-active file.
    """
    global _active_path, _next_client_id
    if not username:
        _active_path = None
    else:
        target = user_config_path(username)
        if not target.exists() and _DEFAULT_PATH.exists():
            try:
                _write_json(target, _read_json(_DEFAULT_PATH))  # seed from base
                log.info("Seeded new config %s from base template", target.name)
            except Exception:
                log.exception("Failed to seed config for user %r", username)
        _active_path = target
    # Re-read next_client_id from whichever file is now active.
    _next_client_id = 100
    load_connections()
    log.info("Active config → %s", _default_path())


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
        log.info("Loaded %d connection(s) from %s  (next_client_id=%d)",
                 len(rows), p, _next_client_id)
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
    Load the saved-contracts list from SQLite (``data/contracts.db``).

    The *path* argument is accepted for backward-compatibility but ignored;
    contracts are always read from the SQLite database.  On first run the
    database is auto-created and any contracts stored in ``app_config.json``
    are migrated automatically.

    Each dict: ``{conid, symbol, sec_type, exchange, currency, expiry,
    strike, right, local_symbol, multiplier}``.
    """
    from src.core.contracts_db import load_contracts as _load
    return _load()


def save_contracts(
    contracts: list[dict[str, Any]],
    path: Path | str | None = None,
) -> None:
    """
    Persist *contracts* to SQLite (``data/contracts.db``).

    The *path* argument is accepted for backward-compatibility but ignored.
    """
    from src.core.contracts_db import save_contracts as _save
    _save(contracts)


def remove_stock_contract_if_unused(
    symbol: str, path: Path | str | None = None,
) -> bool:
    """Delete *symbol*'s STK contract(s) from the contracts DB after a
    chip-bar removal — but only when nothing else still references the
    symbol:

    * the Order Entry chip list,
    * the Book Trader chip list,
    * any enabled hot button.

    FUT/OPT contracts are never touched: those are curated on the
    Contracts page (with expiries picked by hand) and removing a chip
    shouldn't destroy them.  Reloads ReferenceData — which emits
    ``contracts_changed`` — when a row is deleted.  Returns True if
    something was deleted.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return False
    used = {s.upper() for s in load_order_entry_symbols(path)}
    used |= {s.upper() for s in load_book_trader_symbols(path)}
    for b in load_hot_buttons(path):
        if not b.get("disabled"):
            used.add(b.get("symbol", "").upper())
    if sym in used:
        return False
    contracts = load_contracts()
    kept = [c for c in contracts
            if c.get("sec_type") != "STK"
            or c.get("symbol", "").upper() != sym]
    if len(kept) == len(contracts):
        return False
    save_contracts(kept)
    log.info("Removed STK contract %s from contracts DB "
             "(chip removed, no remaining references)", sym)
    from src.core.reference_data import get_instance as _rd
    _rd().reload_contracts()
    return True


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
    """Load the list of local_symbol strings pinned to the Order Entry panel.

    Falls back to ``["MNQ"]`` on first launch so the panel has a sensible
    default without requiring the user to add a symbol manually.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    saved = list(data.get("order_entry_symbols", []))
    return saved if saved else ["MNQ"]


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


def load_active_order_entry_symbol(path: Path | str | None = None) -> str:
    """Return the last-active symbol in the Order Entry panel, or ``""``."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    return str(data.get("order_entry_active_symbol", ""))


def save_active_order_entry_symbol(
    symbol: str,
    path: Path | str | None = None,
) -> None:
    """Persist the currently-active Order Entry symbol."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["order_entry_active_symbol"] = symbol
    _write_json(p, data)
    log.debug("Saved active order-entry symbol %r to %s", symbol, p)


# ── Book Trader symbol list persistence ───────────────────────────────────────


def load_book_trader_symbols(path: Path | str | None = None) -> list[str]:
    """Load the list of symbols pinned to the Book Trader panel.

    The Book Trader panel keeps its own chip list independent of the
    Trade (Order Entry) panel.  On first launch (no ``book_trader_symbols``
    key in config), seed from the existing Order Entry list so upgrading
    users don't lose their chips; if that's also empty, default to ``["MNQ"]``.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    if "book_trader_symbols" in data:
        return list(data.get("book_trader_symbols", []))
    seeded = list(data.get("order_entry_symbols", []))
    return seeded if seeded else ["MNQ"]


def save_book_trader_symbols(
    symbols: list[str],
    path: Path | str | None = None,
) -> None:
    """Persist Book Trader symbol list. Merges with existing config."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["book_trader_symbols"] = symbols
    _write_json(p, data)
    log.debug("Saved %d book-trader symbol(s) to %s", len(symbols), p)


def load_book_trader_active_symbol(path: Path | str | None = None) -> str:
    """Return the last-active symbol in the Book Trader panel, or ``""``."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    return str(data.get("book_trader_active_symbol", ""))


def save_book_trader_active_symbol(
    symbol: str,
    path: Path | str | None = None,
) -> None:
    """Persist the currently-active Book Trader symbol."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["book_trader_active_symbol"] = symbol
    _write_json(p, data)
    log.debug("Saved active book-trader symbol %r to %s", symbol, p)


def load_book_trader_lock_state(path: Path | str | None = None) -> bool:
    """Return the persisted Book Trader ladder-lock state.

    Defaults to ``False`` (unlocked) on first launch so the user is not
    surprised by auto-scrolling on a fresh install.  Once the user
    toggles the lock, ``save_book_trader_lock_state`` persists their
    choice so subsequent launches respect it.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    return bool(data.get("book_trader_lock_state", False))


def save_book_trader_lock_state(
    locked: bool,
    path: Path | str | None = None,
) -> None:
    """Persist the Book Trader ladder-lock state."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["book_trader_lock_state"] = bool(locked)
    _write_json(p, data)
    log.debug("Saved book-trader lock state %s to %s", bool(locked), p)


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


def load_account_groups(
    path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Load named account groups for the Accounts panel's quick-select
    pills.  Returns an ordered list of ``{"name": str, "accounts":
    list[str], "selected": list[str] | None}`` dicts.  ``selected`` is
    the per-group checked subset (absent in legacy configs — the UI
    migrates those to ``selected = accounts``).  Invalid entries
    (non-string names, empty account lists, duplicate names) are
    dropped silently — the UI rebuilds persistence on every mutation
    so a corrupt entry won't round-trip."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    raw = data.get("account_groups", [])
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        accounts = entry.get("accounts")
        if not isinstance(name, str) or not name.strip():
            continue
        if name in seen:
            continue
        if not isinstance(accounts, list):
            continue
        acct_list = [str(a) for a in accounts if isinstance(a, str) and a]
        if not acct_list:
            continue
        out: dict[str, Any] = {"name": name, "accounts": acct_list}
        sel = entry.get("selected")
        if isinstance(sel, list):
            # Keep only strings that are actual members.
            out["selected"] = [
                str(a) for a in sel
                if isinstance(a, str) and a in acct_list
            ]
        cleaned.append(out)
        seen.add(name)
    return cleaned


def save_account_groups(
    groups: list[dict[str, Any]],
    path: Path | str | None = None,
) -> None:
    """Persist named account groups.  Order is preserved — the caller
    controls pill order in the Accounts panel.  Each group's
    ``selected`` list (the per-group checked subset) is written when
    present so check-state survives a restart."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    serialised: list[dict[str, Any]] = []
    for g in groups:
        if not (isinstance(g, dict) and g.get("name")):
            continue
        entry: dict[str, Any] = {
            "name": g["name"],
            "accounts": list(g.get("accounts", [])),
        }
        if "selected" in g and isinstance(g["selected"], list):
            entry["selected"] = list(g["selected"])
        serialised.append(entry)
    data["account_groups"] = serialised
    _write_json(p, data)
    log.debug("Saved %d account group(s) to %s", len(serialised), p)


def load_last_account_group(path: Path | str | None = None) -> str:
    """Return the name of the last-active account group, or ``""``
    when nothing has been persisted yet.  ``"All"`` and ``"None"`` are
    valid values (the built-in quick-select pills) alongside any
    user-defined group name."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    name = data.get("last_account_group", "")
    return str(name) if isinstance(name, str) else ""


def save_last_account_group(
    name: str | None,
    path: Path | str | None = None,
) -> None:
    """Persist the currently-active account group so the next launch
    can restore it.  ``None`` or ``""`` clears the preference."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["last_account_group"] = str(name) if name else ""
    _write_json(p, data)
    log.debug("Saved last active account group %r to %s", name, p)


# ── Hot buttons persistence ──────────────────────────────────────────────────


def _normalise_hot_button_leg(leg: Any) -> dict[str, Any]:
    """Coerce a stored bracket leg into the shape the modal expects:
    ``{"on": bool, "v": float, "u": "abs"}``.  Legacy ``ticks``/``pct``
    units are migrated to ``"abs"`` because the only supported unit is
    now absolute dollar offset.
    """
    if not isinstance(leg, dict):
        return {"on": False, "v": 80.0, "u": "abs"}
    try:
        v = float(leg.get("v", 80.0) or 80.0)
    except (TypeError, ValueError):
        v = 80.0
    return {
        "on": bool(leg.get("on", False)),
        "v": v,
        "u": "abs",
    }


def _normalise_hot_button(entry: Any) -> dict[str, Any] | None:
    """Coerce a stored hot-button entry into the canonical schema.
    Returns ``None`` for invalid entries (caller drops them silently —
    persistence will overwrite the corrupt slot on next save)."""
    if not isinstance(entry, dict):
        return None
    side = entry.get("side", "BUY")
    if side not in ("BUY", "SELL", "FLATTEN", "CANCEL"):
        return None
    ord_type = entry.get("ord_type", "LMT")
    if ord_type not in ("LMT", "MKT"):
        ord_type = "LMT"
    anchor = entry.get("anchor", "BID")
    if anchor not in ("BID", "ASK", "MID"):
        anchor = "BID"
    try:
        offset = int(entry.get("offset", 0) or 0)
    except (TypeError, ValueError):
        offset = 0
    return {
        "side": side,
        "group": str(entry.get("group", "__current__") or "__current__"),
        "symbol": str(entry.get("symbol", "") or ""),
        "qty": str(entry.get("qty", "") or ""),
        "ord_type": ord_type,
        "anchor": anchor,
        "offset": offset,
        "label": str(entry.get("label", "") or ""),
        "tp": _normalise_hot_button_leg(entry.get("tp")),
        "sl": _normalise_hot_button_leg(entry.get("sl")),
        # Set when a saved contract referenced by this button is deleted
        # without a replacement.  Disabled buttons render greyed-out and
        # ignore clicks until the user re-edits them or a replacement
        # contract is added.
        "disabled": bool(entry.get("disabled", False)),
    }


def load_hot_buttons(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Load the OrderEntry hot-button list.  Returns an ordered list
    of normalised hot-button dicts (see
    :func:`_normalise_hot_button`).  Invalid entries are dropped.
    Returns an empty list when nothing has been persisted yet — the
    UI starts with just the trailing "+" placeholder in that case.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    raw = data.get("hot_buttons", [])
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for entry in raw:
        normed = _normalise_hot_button(entry)
        if normed is not None:
            cleaned.append(normed)
    return cleaned


def save_hot_buttons(
    buttons: list[dict[str, Any]],
    path: Path | str | None = None,
) -> None:
    """Persist the OrderEntry hot-button list, preserving order.
    Each entry is renormalised before write so any stale fields from
    the in-memory dict don't leak to disk."""
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    serialised: list[dict[str, Any]] = []
    for entry in buttons:
        normed = _normalise_hot_button(entry)
        if normed is not None:
            serialised.append(normed)
    data["hot_buttons"] = serialised
    _write_json(p, data)
    log.debug("Saved %d hot button(s) to %s", len(serialised), p)


# ── Preferences persistence ──────────────────────────────────────────────────


def load_preferences(path: Path | str | None = None) -> dict[str, Any]:
    """Load the ``preferences`` dict from config JSON.

    Returns an empty dict if missing or malformed.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    return dict(data.get("preferences", {}))


def save_preferences(
    prefs: dict[str, Any],
    path: Path | str | None = None,
) -> None:
    """Persist *prefs* dict into the config JSON.

    Merges with existing data so other keys are preserved.
    """
    p = Path(path) if path else _default_path()
    data = _read_json(p)
    data["preferences"] = prefs
    _write_json(p, data)
    log.debug("Saved preferences to %s", p)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _read_json(p: Path) -> dict[str, Any]:
    """Read existing JSON or return empty dict.

    The ``"contracts"`` key is stripped on read: contracts are now stored
    exclusively in SQLite (``data/contracts.db``) and must never be written
    back to the JSON file by any of the save helpers below.
    """
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.pop("contracts", None)   # owned by contracts_db – not JSON
        return data
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
