"""Security response headers middleware.

Python port of the Go `server.secureHeaders` middleware. Framework-agnostic:
wraps any handler with the signature `handler(request, *args, **kwargs) ->
response`, then adds the security headers to whatever response comes back.
Works with anything exposing a dict-like `.headers` (Flask, Starlette,
FastAPI responses, or our own `httpx.JSONResponse`) or a Flask-style
`(body, status, headers)` tuple.

HSTS is only sent in production (behind HTTPS).
"""

from __future__ import annotations

from typing import Callable

_SECURE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-XSS-Protection": "0",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

_HSTS_HEADER = "Strict-Transport-Security"
_HSTS_VALUE = "max-age=31536000; includeSubDomains"


def secure_headers(prod: bool) -> Callable[[Callable], Callable]:
    """Return middleware that sets conservative security response headers.

    Usage:
        app_handler = secure_headers(prod=True)(my_handler)
    """
    headers = dict(_SECURE_HEADERS)
    if prod:
        headers[_HSTS_HEADER] = _HSTS_VALUE

    def middleware(next_handler: Callable) -> Callable:
        def wrapped(request, *args, **kwargs):
            response = next_handler(request, *args, **kwargs)
            _apply_headers(response, headers)
            return response

        return wrapped

    return middleware


def _apply_headers(response, headers: dict) -> None:
    """Merge `headers` into `response` in place.

    Handles the common response shapes: an object with a dict-like
    `.headers` attribute, or a Flask-style `(body, status, headers)` tuple.
    Adapt this for your framework's response type if neither shape fits.
    """
    if hasattr(response, "headers"):
        for key, value in headers.items():
            response.headers[key] = value
        return

    if isinstance(response, tuple) and len(response) == 3 and isinstance(response[2], dict):
        response[2].update(headers)
        return