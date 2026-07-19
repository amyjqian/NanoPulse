"""AuthService — the single facade the UI talks to for authentication and
user management.  Owns the SQLite ``users`` table and PBKDF2 hashing; tracks
failed-attempt / lockout state in memory (keyed by lowercased username), which
the spec explicitly allows.

All public methods raise on the sad path with a specific exception subclass so
the UI can map each to the right message / screen without string-sniffing.
"""
from __future__ import annotations

import logging
import secrets
import string
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .db import connect
from .hashing import hash_password, verify_password
from .models import Role, User

log = logging.getLogger(__name__)

# Defaults — surface these as configurable constants (see the ``lock_threshold``
# / ``lock_minutes`` constructor args, wired from preferences at startup).
LOCK_THRESHOLD = 3     # failed attempts before lockout
LOCK_MINUTES = 5       # lockout duration


# ── Exceptions ────────────────────────────────────────────────────────────────

class AuthError(Exception):
    """Base class for all authentication failures."""


class InvalidCredentials(AuthError):
    """Unknown user or wrong password.  Carries the number of attempts left
    before lockout so the login page can show the countdown message."""

    def __init__(self, attempts_remaining: int):
        self.attempts_remaining = attempts_remaining
        super().__init__(
            f"Invalid credentials. {attempts_remaining} attempt"
            f"{'' if attempts_remaining == 1 else 's'} remaining before lockout."
        )


class AccountDisabled(AuthError):
    """A valid password was given for an account an admin has disabled."""

    def __init__(self) -> None:
        super().__init__("This account is disabled. Contact your administrator.")


class AccountLocked(AuthError):
    """Sign-in is temporarily disabled after too many failures.  Carries the
    number of seconds remaining so the Locked screen can seed its countdown."""

    def __init__(self, seconds_remaining: int):
        self.seconds_remaining = max(0, int(seconds_remaining))
        super().__init__("Account temporarily locked.")


# ── Service ───────────────────────────────────────────────────────────────────

class AuthService:
    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        lock_threshold: int = LOCK_THRESHOLD,
        lock_minutes: int = LOCK_MINUTES,
        now: Callable[[], datetime] | None = None,
    ):
        self._db_path = db_path
        self.lock_threshold = max(1, int(lock_threshold))
        self.lock_minutes = max(1, int(lock_minutes))
        self._now = now or datetime.now
        # username(lower) → {"attempts": int, "locked_until": datetime | None}
        self._lock_state: dict[str, dict] = {}

    # ── low-level row helpers ────────────────────────────────────────────────

    def _conn(self):
        return connect(self._db_path)

    @staticmethod
    def _to_user(row) -> User:
        return User(
            username=row["username"],
            display_name=row["display_name"],
            role=Role(row["role"]),
            enabled=bool(row["enabled"]),
            must_change_pw=bool(row["must_change_pw"]),
            created_at=row["created_at"],
            last_login=row["last_login"],
        )

    def _fetch_row(self, conn, username: str):
        return conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()

    # ── existence / listing ──────────────────────────────────────────────────

    def has_any_account(self) -> bool:
        conn = self._conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
        finally:
            conn.close()

    def get_user(self, username: str) -> User | None:
        conn = self._conn()
        try:
            row = self._fetch_row(conn, username)
            return self._to_user(row) if row else None
        finally:
            conn.close()

    def list_users(self) -> list[User]:
        """All users, admins first then alphabetical — the order the User
        Management table renders them in."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY "
                "(role = 'admin') DESC, username COLLATE NOCASE ASC"
            ).fetchall()
            return [self._to_user(r) for r in rows]
        finally:
            conn.close()

    # ── first-run setup ──────────────────────────────────────────────────────

    def create_admin(self, username: str, password: str) -> User:
        """Create the single administrator.  Only valid while the store is
        empty — refuses once any account exists (there is no UI path to a
        second admin)."""
        username = (username or "").strip()
        if not username:
            raise ValueError("Choose an admin username.")
        if not password:
            raise ValueError("Enter a password.")
        conn = self._conn()
        try:
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
                raise ValueError("An account already exists.")
            conn.execute(
                "INSERT INTO users (username, display_name, role, pw_hash, "
                "enabled, must_change_pw, created_at) "
                "VALUES (?, ?, 'admin', ?, 1, 0, ?)",
                (username, "Administrator", hash_password(password),
                 self._now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            log.info("Admin account %r created", username)
            return self.get_user(username)
        finally:
            conn.close()

    # ── authentication ───────────────────────────────────────────────────────

    def _lock(self, key: str) -> dict:
        return self._lock_state.setdefault(key, {"attempts": 0, "locked_until": None})

    def lock_seconds_remaining(self, username: str) -> int:
        """Seconds until *username* can sign in again (0 if not locked)."""
        st = self._lock_state.get((username or "").strip().lower())
        if not st or not st["locked_until"]:
            return 0
        return max(0, int((st["locked_until"] - self._now()).total_seconds()))

    def authenticate(self, username: str, password: str) -> User:
        username = (username or "").strip()
        if not username or not password:
            raise ValueError("Enter your username and password.")

        key = username.lower()
        st = self._lock(key)

        # Already locked?
        remaining = self.lock_seconds_remaining(username)
        if remaining > 0:
            raise AccountLocked(remaining)

        conn = self._conn()
        try:
            row = self._fetch_row(conn, username)
            if row is None or not verify_password(password, row["pw_hash"]):
                return self._register_failure(st)

            # Correct password — but a disabled account is still refused.
            if not bool(row["enabled"]):
                raise AccountDisabled()

            # Success — clear lockout, stamp last_login.
            self._lock_state.pop(key, None)
            stamp = self._now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE users SET last_login = ? WHERE username = ? COLLATE NOCASE",
                (stamp, username),
            )
            conn.commit()
            user = self._to_user(self._fetch_row(conn, username))
            log.info("User %r authenticated (%s)", username, user.role.value)
            return user
        finally:
            conn.close()

    def _register_failure(self, st: dict) -> User:
        """Bump the attempt counter and raise the right exception.  Never
        returns (the return-type annotation just keeps ``authenticate`` tidy)."""
        st["attempts"] += 1
        if st["attempts"] >= self.lock_threshold:
            st["locked_until"] = self._now() + timedelta(minutes=self.lock_minutes)
            raise AccountLocked(self.lock_minutes * 60)
        raise InvalidCredentials(self.lock_threshold - st["attempts"])

    # ── change password ──────────────────────────────────────────────────────

    def change_password(self, username: str, current: str, new: str) -> None:
        """Verify *current*, then set *new* and clear the must-change flag.
        Only validation is: current correct, new non-empty (complexity rules
        are intentionally not enforced, per spec)."""
        if not current:
            raise ValueError("Enter your current password.")
        if not new:
            raise ValueError("Enter a new password.")
        conn = self._conn()
        try:
            row = self._fetch_row(conn, username)
            if row is None or not verify_password(current, row["pw_hash"]):
                raise InvalidCredentials(0)  # message: "current password wrong"
            conn.execute(
                "UPDATE users SET pw_hash = ?, must_change_pw = 0 "
                "WHERE username = ? COLLATE NOCASE",
                (hash_password(new), username),
            )
            conn.commit()
            log.info("Password changed for %r", username)
        finally:
            conn.close()

    # ── user management (admin only) ─────────────────────────────────────────

    @staticmethod
    def _temp_password(length: int = 10) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def add_trader(
        self, username: str, display_name: str, password: str | None = None,
    ) -> tuple[User, str]:
        """Create a Trader (role is forced — there is no way to add a second
        admin).  Returns ``(user, temp_password)``; when *password* is omitted
        a random temp password is generated, ``must_change_pw`` is set, and the
        admin is shown the temp password to hand off."""
        username = (username or "").strip()
        if not username:
            raise ValueError("Username is required.")
        conn = self._conn()
        try:
            if self._fetch_row(conn, username) is not None:
                raise ValueError("That username already exists.")
            temp = password or self._temp_password()
            must_change = 1 if password is None else 0
            conn.execute(
                "INSERT INTO users (username, display_name, role, pw_hash, "
                "enabled, must_change_pw, created_at) "
                "VALUES (?, ?, 'trader', ?, 1, ?, ?)",
                (username, (display_name or "").strip() or username,
                 hash_password(temp), must_change,
                 self._now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
            log.info("Trader %r created (must_change_pw=%d)", username, must_change)
            return self.get_user(username), temp
        finally:
            conn.close()

    def set_enabled(self, username: str, enabled: bool) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE users SET enabled = ? WHERE username = ? COLLATE NOCASE",
                (1 if enabled else 0, username),
            )
            conn.commit()
            log.info("User %r enabled=%s", username, bool(enabled))
        finally:
            conn.close()

    def remove_user(self, username: str) -> None:
        """Delete *username*; refuses to remove an admin (admin is permanent)."""
        conn = self._conn()
        try:
            row = self._fetch_row(conn, username)
            if row is None:
                return
            if row["role"] == Role.ADMIN.value:
                raise ValueError("The admin account cannot be removed.")
            conn.execute(
                "DELETE FROM users WHERE username = ? COLLATE NOCASE", (username,)
            )
            conn.commit()
            self._lock_state.pop(username.strip().lower(), None)
            log.info("User %r removed", username)
        finally:
            conn.close()

    def reset_all(self) -> None:
        """Factory reset — delete every account and clear lockout state so the
        app returns to first-run setup.  Destructive; the caller must confirm."""
        conn = self._conn()
        try:
            conn.execute("DELETE FROM users")
            conn.commit()
            self._lock_state.clear()
            log.warning("All accounts deleted — reset to first-run state")
        finally:
            conn.close()

    def reset_password(self, username: str) -> str:
        """Admin action: reset a trader to a fresh temp password with
        ``must_change_pw`` set.  Returns the temp password to hand off."""
        temp = self._temp_password()
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE users SET pw_hash = ?, must_change_pw = 1 "
                "WHERE username = ? COLLATE NOCASE",
                (hash_password(temp), username),
            )
            conn.commit()
            self._lock_state.pop(username.strip().lower(), None)
            return temp
        finally:
            conn.close()
