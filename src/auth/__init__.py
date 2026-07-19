"""Authentication & user-management package for NanoPulse.

Exposes the role model, the ``User`` dataclass, the ``AuthService`` facade
and its exception hierarchy.  The service owns a small SQLite ``users`` table
(``data/users.db``) and hashes passwords with PBKDF2-HMAC-SHA256 (stdlib —
no third-party dependency required).
"""
from __future__ import annotations

from .models import Role, User
from .service import (
    AuthService,
    AuthError,
    InvalidCredentials,
    AccountDisabled,
    AccountLocked,
)

__all__ = [
    "Role",
    "User",
    "AuthService",
    "AuthError",
    "InvalidCredentials",
    "AccountDisabled",
    "AccountLocked",
]
