"""Overnight bracket exits waiting for their parent to fill — on disk.

Overnight a stock order routes to IBEOS, which refuses stop children, so
``IBWorkerThread`` sends the parent alone and places its TP / SL / trail
exits itself once the parent fills.  Until then the exits exist only in
NanoPulse.  This store keeps them on disk so that a parent which fills
while NanoPulse is closed, disconnected or mid-TWS-restart still gets
its exits: every connect reconciles the store against TWS's open orders
and fills (``IBWorkerThread._reconcile_pending_exits``).

Records are keyed ``{account}_{parent orderRef}`` — the same key the
worker uses for the parent.  Qt-free and lock-guarded: every worker
thread reads and writes it.  Writes are atomic (temp file + replace).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

_path = Path(__file__).resolve().parents[2] / "data" / "pending_exits.json"
_lock = threading.Lock()


def _read() -> dict:
    try:
        with open(_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    _path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_path.parent, prefix=".pending_exits.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, default=str)
        os.replace(tmp, _path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _json_safe(params: dict) -> dict:
    """Drop private (``_``-prefixed) keys and anything JSON can't hold."""
    out = {}
    for k, v in params.items():
        if str(k).startswith("_"):
            continue
        try:
            json.dumps(v)
        except (TypeError, ValueError):
            continue
        out[k] = v
    return out


def add(key: str, *, label: str, account: str, order_ref: str,
        params: dict, ref_price: float) -> None:
    rec = {"label": label, "account": account, "order_ref": order_ref,
           "params": _json_safe(params), "ref_price": float(ref_price),
           "created": time.time()}
    with _lock:
        data = _read()
        data[key] = rec
        _write(data)


def take(key: str) -> dict | None:
    """Remove and return *key*'s record — the caller then owns placing
    its exits, so a second event for the same parent finds nothing."""
    with _lock:
        data = _read()
        rec = data.pop(key, None)
        if rec is not None:
            _write(data)
        return rec


def has(key: str) -> bool:
    with _lock:
        return key in _read()


def records() -> dict:
    with _lock:
        return _read()
