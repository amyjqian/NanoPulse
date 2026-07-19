"""Password hashing.

The implementation spec prefers ``argon2-cffi`` / ``bcrypt``, but neither is
installed in this environment and we don't want to add a native-build
dependency to a desktop app that must ``pip install`` cleanly.  We therefore
use PBKDF2-HMAC-SHA256 from the standard library — salted, high iteration
count, constant-time verify.  The stored format is self-describing so the
algorithm / cost can be upgraded later without a migration:

    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>

If ``argon2`` or ``bcrypt`` is added to the environment later, extend
``verify_password`` to dispatch on the algorithm prefix.
"""
from __future__ import annotations

import hashlib
import hmac
import os

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 240_000
_SALT_BYTES = 16
_DKLEN = 32


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    """Return a self-describing PBKDF2 hash string for *password*."""
    if not isinstance(password, str):
        raise TypeError("password must be a str")
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=_DKLEN
    )
    return f"{_ALGO}${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of *password* against a stored hash string.

    Returns ``False`` (never raises) for malformed / unknown-format hashes so
    a corrupt row can't crash the login flow.
    """
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False

    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=len(expected)
    )
    return hmac.compare_digest(dk, expected)
