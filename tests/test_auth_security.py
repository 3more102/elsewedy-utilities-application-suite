from __future__ import annotations

import hashlib

from app.auth import (
    LEGACY_PBKDF2_ROUNDS,
    PBKDF2_ALGORITHM,
    PBKDF2_ROUNDS,
    hash_password,
    password_needs_upgrade,
    verify_password,
    verify_password_with_upgrade,
)


def legacy_hash(password: str, salt: str = "legacy-salt") -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        LEGACY_PBKDF2_ROUNDS,
    ).hex()
    return f"{salt}${digest}"


def test_new_password_hash_is_versioned_and_verifiable():
    stored = hash_password("EUAS-Test@2026", salt="fixed-salt")

    algorithm, rounds, salt, digest = stored.split("$", 3)
    assert algorithm == PBKDF2_ALGORITHM
    assert int(rounds) == PBKDF2_ROUNDS
    assert salt == "fixed-salt"
    assert len(digest) == 64
    assert verify_password("EUAS-Test@2026", stored) is True
    assert verify_password("wrong-password", stored) is False
    assert password_needs_upgrade(stored) is False


def test_random_salt_prevents_identical_password_hashes():
    first = hash_password("EUAS-Test@2026")
    second = hash_password("EUAS-Test@2026")

    assert first != second
    assert verify_password("EUAS-Test@2026", first) is True
    assert verify_password("EUAS-Test@2026", second) is True


def test_legacy_hash_remains_compatible_and_requests_upgrade():
    stored = legacy_hash("Legacy@Password1")

    assert verify_password("Legacy@Password1", stored) is True
    assert verify_password("wrong-password", stored) is False
    assert password_needs_upgrade(stored) is True

    valid, replacement = verify_password_with_upgrade("Legacy@Password1", stored)
    assert valid is True
    assert replacement is not None
    assert replacement.startswith(f"{PBKDF2_ALGORITHM}${PBKDF2_ROUNDS}$")
    assert verify_password("Legacy@Password1", replacement) is True
    assert password_needs_upgrade(replacement) is False


def test_versioned_lower_work_factor_requests_upgrade():
    stored = hash_password("Lower@Rounds1", salt="fixed-salt", rounds=10_000)

    assert verify_password("Lower@Rounds1", stored) is True
    assert password_needs_upgrade(stored) is True

    valid, replacement = verify_password_with_upgrade("Lower@Rounds1", stored)
    assert valid is True
    assert replacement is not None
    assert replacement.startswith(f"{PBKDF2_ALGORITHM}${PBKDF2_ROUNDS}$")


def test_current_hash_does_not_request_replacement():
    stored = hash_password("Current@Password1", salt="current-salt")

    valid, replacement = verify_password_with_upgrade("Current@Password1", stored)
    assert valid is True
    assert replacement is None


def test_bad_password_never_returns_upgrade_hash():
    stored = legacy_hash("Correct@Password1")

    valid, replacement = verify_password_with_upgrade("Wrong@Password1", stored)
    assert valid is False
    assert replacement is None


def test_malformed_hashes_fail_closed_without_exceptions():
    malformed = [
        "",
        "not-a-hash",
        "salt$",
        "$digest",
        "pbkdf2_sha256$not-an-int$salt$digest",
        "pbkdf2_sha256$0$salt$digest",
        "unknown$600000$salt$digest",
        "pbkdf2_sha256$600000$$digest",
    ]

    for stored in malformed:
        assert verify_password("Anything@123", stored) is False
        assert password_needs_upgrade(stored) is True
