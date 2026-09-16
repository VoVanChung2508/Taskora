"""
Module sharepoint: client Microsoft Graph mỏng dùng để đồng bộ file dự án
của Flowie vào một thư viện tài liệu SharePoint đã cấu hình. Xác thực bằng
luồng OAuth2 client-credentials (application permissions) và có thể tự tạo
cấu trúc thư mục lồng nhau theo Workspace / Project / Task.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from . import config

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphError(Exception):
    """Lỗi khi gọi Microsoft Graph API."""


@dataclass
class Client:
    """Giao tiếp với Microsoft Graph cho một site + drive SharePoint cụ thể."""

    cfg: "config.SharePointConfig"
    http: httpx.AsyncClient = field(init=False)
    token_url: str = field(init=False)
    root_folder: str = field(init=False)

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _token: str = field(default="", init=False)
    _token_exp: float = field(default=0.0, init=False)
    _site_id: str = field(default="", init=False)
    _drive_id: str = field(default="", init=False)
    _resolved: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.http = httpx.AsyncClient(timeout=30.0)
        self.token_url = (
            f"https://login.microsoftonline.com/{self.cfg.tenant_id}"
            "/oauth2/v2.0/token"
        )
        self.root_folder = self.cfg.root_folder.strip("/")

    @classmethod
    def new(cls, cfg: "config.SharePointConfig") -> Optional["Client"]:
        """Xây dựng client SharePoint. Trả về None (không lỗi) khi chưa được
        cấu hình, để ứng dụng vẫn chạy được mà không cần lưu trữ file trong
        giai đoạn phát triển sớm."""
        if not cfg.configured():
            return None
        return cls(cfg=cfg)

    async def aclose(self) -> None:
        await self.http.aclose()

    # ---- Xác thực (client credentials) -------------------------------

    async def _access_token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._token_exp - 60:
                return self._token

            form = {
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            }
            try:
                resp = await self.http.post(
                    self.token_url,
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                raise GraphError(f"token request: {exc}") from exc

            if resp.status_code != 200:
                raise GraphError(
                    f"token request failed ({resp.status_code}): {resp.text}"
                )

            data = resp.json()
            self._token = data["access_token"]
            self._token_exp = time.monotonic() + data.get("expires_in", 0)
            return self._token

    # doJSON: thực hiện một request Graph đã xác thực và giải mã JSON trả về.
    # body có thể là None; nếu out_model là None thì bỏ qua việc parse.
    async def _do_json(
        self,
        method: str,
        path: str,
        *,
        content: Any = None,
        content_type: str = "",
    ) -> Optional[dict]:
        token = await self._access_token()

        headers = {"Authorization": f"Bearer {token}"}
        if content_type:
            headers["Content-Type"] = content_type

        try:
            resp = await self.http.request(
                method, GRAPH_BASE + path, content=content, headers=headers
            )
        except httpx.HTTPError as exc:
            raise GraphError(f"graph {method} {path} failed: {exc}") from exc

        if not (200 <= resp.status_code < 300):
            raise GraphError(
                f"graph {method} {path} failed ({resp.status_code}): {resp.text}"
            )

        if resp.content:
            return resp.json()
        return None

    # ---- Phân giải site / drive ---------------------------------------

    async def _resolve(self) -> None:
        """Tra cứu site id và drive id mặc định cho site đã cấu hình."""
        async with self._lock:
            if self._resolved:
                return

        try:
            site = await self._do_json("GET", "/sites/" + self.cfg.site_url)
        except GraphError as exc:
            raise GraphError(f"resolve site: {exc}") from exc

        try:
            drive = await self._do_json("GET", f"/sites/{site['id']}/drive")
        except GraphError as exc:
            raise GraphError(f"resolve drive: {exc}") from exc

        async with self._lock:
            self._site_id = site["id"]
            self._drive_id = drive["id"]
            self._resolved = True