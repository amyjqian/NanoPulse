"""One-time migration: legacy contracts.db → inline chip/hot-button metadata.

The migration is read-only on the DB, idempotent, and enriches only
entries that don't already carry a conid.  FUT roots with multiple saved
expiries resolve to the soonest non-expired one (front month); OPT chips
match rows stored under their raw OCC key.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import src.core.config_manager as cm
import src.core.instruments as instruments

_COLS = ("conid", "symbol", "sec_type", "exchange", "currency", "expiry",
         "strike", "right", "local_symbol", "multiplier",
         "primary_exchange", "name")

_OCC = "QQQ   260807P00670000"


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE contracts ({', '.join(_COLS)})")
    for r in rows:
        conn.execute(
            f"INSERT INTO contracts VALUES ({', '.join('?' * len(_COLS))})",
            tuple(str(r.get(c, "")) for c in _COLS))
    conn.commit()
    conn.close()


def _row(**over):
    base = {c: "" for c in _COLS}
    base.update(over)
    return base


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "app_config-test.json"
    db = tmp_path / "contracts.db"
    monkeypatch.setattr(cm, "_active_path", cfg)
    monkeypatch.setattr(instruments, "_LEGACY_DB", db)
    instruments.invalidate_cache()
    yield cfg, db
    instruments.invalidate_cache()


def test_enriches_bare_chips_and_hot_buttons(env):
    cfg, db = env
    _make_db(db, [
        _row(conid="265598", symbol="AAPL", sec_type="STK",
             exchange="SMART", currency="USD", local_symbol="AAPL",
             primary_exchange="ISLAND"),
        _row(conid="793356225", symbol="MNQ", sec_type="FUT",
             exchange="CME", currency="USD", expiry="29990320",
             local_symbol="MNQH9", multiplier="2"),
    ])
    cfg.write_text(json.dumps({
        "order_entry_symbols": ["AAPL", "MNQ", "SPCX"],
        "book_trader_symbols": ["MNQ"],
        "hot_buttons": [{"side": "BUY", "symbol": "AAPL"}],
    }), encoding="utf-8")
    instruments.invalidate_cache()

    updated = instruments.migrate_active_config_from_db()
    assert updated == 4          # AAPL(oe) MNQ(oe) MNQ(bt) AAPL(hot)

    oe = cm.load_order_entry_entries()
    assert oe[0]["conid"] == "265598"
    assert oe[1]["conid"] == "793356225"
    assert "conid" not in oe[2]  # SPCX has no DB row — stays bare
    assert cm.load_book_trader_entries()[0]["local_symbol"] == "MNQH9"
    assert cm.load_hot_buttons()[0]["contract"]["conid"] == "265598"

    # Idempotent: second run touches nothing.
    assert instruments.migrate_active_config_from_db() == 0


def test_fut_multi_expiry_picks_front_month(env):
    cfg, db = env
    _make_db(db, [
        _row(conid="1", symbol="NQ", sec_type="FUT", expiry="20200619",
             local_symbol="NQM0"),                       # expired
        _row(conid="2", symbol="NQ", sec_type="FUT", expiry="29990320",
             local_symbol="NQH9"),                       # soonest future
        _row(conid="3", symbol="NQ", sec_type="FUT", expiry="29990619",
             local_symbol="NQM9"),                       # later future
    ])
    cfg.write_text(json.dumps({"order_entry_symbols": ["NQ"]}),
                   encoding="utf-8")
    instruments.invalidate_cache()
    instruments.migrate_active_config_from_db()
    e = cm.load_order_entry_entries()[0]
    assert e["conid"] == "2" and e["local_symbol"] == "NQH9"


def test_opt_chip_matches_occ_symbol_row(env):
    cfg, db = env
    _make_db(db, [
        _row(conid="896508221", symbol=_OCC, sec_type="OPT",
             exchange="SMART", currency="USD", multiplier="100",
             local_symbol=_OCC),
    ])
    cfg.write_text(json.dumps({"book_trader_symbols": [_OCC]}),
                   encoding="utf-8")
    instruments.invalidate_cache()
    instruments.migrate_active_config_from_db()
    e = cm.load_book_trader_entries()[0]
    assert e["conid"] == "896508221" and e["sec_type"] == "OPT"


def test_missing_db_is_noop(env):
    cfg, _db = env               # db file never created
    cfg.write_text(json.dumps({"order_entry_symbols": ["AAPL"]}),
                   encoding="utf-8")
    instruments.invalidate_cache()
    assert instruments.migrate_active_config_from_db() == 0
    assert cm.load_order_entry_entries() == [{"symbol": "AAPL"}]
