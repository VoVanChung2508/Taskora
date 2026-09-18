"""
Canonical SharePoint / Microsoft Graph client used by Taskora.

This file replaces the migration's duplicated Client implementations.  Other
sharepoint modules can re-export this one instead of maintaining separate copies.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ...config import config


GRAPH_BASE = "https://graph.microsoft.com/v1.0"

DEFAULT_PROJECT_SUBFOLDERS: list[str] = [
    "01_Documents",
    "02_Designs",
    "03_Deliverables",
    "04_Tasks",
    "05_Attachments",
]

SIMPLE_UPLOAD_LIMIT = 4 << 20

# Microsoft Graph requires each non-final upload-session fragment to use a
# size divisible by 320 KiB.
_CHUNK_SIZE = 5 * 320 * 1024  # 1.6 MiB


@dataclass(frozen=True)
class ChunkRange:
    start: int
    end: int


def _plan_chunks(
    size: int,
    chunk: int = 0,
) -> list[ChunkRange]:
    if size <= 0:
        return []

    if chunk <= 0:
        chunk = _CHUNK_SIZE

    result: list[ChunkRange] = []
    start = 0

    while start < size:
        end = min(start + chunk - 1, size - 1)
        result.append(
            ChunkRange(start=start, end=end)
        )
        start = end + 1

    return result


class GraphError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class DriveItemFolderFacet:
    child_count: int = 0


@dataclass
class DriveItem:
    id: str = ""
    name: str = ""
    web_url: str = ""
    folder: Optional[DriveItemFolderFacet] = None
    size: int = 0

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any] | None,
    ) -> "DriveItem":
        data = data or {}
        folder_data = data.get("folder")
        folder = (
            DriveItemFolderFacet(
                child_count=folder_data.get(
                    "childCount",
                    0,
                )
            )
            if isinstance(folder_data, dict)
            else None
        )

        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            web_url=str(data.get("webUrl") or ""),
            folder=folder,
            size=int(data.get("size") or 0),
        )


def _item_path(rel: str) -> str:
    rel = rel.strip("/")
    if not rel:
        return ""
    return "/".join(
        quote(part, safe="")
        for part in rel.split("/")
    )


@dataclass
class Client:
    cfg: "config.SharePointConfig"
    http: httpx.AsyncClient = field(init=False)
    token_url: str = field(init=False)
    root_folder: str = field(init=False)

    _lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
    )
    _resolve_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
    )
    _token: str = field(default="", init=False)
    _token_exp: float = field(default=0.0, init=False)
    _site_id: str = field(default="", init=False)
    _drive_id: str = field(default="", init=False)
    _resolved: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.http = httpx.AsyncClient(timeout=60.0)
        self.token_url = (
            "https://login.microsoftonline.com/"
            f"{self.cfg.tenant_id}"
            "/oauth2/v2.0/token"
        )
        self.root_folder = self.cfg.root_folder.strip("/")

    @classmethod
    def new(
        cls,
        cfg: "config.SharePointConfig",
    ) -> Optional["Client"]:
        if not cfg.configured():
            return None
        return cls(cfg=cfg)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _access_token(self) -> str:
        async with self._lock:
            if (
                self._token
                and time.monotonic()
                < self._token_exp - 60
            ):
                return self._token

            try:
                response = await self.http.post(
                    self.token_url,
                    data={
                        "client_id": self.cfg.client_id,
                        "client_secret": self.cfg.client_secret,
                        "scope": (
                            "https://graph.microsoft.com/.default"
                        ),
                        "grant_type": "client_credentials",
                    },
                    headers={
                        "Content-Type": (
                            "application/x-www-form-urlencoded"
                        ),
                    },
                )
            except httpx.HTTPError as exc:
                raise GraphError(
                    f"token request: {exc}"
                ) from exc

            if response.status_code != 200:
                raise GraphError(
                    "token request failed "
                    f"({response.status_code}): "
                    f"{response.text}",
                    status_code=response.status_code,
                )

            try:
                data = response.json()
                token = str(data["access_token"])
            except Exception as exc:
                raise GraphError(
                    "token response missing access_token"
                ) from exc

            self._token = token
            self._token_exp = (
                time.monotonic()
                + int(data.get("expires_in") or 0)
            )
            return self._token

    async def _do_json(
        self,
        method: str,
        path: str,
        *,
        content: Any = None,
        content_type: str = "",
    ) -> Optional[dict[str, Any]]:
        token = await self._access_token()

        headers = {
            "Authorization": f"Bearer {token}",
        }
        if content_type:
            headers["Content-Type"] = content_type

        try:
            response = await self.http.request(
                method,
                GRAPH_BASE + path,
                content=content,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise GraphError(
                f"graph {method} {path} failed: {exc}"
            ) from exc

        if not 200 <= response.status_code < 300:
            raise GraphError(
                f"graph {method} {path} failed "
                f"({response.status_code}): "
                f"{response.text}",
                status_code=response.status_code,
            )

        if not response.content:
            return None

        try:
            return response.json()
        except ValueError as exc:
            raise GraphError(
                f"graph {method} {path} returned invalid JSON"
            ) from exc

    async def _resolve(self) -> None:
        if self._resolved:
            return

        async with self._resolve_lock:
            if self._resolved:
                return

            site = await self._do_json(
                "GET",
                "/sites/" + self.cfg.site_url,
            )
            if not site or not site.get("id"):
                raise GraphError(
                    "resolve site: response missing site id"
                )

            drive = await self._do_json(
                "GET",
                f"/sites/{site['id']}/drive",
            )
            if not drive or not drive.get("id"):
                raise GraphError(
                    "resolve drive: response missing drive id"
                )

            self._site_id = str(site["id"])
            self._drive_id = str(drive["id"])
            self._resolved = True

    async def _get_item(
        self,
        rel: str,
    ) -> tuple[Optional[DriveItem], bool]:
        await self._resolve()

        rel = rel.strip("/")
        if not rel:
            root = await self._do_json(
                "GET",
                f"/drives/{self._drive_id}/root",
            )
            return DriveItem.from_dict(root), True

        graph_path = (
            f"/drives/{self._drive_id}"
            f"/root:/{_item_path(rel)}"
        )

        try:
            item = await self._do_json(
                "GET",
                graph_path,
            )
        except GraphError as exc:
            if exc.status_code == 404:
                return None, False
            raise

        return DriveItem.from_dict(item), True

    async def _create_folder(
        self,
        parent_rel: str,
        child: str,
    ) -> DriveItem:
        await self._resolve()

        payload = json.dumps(
            {
                "name": child,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "replace",
            }
        ).encode("utf-8")

        if not parent_rel:
            endpoint = (
                f"/drives/{self._drive_id}"
                "/root/children"
            )
        else:
            endpoint = (
                f"/drives/{self._drive_id}"
                f"/root:/{_item_path(parent_rel)}"
                ":/children"
            )

        item = await self._do_json(
            "POST",
            endpoint,
            content=payload,
            content_type="application/json",
        )
        return DriveItem.from_dict(item)

    async def ensure_folder(
        self,
        rel: str,
    ) -> Optional[DriveItem]:
        await self._resolve()

        rel = rel.strip("/")
        if not rel:
            item, _exists = await self._get_item("")
            return item

        built = ""
        last: Optional[DriveItem] = None

        for raw_segment in rel.split("/"):
            segment = raw_segment.strip()
            if not segment:
                continue

            next_rel = (
                segment
                if not built
                else f"{built}/{segment}"
            )

            item, exists = await self._get_item(
                next_rel
            )
            if not exists:
                try:
                    item = await self._create_folder(
                        built,
                        segment,
                    )
                except GraphError as exc:
                    raise GraphError(
                        f'create folder "{next_rel}": {exc}',
                        status_code=exc.status_code,
                    ) from exc

            last = item
            built = next_rel

        return last

    def root_folder_path(self) -> str:
        return self.root_folder

    async def upload_file(
        self,
        rel: str,
        content: bytes,
    ) -> DriveItem:
        await self._resolve()

        endpoint = (
            f"/drives/{self._drive_id}"
            f"/root:/{_item_path(rel)}:/content"
        )
        item = await self._do_json(
            "PUT",
            endpoint,
            content=content,
            content_type="application/octet-stream",
        )
        return DriveItem.from_dict(item)

    async def upload_large_file(
        self,
        rel: str,
        content: bytes,
    ) -> DriveItem:
        """
        Upload a file, automatically switching to a Graph upload session above
        the simple-upload threshold.
        """
        if len(content) <= SIMPLE_UPLOAD_LIMIT:
            return await self.upload_file(
                rel,
                content,
            )

        await self._resolve()

        session_endpoint = (
            f"/drives/{self._drive_id}"
            f"/root:/{_item_path(rel)}"
            ":/createUploadSession"
        )

        session = await self._do_json(
            "POST",
            session_endpoint,
            content=json.dumps(
                {
                    "item": {
                        "@microsoft.graph.conflictBehavior": (
                            "replace"
                        ),
                    }
                }
            ).encode("utf-8"),
            content_type="application/json",
        )

        upload_url = (
            str(session.get("uploadUrl") or "")
            if session
            else ""
        )
        if not upload_url:
            raise GraphError(
                "create upload session: response missing uploadUrl"
            )

        final_item: Optional[DriveItem] = None
        total = len(content)

        for chunk in _plan_chunks(total):
            body = content[
                chunk.start : chunk.end + 1
            ]
            headers = {
                "Content-Length": str(len(body)),
                "Content-Range": (
                    f"bytes {chunk.start}-{chunk.end}/{total}"
                ),
            }

            try:
                response = await self.http.put(
                    upload_url,
                    content=body,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise GraphError(
                    "upload session chunk failed: "
                    f"{exc}"
                ) from exc

            if response.status_code in {200, 201}:
                try:
                    final_item = DriveItem.from_dict(
                        response.json()
                    )
                except ValueError as exc:
                    raise GraphError(
                        "upload session completed with invalid JSON"
                    ) from exc
                continue

            if response.status_code == 202:
                continue

            raise GraphError(
                "upload session chunk failed "
                f"({response.status_code}): "
                f"{response.text}",
                status_code=response.status_code,
            )

        if final_item is not None and final_item.id:
            return final_item

        # Defensive fallback: Graph normally returns the driveItem with the
        # final chunk; fetch it explicitly if an intermediary/proxy stripped it.
        item, exists = await self._get_item(rel)
        if not exists or item is None:
            raise GraphError(
                "upload completed but drive item could not be resolved"
            )
        return item

    async def list_folder(
        self,
        rel: str,
    ) -> list[DriveItem]:
        await self._resolve()

        if not rel.strip("/"):
            endpoint = (
                f"/drives/{self._drive_id}"
                "/root/children"
            )
        else:
            endpoint = (
                f"/drives/{self._drive_id}"
                f"/root:/{_item_path(rel)}"
                ":/children"
            )

        data = await self._do_json(
            "GET",
            endpoint,
        )
        values = data.get("value", []) if data else []
        return [
            DriveItem.from_dict(value)
            for value in values
        ]

    def workspace_folder(
        self,
        workspace_slug: str,
    ) -> str:
        return posixpath.normpath(
            posixpath.join(
                self.root_folder,
                workspace_slug,
            )
        )

    def project_folder(
        self,
        workspace_slug: str,
        project_slug: str,
    ) -> str:
        return posixpath.normpath(
            posixpath.join(
                self.root_folder,
                workspace_slug,
                project_slug,
            )
        )

    def task_folder(
        self,
        workspace_slug: str,
        project_slug: str,
        task_ref: str,
    ) -> str:
        return posixpath.normpath(
            posixpath.join(
                self.root_folder,
                workspace_slug,
                project_slug,
                "04_Tasks",
                task_ref,
            )
        )
