"""Tests for `totp.py`.

Python port of the Go `auth` TOTP tests. Run with:
    pip install pytest
    pytest test_totp.py
"""

from datetime import datetime, timedelta, timezone

import pytest

from totp import (
    new_recovery_codes,
    new_totp_secret,
    totp_code,
    totp_provisioning_uri,
    verify_totp,
)

# RFC 6238 publishes test vectors for the seed "12345678901234567890"
# (ASCII). Base32 of that seed is GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ.
# The RFC's 8-digit values are truncated to the 6 digits we emit.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize(
    "unix_time, want",
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
    ],
)
def test_totp_matches_rfc6238_vectors(unix_time, want):
    t = datetime.fromtimestamp(unix_time, tz=timezone.utc)
    got = totp_code(RFC_SECRET, t)
    assert got == want, f"totp_code at {unix_time} = {got}, want {want}"


def test_verify_totp_accepts_current_code():
    secret = new_totp_secret()
    now = datetime.now(tz=timezone.utc)
    code = totp_code(secret, now)
    assert verify_totp(secret, code, now), "the freshly generated code should verify"


def test_verify_totp_tolerates_one_window_of_skew():
    secret = new_totp_secret()
    now = datetime.now(tz=timezone.utc)

    prev = totp_code(secret, now - timedelta(seconds=30))
    nxt = totp_code(secret, now + timedelta(seconds=30))
    assert verify_totp(secret, prev, now), "the previous window should be accepted (clock skew)"
    assert verify_totp(secret, nxt, now), "the next window should be accepted (clock skew)"

    # Two windows away must be rejected, otherwise the code lives too long.
    far = totp_code(secret, now - timedelta(seconds=90))
    assert not verify_totp(secret, far, now), "a code from 3 windows ago must be rejected"


def test_verify_totp_rejects_bad_input():
    secret = new_totp_secret()
    now = datetime.now(tz=timezone.utc)
    valid = totp_code(secret, now)

    for bad in ["", "12345", "1234567", "abcdef", "000000"]:
        if bad == valid:
            continue  # astronomically unlikely, but keep the test honest
        assert not verify_totp(secret, bad, now), f"code {bad!r} must not verify"

    # A different secret must not validate this code.
    other = new_totp_secret()
    assert not verify_totp(other, valid, now), "a code must not verify against a different secret"


def test_new_totp_secret_is_random_and_decodable():
    a = new_totp_secret()
    b = new_totp_secret()
    assert a != b, "secrets must not repeat"
    totp_code(a, datetime.now(tz=timezone.utc))  # must not raise


def test_provisioning_uri_contains_the_essentials():
    uri = totp_provisioning_uri("ABCD2345", "user@corp.com", "Flowie")
    for want in ["otpauth://totp/", "secret=ABCD2345", "issuer=Flowie", "digits=6", "period=30"]:
        assert want in uri, f"URI {uri!r} missing {want!r}"


def test_recovery_codes_are_unique():
    codes = new_recovery_codes(10)
    assert len(codes) == 10, f"got {len(codes)} codes, want 10"

    seen = set()
    for c in codes:
        assert c != "", "empty recovery code"
        assert c not in seen, f"duplicate recovery code {c!r}"
        seen.add(c)