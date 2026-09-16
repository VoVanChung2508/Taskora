"""Small HTTP helpers shared by handlers.

Python port of the Go `httpx` package. Framework-agnostic: instead of
writing directly to an `http.ResponseWriter` (there is no single Python
equivalent across frameworks), `json_response`/`error` return a small
`JSONResponse` value your handler can return directly. Most frameworks
accept `(body, status, headers)` — see `JSONResponse.as_tuple()` — or can
be adapted with one line (e.g. `Response(r.body, status=r.status,
headers=r.headers)` in Flask/Starlette).

This matches the `httpx.error(status, code, message)` calls already used
in `require_auth.py`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Optional, Type, TypeVar, Union

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class JSONResponse:
    """A JSON response ready to hand back to your web framework."""

    status: int
    body: bytes
    headers: dict

    def as_tuple(self):
        """Flask-style `(body, status, headers)` tuple."""
        return self.body, self.status, self.headers


def json_response(status: int, value: Optional[Any] = None) -> JSONResponse:
    """Build a JSON response with the given status code.

    Equivalent of Go's `JSON(w, status, v)`.
    """
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if value is None:
        return JSONResponse(status=status, body=b"", headers=headers)

    try:
        payload = json.dumps(_to_jsonable(value)).encode("utf-8")
    except (TypeError, ValueError) as exc:
        logger.error("encode json response: %s", exc)
        return JSONResponse(status=status, body=b"", headers=headers)

    return JSONResponse(status=status, body=payload, headers=headers)


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    return value


@dataclass
class ErrorBody:
    """The standard error envelope."""

    error: str
    message: str = ""


def error(status: int, code: str, message: str) -> JSONResponse:
    """Build a JSON error response.

    Equivalent of Go's `Error(w, status, code, message)`.
    """
    body: dict = {"error": code}
    if message:
        body["message"] = message
    return json_response(status, body)


def decode(raw_body: Union[bytes, str], target_cls: Type[T]) -> T:
    """Read and validate a JSON request body into an instance of
    `target_cls`, rejecting unknown fields.

    Equivalent of Go's `Decode(r, dst)` with `DisallowUnknownFields`.
    `target_cls` must be a dataclass. Pass the raw request body — e.g.
    Flask's `request.get_data()`, FastAPI's `await request.body()`, etc.
    """
    if not is_dataclass(target_cls):
        raise TypeError("decode: target_cls must be a dataclass")

    data = json.loads(raw_body)
    if not isinstance(data, dict):
        raise ValueError("decode: expected a JSON object")

    allowed = {f.name for f in fields(target_cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"json: unknown field(s) {sorted(unknown)}")

    return target_cls(**data)