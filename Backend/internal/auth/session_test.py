"""Tests for `session_manager.py`.

Python port of the Go `auth` package tests. Run with:
    pip install pytest PyJWT
    pytest test_session_manager.py
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from session_manager import SessionManager, hash_token


def test_hash_token_is_stable_and_hides_the_token():
    token = "super-secret-token"
    h1 = hash_token(token)
    h2 = hash_token(token)

    assert h1 == h2, "hash_token must be deterministic"
    assert len(h1) == 64, f"hash length = {len(h1)}, want 64"  # sha256 hex
    assert token not in h1, "hash must not embed the raw token"
    assert hash_token("other-token") != h1, "different tokens must hash differently"


def test_issue_and_verify_round_trip():
    m = SessionManager("test-secret", timedelta(hours=1), secure=False)
    user_id = uuid4()

    tok = m.issue(user_id, "a@b.com", "Tester")
    claims = m.verify(tok)

    assert claims.subject == str(user_id), (
        f"subject = {claims.subject!r}, want {user_id}"
    )
    assert claims.email == "a@b.com", f"email = {claims.email!r}, want a@b.com"


def test_verify_rejects_wrong_secret():
    issuer = SessionManager("secret-a", timedelta(hours=1), secure=False)
    verifier = SessionManager("secret-b", timedelta(hours=1), secure=False)

    tok = issuer.issue(uuid4(), "a@b.com", "Tester")

    with pytest.raises(ValueError):
        verifier.verify(tok)


def test_verify_rejects_expired_token():
    m = SessionManager("test-secret", timedelta(hours=-1), secure=False)  # already expired
    tok = m.issue(uuid4(), "a@b.com", "Tester")

    with pytest.raises(ValueError):
        m.verify(tok)


def test_ttl_is_exposed():
    m = SessionManager("s", timedelta(minutes=42), secure=False)
    assert m.ttl == timedelta(minutes=42), f"ttl = {m.ttl}, want 42m"