"""Backup & Restore for NanoPulse.

Bundles the signed-in user's config file (which carries the chip /
hot-button contract metadata) plus the shared SQLite databases
(``users.db``, ``strategy_tiers.db``) into a single ``.npbak`` archive,
and restores from one.  See :class:`ConfigBackup`.
"""
from __future__ import annotations

from .service import ConfigBackup

__all__ = ["ConfigBackup"]
