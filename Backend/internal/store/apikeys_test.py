"""
Unit tests for `api_key_store` — Python port of the Go tests in
`store/apikey_test.go`.

Run with: pytest test_api_key_store.py
"""

from api_key_store import (
    API_KEY_PREFIX,
    ResolvedKey,
    generate_api_key,
    hash_api_key,
    _visible_prefix,
)


def test_generate_api_key_shape():
    a = generate_api_key()

    assert a.startswith(API_KEY_PREFIX), (
        f"key {a!r} must carry the {API_KEY_PREFIX} prefix so it is recognisable"
    )
    assert len(a) >= 40, f"key length = {len(a)}, want >= 40 for 256 bits of entropy"

    b = generate_api_key()
    assert a != b, "generated keys must not repeat"


def test_hash_api_key_hides_the_secret():
    key = generate_api_key()
    h = hash_api_key(key)

    assert len(h) == 64, f"hash length = {len(h)}, want 64 (sha256 hex)"
    assert key not in h, "hash must not embed the plaintext key"
    assert hash_api_key(key) == h, "hashing must be deterministic"

    other = generate_api_key()
    assert hash_api_key(other) != h, "different keys must hash differently"


def test_visible_prefix_leaks_only_the_label():
    key = "flw_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    p = _visible_prefix(key)

    assert key.startswith(p), f"prefix {p!r} is not a prefix of the key"
    assert len(p) < len(key), "prefix must be shorter than the key — it is shown in the UI"
    assert len(p) == len(API_KEY_PREFIX) + 6, (
        f"prefix length = {len(p)}, want {len(API_KEY_PREFIX) + 6}"
    )

    # A short key must not raise or over-slice.
    got = _visible_prefix("flw_x")
    assert got == "flw_x", f"short key prefix = {got!r}, want the key itself"


def test_resolved_key_scopes():
    k = ResolvedKey(key_id=None, workspace_id=None, scopes=["read"])
    assert k.has_scope("read"), "read scope should be reported"
    assert not k.has_scope("write"), "write must not be granted by a read-only key"

    rw = ResolvedKey(key_id=None, workspace_id=None, scopes=["read", "write"])
    assert rw.has_scope("write"), "write scope should be reported"

    none = ResolvedKey(key_id=None, workspace_id=None, scopes=[])
    assert not none.has_scope("read"), "a key with no scopes grants nothing"