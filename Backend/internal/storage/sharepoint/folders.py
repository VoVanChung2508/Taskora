"""
Module sharepoint: client Microsoft Graph mỏng dùng để đồng bộ file dự án
của Flowie vào một thư viện tài liệu SharePoint đã cấu hình. Xác thực bằng
luồng OAuth2 client-credentials (application permissions) và có thể tự tạo
cấu trúc thư mục lồng nhau theo Workspace / Project / Task.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple
from urllib.parse import quote

import httpx

from . import config

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphError(Exception):
    """Lỗi khi gọi Microsoft Graph API."""


@dataclass
class DriveItemFolderFacet:
    child_count: int = 0


@dataclass
class DriveItem:
    """Biểu diễn tối giản của một driveItem trong Graph."""

    id: str = ""
    name: str = ""
    web_url: str = ""
    folder: Optional[DriveItemFolderFacet] = None
    size: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "DriveItem":
        folder_data = data.get("folder")
        folder = (
            DriveItemFolderFacet(child_count=folder_data.get("childCount", 0))
            if folder_data is not None
            else None
        )
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            web_url=data.get("webUrl", ""),
            folder=folder,
            size=data.get("size", 0),
        )


def _item_path(rel: str) -> str:
    """Escape một đường dẫn tương đối trong drive để dùng trong đoạn địa chỉ Graph."""
    rel = rel.strip("/")
    parts = rel.split("/")
    return "/".join(quote(p, safe="") for p in parts)


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

    # ---- Thư mục / file --------------------------------------------------

    async def _get_item(self, rel: str) -> Tuple[Optional[DriveItem], bool]:
        """Lấy driveItem theo đường dẫn tương đối trong drive, hoặc
        (None, False) nếu không tồn tại."""
        if rel == "":
            root = await self._do_json("GET", f"/drives/{self._drive_id}/root")
            return DriveItem.from_dict(root), True

        path = f"/drives/{self._drive_id}/root:/{_item_path(rel)}"
        try:
            item = await self._do_json("GET", path)
        except GraphError as exc:
            if "(404)" in str(exc):
                return None, False
            raise
        return DriveItem.from_dict(item), True

    async def _create_folder(self, parent_rel: str, child: str) -> DriveItem:
        """Tạo một thư mục con tên `child` bên dưới `parent_rel`
        (đường dẫn tương đối trong drive)."""
        payload = {
            "name": child,
            "folder": {},
            # idempotent-ish; giữ nguyên nếu đã tồn tại
            "@microsoft.graph.conflictBehavior": "replace",
        }
        body = json.dumps(payload).encode()

        if parent_rel == "":
            children_path = f"/drives/{self._drive_id}/root/children"
        else:
            children_path = (
                f"/drives/{self._drive_id}/root:/{_item_path(parent_rel)}:/children"
            )

        item = await self._do_json(
            "POST", children_path, content=body, content_type="application/json"
        )
        return DriveItem.from_dict(item)

    async def ensure_folder(self, rel: str) -> Optional[DriveItem]:
        """Đảm bảo mọi phân đoạn của đường dẫn tương đối cho trước đều tồn
        tại, tạo các thư mục còn thiếu. Trả về driveItem của thư mục cuối
        cùng. Đây là phần cốt lõi của hành vi "tự tạo cấu trúc thư mục con"
        của Flowie."""
        await self._resolve()

        rel = rel.strip("/")
        if rel == "":
            item, _ = await self._get_item("")
            return item

        segments = [s for s in rel.split("/")]
        built = ""
        last: Optional[DriveItem] = None
        for seg in segments:
            seg = seg.strip()
            if seg == "":
                continue
            next_rel = seg if built == "" else f"{built}/{seg}"

            item, exists = await self._get_item(next_rel)
            if not exists:
                try:
                    item = await self._create_folder(built, seg)
                except GraphError as exc:
                    raise GraphError(f'create folder "{next_rel}": {exc}') from exc

            last = item
            built = next_rel

        return last

    def root_folder_path(self) -> str:
        """Trả về thư mục gốc đã cấu hình (tương đối trong drive, không có
        dấu / ở đầu)."""
        return self.root_folder

    async def upload_file(self, rel: str, content: bytes) -> DriveItem:
        """Tải lên (hoặc thay thế) một file nhỏ tại đường dẫn tương đối cho
        trước. Với file dưới khoảng 4MB, PUT đơn giản này là đủ; file lớn
        cần dùng upload session (sẽ bổ sung sau)."""
        await self._resolve()
        path = f"/drives/{self._drive_id}/root:/{_item_path(rel)}:/content"
        item = await self._do_json(
            "PUT", path, content=content, content_type="application/octet-stream"
        )
        return DriveItem.from_dict(item)

    async def list_folder(self, rel: str) -> List[DriveItem]:
        """Trả về danh sách các item con của một thư mục theo đường dẫn
        tương đối trong drive."""
        await self._resolve()
        if rel.strip("/") == "":
            list_path = f"/drives/{self._drive_id}/root/children"
        else:
            list_path = (
                f"/drives/{self._drive_id}/root:/{_item_path(rel)}:/children"
            )

        out = await self._do_json("GET", list_path)
        return [DriveItem.from_dict(v) for v in out.get("value", [])]