"""Backup & Restore for NanoPulse.

Bundles the signed-in user's config file plus the shared SQLite databases
(``users.db``, ``contracts.db``, ``strategy_tiers.db``) into a single
``.npbak`` archive, and restores from one.  See :class:`ConfigBackup`.
"""
from __future__ import annotations

from .service import ConfigBackup

__all__ = ["ConfigBackup"]
