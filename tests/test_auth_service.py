"""Unit tests for the auth core (no Qt).  Uses a temp DB per test and a
frozen clock so lockout timing is deterministic.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.auth import (
    AuthService,
    Role,
    AuthError,
    InvalidCredentials,
    AccountDisabled,
    AccountLocked,
)


class Clock:
    """Manually-advanceable clock injected into AuthService."""

    def __init__(self):
        self.t = datetime(2026, 7, 18, 9, 0, 0)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


@pytest.fixture
def svc(tmp_path):
    return AuthService(
        tmp_path / "users.db", lock_threshold=3, lock_minutes=5, now=Clock(),
    )


@pytest.fixture
def clock(svc):
    return svc._now


# ── setup / existence ─────────────────────────────────────────────────────────

def test_empty_store(svc):
    assert svc.has_any_account() is False


def test_create_admin(svc):
    user = svc.create_admin("admin", "supersecret")
    assert user.role is Role.ADMIN
    assert user.is_admin and not user.can_trade
    assert svc.has_any_account() is True


def test_create_admin_allows_short_password(svc):
    # The 8-char minimum was removed — any non-empty password is accepted.
    user = svc.create_admin("admin", "abc")
    assert user.role is Role.ADMIN
    assert svc.authenticate("admin", "abc").username == "admin"


def test_create_admin_rejects_empty_password(svc):
    with pytest.raises(ValueError):
        svc.create_admin("admin", "")


def test_second_admin_refused(svc):
    svc.create_admin("admin", "supersecret")
    with pytest.raises(ValueError):
        svc.create_admin("boss", "supersecret2")


# ── authentication ────────────────────────────────────────────────────────────

def test_login_success(svc):
    svc.create_admin("admin", "supersecret")
    user = svc.authenticate("admin", "supersecret")
    assert user.username == "admin"
    assert user.last_login  # stamped


def test_login_case_insensitive_username(svc):
    svc.create_admin("Admin", "supersecret")
    assert svc.authenticate("admin", "supersecret").username == "Admin"


def test_login_empty_fields(svc):
    svc.create_admin("admin", "supersecret")
    with pytest.raises(ValueError):
        svc.authenticate("", "")


def test_wrong_password_counts_down(svc):
    svc.create_admin("admin", "supersecret")
    with pytest.raises(InvalidCredentials) as e1:
        svc.authenticate("admin", "nope")
    assert e1.value.attempts_remaining == 2
    with pytest.raises(InvalidCredentials) as e2:
        svc.authenticate("admin", "nope")
    assert e2.value.attempts_remaining == 1


def test_lockout_after_threshold(svc):
    svc.create_admin("admin", "supersecret")
    for _ in range(2):
        with pytest.raises(InvalidCredentials):
            svc.authenticate("admin", "nope")
    with pytest.raises(AccountLocked) as e:
        svc.authenticate("admin", "nope")
    assert e.value.seconds_remaining == 300
    # Even a correct password is refused while locked.
    with pytest.raises(AccountLocked):
        svc.authenticate("admin", "supersecret")


def test_lockout_expires(svc, clock):
    svc.create_admin("admin", "supersecret")
    for _ in range(3):
        with pytest.raises(AuthError):
            svc.authenticate("admin", "nope")
    clock.advance(minutes=5, seconds=1)
    # Unlocked again — correct password now works.
    assert svc.authenticate("admin", "supersecret").username == "admin"


def test_disabled_account_refused(svc):
    svc.create_admin("admin", "supersecret")
    svc.add_trader("t1", "Trader One", password="traderpass")
    svc.set_enabled("t1", False)
    with pytest.raises(AccountDisabled):
        svc.authenticate("t1", "traderpass")


# ── change password ───────────────────────────────────────────────────────────

def test_change_password(svc):
    svc.create_admin("admin", "supersecret")
    svc.change_password("admin", "supersecret", "newsecret")
    assert svc.authenticate("admin", "newsecret").username == "admin"
    with pytest.raises(AuthError):
        svc.authenticate("admin", "supersecret")


def test_change_password_wrong_current(svc):
    svc.create_admin("admin", "supersecret")
    with pytest.raises(InvalidCredentials):
        svc.change_password("admin", "wrong", "newsecret")


def test_change_password_clears_must_change(svc):
    svc.create_admin("admin", "supersecret")
    user, temp = svc.add_trader("t1", "Trader One")  # temp → must_change_pw=1
    assert user.must_change_pw is True
    svc.change_password("t1", temp, "chosenpass")
    assert svc.get_user("t1").must_change_pw is False


# ── user management ───────────────────────────────────────────────────────────

def test_add_trader_forces_role(svc):
    svc.create_admin("admin", "supersecret")
    user, temp = svc.add_trader("j.walker", "Jordan Walker")
    assert user.role is Role.TRADER
    assert user.can_trade
    assert temp  # a temp password was generated
    # New trader must change password on first login.
    assert user.must_change_pw is True


def test_add_trader_duplicate_username(svc):
    svc.create_admin("admin", "supersecret")
    svc.add_trader("j.walker", "Jordan Walker")
    with pytest.raises(ValueError):
        svc.add_trader("J.Walker", "Dup")  # case-insensitive clash


def test_remove_trader(svc):
    svc.create_admin("admin", "supersecret")
    svc.add_trader("t1", "Trader One")
    svc.remove_user("t1")
    assert svc.get_user("t1") is None


def test_remove_admin_refused(svc):
    svc.create_admin("admin", "supersecret")
    with pytest.raises(ValueError):
        svc.remove_user("admin")


def test_list_users_admin_first(svc):
    svc.create_admin("zadmin", "supersecret")
    svc.add_trader("a.trader", "A")
    svc.add_trader("b.trader", "B")
    users = svc.list_users()
    assert users[0].role is Role.ADMIN
    assert [u.username for u in users[1:]] == ["a.trader", "b.trader"]


def test_password_never_plaintext(svc):
    svc.create_admin("admin", "supersecret")
    conn = svc._conn()
    try:
        row = conn.execute("SELECT pw_hash FROM users").fetchone()
    finally:
        conn.close()
    assert "supersecret" not in row["pw_hash"]
    assert row["pw_hash"].startswith("pbkdf2_sha256$")
