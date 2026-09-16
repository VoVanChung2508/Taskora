"""TOTP implementation (RFC 6238) used for two-factor authentication.

Python port of the Go `auth` TOTP code. Written directly against the
standard library (`hmac`, `hashlib`, `secrets`, `base64`, `struct`,
`urllib.parse`) so no third-party dependency is needed — same spirit as
the Go original.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import datetime, timedelta, timezone
from typing import List
from urllib.parse import quote, urlencode

TOTP_DIGITS = 6
TOTP_PERIOD = timedelta(seconds=30)
# Accept the neighbouring windows so a slightly skewed clock still works.
TOTP_SKEW_WINDOWS = 1


def _b32_encode(data: bytes) -> str:
    """Base32-encode without padding (mirrors Go's `base32.NoPadding`)."""
    return base64.b32encode(data).decode("ascii").rstrip("=")


def _b32_decode(s: str) -> bytes:
    """Base32-decode a string that may be missing its padding."""
    s = s.upper()
    padding = "=" * (-len(s) % 8)
    return base64.b32decode(s + padding)


def new_totp_secret() -> str:
    """Return a fresh base32 secret suitable for authenticator apps."""
    buf = secrets.token_bytes(20)  # 160-bit, as recommended by RFC 4226
    return _b32_encode(buf)


def totp_code(secret: str, t: datetime) -> str:
    """Compute the code for a secret at a point in time."""
    try:
        key = _b32_decode(secret.strip())
    except Exception as exc:
        raise ValueError(f"invalid secret: {exc}") from exc

    counter = int(t.timestamp()) // int(TOTP_PERIOD.total_seconds())

    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()

    # Dynamic truncation (RFC 4226 §5.4).
    offset = digest[-1] & 0x0F
    value = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFFFFFF

    mod = 10**TOTP_DIGITS
    return str(value % mod).zfill(TOTP_DIGITS)


def verify_totp(secret: str, code: str, now: datetime) -> bool:
    """Report whether `code` is valid for `secret` around `now`, tolerating
    one window of clock skew either way. Comparison is constant-time to
    avoid leaking information through timing.
    """
    code = code.strip()
    if len(code) != TOTP_DIGITS:
        return False

    for w in range(-TOTP_SKEW_WINDOWS, TOTP_SKEW_WINDOWS + 1):
        try:
            want = totp_code(secret, now + w * TOTP_PERIOD)
        except ValueError:
            return False
        if hmac.compare_digest(want, code):
            return True
    return False


def totp_provisioning_uri(secret: str, account: str, issuer: str) -> str:
    """Build the otpauth:// URI that authenticator apps scan."""
    label = quote(f"{issuer}:{account}", safe="")
    q = {
        "secret": secret,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": str(TOTP_DIGITS),
        "period": str(int(TOTP_PERIOD.total_seconds())),
    }
    return f"otpauth://totp/{label}?{urlencode(q)}"


def new_recovery_codes(n: int) -> List[str]:
    """Return `n` single-use backup codes for account recovery."""
    codes = []
    for _ in range(n):
        buf = secrets.token_bytes(5)
        codes.append(_b32_encode(buf).lower())
    return codes