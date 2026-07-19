"""Role enum and the ``User`` dataclass.

These are pure data types with no Qt / DB coupling so they can be imported
from the worker thread, the UI, or a test without side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    """User roles.  ``str`` mix-in so the value serialises straight to the
    ``role`` TEXT column and compares equal to the raw string from SQLite."""

    ADMIN = "admin"
    TRADER = "trader"


@dataclass
class User:
    """A single account row (never carries the password hash into the UI)."""

    username: str
    display_name: str
    role: Role
    enabled: bool = True
    must_change_pw: bool = False
    created_at: str = ""
    last_login: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def can_trade(self) -> bool:
        """Only traders may place / manage trades.  Admins manage users."""
        return self.role == Role.TRADER and self.enabled


def require_role(user: User, role: Role) -> None:
    """Raise ``PermissionError`` unless *user* holds *role*.

    Call this at the top of any trade / user-management action handler —
    hidden UI alone is never sufficient enforcement.
    """
    if user is None or user.role != role:
        raise PermissionError(f"Requires {role.value} role")
