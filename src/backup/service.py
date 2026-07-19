"""ConfigBackup — create / validate / restore ``.npbak`` archives, plus
scheduled automatic backups.

A ``.npbak`` file is a ZIP archive so it can carry the binary SQLite databases
alongside the JSON config:

    manifest.json          {format, version, exported_at, app_version, username}
    config.json            the user's app_config-<username>.json contents
    db/users.db            accounts (pw_hash only — never plaintext)
    db/contracts.db        the contract universe
    db/strategy_tiers.db   per-symbol strategy tiers

Restore snapshots the current files first and rolls back on any failure, so a
bad archive can never leave the app half-restored.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from src.core import config_manager

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_DATA = _ROOT / "data"

# name in archive → path on disk (shared, per-app databases)
_DB_FILES: dict[str, Path] = {
    "users.db": _DATA / "users.db",
    "contracts.db": _DATA / "contracts.db",
    "strategy_tiers.db": _DATA / "strategy_tiers.db",
}

APP_VERSION = "3.2.1"


class ConfigBackup:
    FORMAT = "npbak"
    VERSION = 1

    def __init__(self, username: str | None = None):
        self._username = username

    # ── paths ────────────────────────────────────────────────────────────────

    def _config_path(self) -> Path:
        """The active user's config file (falls back to the base file)."""
        if self._username:
            return config_manager.user_config_path(self._username)
        return config_manager._default_path()

    def managed_dir(self) -> Path:
        """OS per-user app-data dir for automatic backups (predictable pruning)."""
        base = None
        try:
            from PySide6.QtCore import QStandardPaths
            loc = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppDataLocation)
            if loc:
                base = Path(loc)
        except Exception:
            base = None
        if base is None:                       # headless / no Qt paths
            base = _DATA
        d = base / "backups"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def default_filename(self, *, at: datetime | None = None) -> str:
        at = at or datetime.now()
        return f"nanopulse-config-{at:%Y-%m-%d-%H%M}.npbak"

    # ── export ───────────────────────────────────────────────────────────────

    def export(self, dest: str | Path, *, auto: bool = False) -> Path:
        """Write a ``.npbak`` archive to *dest* atomically (``*.tmp`` → rename).
        Records the backup timestamp in the user's preferences."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        manifest = {
            "format": self.FORMAT,
            "version": self.VERSION,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "app_version": APP_VERSION,
            "username": self._username or "",
        }
        config = self._read_config(self._config_path())

        fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("manifest.json", json.dumps(manifest, indent=2))
                z.writestr("config.json", json.dumps(config, indent=2))
                for name, path in _DB_FILES.items():
                    if path.exists():
                        z.write(path, f"db/{name}")
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        self._record_backup(auto=auto)
        log.info("Backup written: %s (auto=%s)", dest, auto)
        return dest

    # ── validate ─────────────────────────────────────────────────────────────

    def validate(self, path: str | Path) -> tuple[bool, str]:
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if "manifest.json" not in names:
                    return False, "Not a NanoPulse backup (manifest missing)."
                m = json.loads(z.read("manifest.json"))
                if m.get("format") != self.FORMAT:
                    return False, "Unrecognised backup format."
                if int(m.get("version", 0)) > self.VERSION:
                    return False, "Backup was created by a newer version of NanoPulse."
                if "config.json" not in names:
                    return False, "Backup is missing configuration data."
        except zipfile.BadZipFile:
            return False, "File is not a valid .npbak archive."
        except Exception as e:                                  # noqa: BLE001
            return False, f"Could not read backup: {e}"
        return True, "Valid backup."

    # ── restore ──────────────────────────────────────────────────────────────

    def restore(self, path: str | Path) -> None:
        """Validate → snapshot current files → apply → rollback on any error.

        The caller is responsible for reloading the app afterwards (users.db may
        now hold a different account set, so returning to the Login screen is
        the safe move)."""
        ok, msg = self.validate(path)
        if not ok:
            raise ValueError(msg)

        target_config = self._config_path()
        snapshot = self._snapshot(target_config)
        try:
            with zipfile.ZipFile(path) as z:
                members = set(z.namelist())
                config = json.loads(z.read("config.json"))
                config.pop("contracts", None)     # contracts live in the DB only
                self._atomic_write_bytes(
                    target_config,
                    json.dumps(config, indent=2, ensure_ascii=False).encode("utf-8"),
                )
                for name, db_path in _DB_FILES.items():
                    member = f"db/{name}"
                    if member in members:
                        self._atomic_write_bytes(db_path, z.read(member))
        except Exception:
            log.exception("Restore failed — rolling back")
            self._rollback(snapshot)
            raise
        finally:
            self._cleanup_snapshot(snapshot)
        log.warning("Restore applied from %s", path)

    # ── automatic backups ────────────────────────────────────────────────────

    def is_auto_enabled(self) -> bool:
        return bool(self._backup_prefs().get("auto", False))

    def set_auto_enabled(self, enabled: bool) -> None:
        b = self._backup_prefs()
        b["auto"] = bool(enabled)
        self._write_backup_prefs(b)
        log.info("Automatic backups %s", "enabled" if enabled else "disabled")

    def maybe_auto_backup(self, *, keep: int = 5, interval_hours: int = 24) -> bool:
        """Back up if auto is on and the last auto-backup is older than the
        interval (catch-up-on-launch semantics).  Returns True if it ran."""
        if not self.is_auto_enabled():
            return False
        last = self._parse_iso(self._backup_prefs().get("last_auto_at"))
        if last is not None and (datetime.now() - last) < timedelta(hours=interval_hours):
            return False
        try:
            dest = self.managed_dir() / self.default_filename()
            self.export(dest, auto=True)
            self._prune(keep)
            return True
        except Exception:                                       # never block the app
            log.exception("Automatic backup failed")
            return False

    def _prune(self, keep: int) -> None:
        files = sorted(
            self.managed_dir().glob("*.npbak"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[keep:]:
            try:
                old.unlink()
                log.info("Pruned old auto-backup: %s", old.name)
            except OSError:
                log.warning("Could not prune %s", old, exc_info=True)

    # ── display helpers ──────────────────────────────────────────────────────

    def last_backup_display(self) -> str:
        iso = self._backup_prefs().get("last_backup_at")
        dt = self._parse_iso(iso)
        return dt.strftime("%Y-%m-%d %H:%M") if dt else "Never"

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _read_config(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("contracts", None)
            return data
        except Exception:                                       # noqa: BLE001
            return {}

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _snapshot(self, config_path: Path) -> dict[str, Path]:
        """Copy current config + DBs into a temp dir; return {name: temp_path}."""
        snap_dir = Path(tempfile.mkdtemp(prefix="npbak_snap_"))
        snap: dict[str, Path] = {"__dir__": snap_dir}
        pairs = {"config": config_path, **_DB_FILES}
        for name, src in pairs.items():
            if src.exists():
                dst = snap_dir / f"{name}"
                shutil.copy2(src, dst)
                snap[name] = dst
        return snap

    def _rollback(self, snapshot: dict[str, Path]) -> None:
        config_path = self._config_path()
        targets = {"config": config_path, **_DB_FILES}
        for name, dst in targets.items():
            saved = snapshot.get(name)
            try:
                if saved is not None and saved.exists():
                    self._atomic_write_bytes(dst, saved.read_bytes())
            except Exception:                                   # noqa: BLE001
                log.exception("Rollback of %s failed", name)

    @staticmethod
    def _cleanup_snapshot(snapshot: dict[str, Path]) -> None:
        d = snapshot.get("__dir__")
        if d is not None:
            shutil.rmtree(d, ignore_errors=True)

    # ── preferences (per-user config's "backup" section) ──────────────────────

    @staticmethod
    def _backup_prefs() -> dict:
        return dict(config_manager.load_preferences().get("backup", {}))

    @staticmethod
    def _write_backup_prefs(b: dict) -> None:
        prefs = config_manager.load_preferences()
        prefs["backup"] = b
        config_manager.save_preferences(prefs)

    def _record_backup(self, *, auto: bool) -> None:
        b = self._backup_prefs()
        now = datetime.now().isoformat(timespec="seconds")
        b["last_backup_at"] = now
        if auto:
            b["last_auto_at"] = now
        self._write_backup_prefs(b)

    @staticmethod
    def _parse_iso(value) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None
