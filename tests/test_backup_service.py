"""Tests for ConfigBackup — export / validate / restore / rollback / auto."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.backup.service import ConfigBackup
from src.core import config_manager


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated backup environment: temp config file + temp DBs."""
    data = tmp_path / "data"
    data.mkdir()
    db_files = {
        "users.db": data / "users.db",
        "strategy_tiers.db": data / "strategy_tiers.db",
    }
    for i, p in enumerate(db_files.values()):
        p.write_bytes(f"ORIGINAL-DB-{i}".encode())
    monkeypatch.setattr("src.backup.service._DB_FILES", db_files)
    # Legacy-archive compatibility path (contracts.db) — pointed at the
    # temp dir so no test can ever touch the real data/ files.
    restore_only = {"contracts.db": data / "contracts.db"}
    monkeypatch.setattr("src.backup.service._RESTORE_ONLY_DB_FILES",
                        restore_only)
    import src.core.instruments as instruments
    monkeypatch.setattr(instruments, "_LEGACY_DB", data / "contracts.db")

    cfg = tmp_path / "app_config-tester.json"
    cfg.write_text(json.dumps({"preferences": {"display": {"theme": "Dark"}},
                               "connections": [{"host": "h", "port": 4002}]}))
    monkeypatch.setattr(config_manager, "_active_path", cfg)
    # Point user_config_path at our temp file too.
    monkeypatch.setattr(config_manager, "user_config_path", lambda u: cfg)

    return {"data": data, "db_files": db_files, "cfg": cfg, "tmp": tmp_path}


def test_export_creates_valid_archive(env):
    svc = ConfigBackup("tester")
    dest = env["tmp"] / "b.npbak"
    svc.export(dest)
    assert dest.exists()
    ok, msg = svc.validate(dest)
    assert ok, msg
    with zipfile.ZipFile(dest) as z:
        names = set(z.namelist())
        assert {"manifest.json", "config.json", "db/users.db",
                "db/strategy_tiers.db"} <= names
        # Contract identity travels inside config.json now.
        assert "db/contracts.db" not in names
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["format"] == "npbak"
        assert manifest["username"] == "tester"


def test_export_atomic_no_tmp_left(env):
    svc = ConfigBackup("tester")
    dest = env["tmp"] / "b.npbak"
    svc.export(dest)
    assert not list(env["tmp"].glob("*.tmp"))


def test_validate_rejects_non_archive(env, tmp_path):
    bad = tmp_path / "bad.npbak"
    bad.write_text("not a zip")
    ok, msg = ConfigBackup("tester").validate(bad)
    assert not ok


def test_validate_rejects_wrong_format(env, tmp_path):
    p = tmp_path / "wrong.npbak"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": "other", "version": 1}))
        z.writestr("config.json", "{}")
    ok, _ = ConfigBackup("tester").validate(p)
    assert not ok


def test_validate_rejects_newer_version(env, tmp_path):
    p = tmp_path / "newer.npbak"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": "npbak", "version": 99}))
        z.writestr("config.json", "{}")
    ok, _ = ConfigBackup("tester").validate(p)
    assert not ok


def test_backup_includes_connections(env):
    # The seeded config has connections; they must survive export→restore.
    env["cfg"].write_text(json.dumps({
        "connections": [
            {"label": "Paper", "host": "127.0.0.1", "port": 4002, "client_id": 1},
            {"label": "Live", "host": "10.0.0.5", "port": 10404, "client_id": 2},
        ],
        "next_client_id": 105,
    }))
    svc = ConfigBackup("tester")
    dest = env["tmp"] / "b.npbak"
    svc.export(dest)
    # The archived config carries the connections verbatim.
    with zipfile.ZipFile(dest) as z:
        archived = json.loads(z.read("config.json"))
    assert [c["label"] for c in archived["connections"]] == ["Paper", "Live"]
    assert archived["connections"][1]["port"] == 10404
    assert archived["next_client_id"] == 105

    # Wipe connections, then restore brings them back.
    env["cfg"].write_text(json.dumps({"connections": []}))
    svc.restore(dest)
    restored = json.loads(env["cfg"].read_text())
    assert len(restored["connections"]) == 2
    assert restored["connections"][1]["port"] == 10404
    assert restored["next_client_id"] == 105


def test_restore_replaces_config_and_dbs(env):
    svc = ConfigBackup("tester")
    dest = env["tmp"] / "b.npbak"
    svc.export(dest)

    # Mutate everything after the backup.
    env["cfg"].write_text(json.dumps({"connections": [{"host": "CHANGED"}]}))
    for p in env["db_files"].values():
        p.write_bytes(b"MUTATED")

    svc.restore(dest)

    restored_cfg = json.loads(env["cfg"].read_text())
    assert restored_cfg["connections"][0]["host"] == "h"
    for i, p in enumerate(env["db_files"].values()):
        assert p.read_bytes() == f"ORIGINAL-DB-{i}".encode()


def test_restore_rolls_back_on_failure(env, tmp_path, monkeypatch):
    svc = ConfigBackup("tester")
    dest = env["tmp"] / "b.npbak"
    svc.export(dest)

    # Mutate current state so we can prove rollback restored it.
    env["cfg"].write_text(json.dumps({"connections": [{"host": "KEEP"}]}))
    env["db_files"]["users.db"].write_bytes(b"KEEP-USERS")

    # Force a failure midway through applying DBs.
    original = ConfigBackup._atomic_write_bytes   # staticmethod → plain function
    calls = {"n": 0}

    def flaky(path, data):
        calls["n"] += 1
        if calls["n"] == 2:            # 1=config, 2=first db → blow up
            raise OSError("disk full")
        original(path, data)

    monkeypatch.setattr(ConfigBackup, "_atomic_write_bytes", staticmethod(flaky))

    with pytest.raises(OSError):
        svc.restore(dest)

    # Rollback used the real writer, so state matches the pre-restore mutation.
    monkeypatch.undo()
    assert json.loads(env["cfg"].read_text())["connections"][0]["host"] == "KEEP"
    assert env["db_files"]["users.db"].read_bytes() == b"KEEP-USERS"


def test_auto_backup_disabled_by_default(env):
    svc = ConfigBackup("tester")
    assert svc.is_auto_enabled() is False
    assert svc.maybe_auto_backup() is False


def test_auto_backup_runs_and_prunes(env, monkeypatch):
    svc = ConfigBackup("tester")
    monkeypatch.setattr(svc, "managed_dir", lambda: env["tmp"] / "auto")
    (env["tmp"] / "auto").mkdir()
    svc.set_auto_enabled(True)

    assert svc.maybe_auto_backup(keep=3) is True
    # Second immediate call is skipped (within interval).
    assert svc.maybe_auto_backup(keep=3) is False

    # Seed extra files and prune to keep=3.
    for i in range(5):
        (env["tmp"] / "auto" / f"nanopulse-config-old{i}.npbak").write_bytes(b"x")
    svc._prune(keep=3)
    assert len(list((env["tmp"] / "auto").glob("*.npbak"))) == 3


def test_last_backup_display(env):
    svc = ConfigBackup("tester")
    assert svc.last_backup_display() == "Never"
    svc.export(env["tmp"] / "b.npbak")
    assert svc.last_backup_display() != "Never"


def test_legacy_archive_restores_contracts_db_and_migrates(env):
    """Archives exported before the contract DB was retired carry
    db/contracts.db and string chip lists — restore writes the file to
    disk and runs the migration so chips regain their metadata."""
    import sqlite3

    import src.core.instruments as instruments

    svc = ConfigBackup("tester")
    legacy = env["tmp"] / "legacy.npbak"
    cols = ("conid", "symbol", "sec_type", "exchange", "currency", "expiry",
            "strike", "right", "local_symbol", "multiplier",
            "primary_exchange", "name")
    dbf = env["tmp"] / "legacy_contracts.db"
    conn = sqlite3.connect(dbf)
    conn.execute(f"CREATE TABLE contracts ({', '.join(cols)})")
    conn.execute(
        f"INSERT INTO contracts VALUES ({', '.join('?' * len(cols))})",
        ("265598", "AAPL", "STK", "SMART", "USD", "", "", "", "AAPL", "",
         "ISLAND", "Apple Inc"))
    conn.commit()
    conn.close()
    with zipfile.ZipFile(legacy, "w") as z:
        z.writestr("manifest.json", json.dumps(
            {"format": "npbak", "version": 1, "username": "tester"}))
        z.writestr("config.json", json.dumps(
            {"order_entry_symbols": ["AAPL"], "connections": []}))
        z.write(dbf, "db/contracts.db")

    svc.restore(legacy)

    # The legacy DB landed on disk and the migration enriched the chip.
    assert (env["data"] / "contracts.db").exists()
    restored = json.loads(env["cfg"].read_text())
    (entry,) = restored["order_entry_symbols"]
    assert entry["symbol"] == "AAPL"
    assert entry["conid"] == "265598"
    assert entry["primary_exchange"] == "ISLAND"
