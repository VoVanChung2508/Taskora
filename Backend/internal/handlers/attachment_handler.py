from __future__ import annotations

import posixpath
import re
import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from ..auth.session import get_user_id
from ..store.attachments import (
    Attachment,
    NotFoundError as AttachmentNotFoundError,
)

if TYPE_CHECKING:
    from .handlers import Handlers


MAX_ATTACHMENT_BYTES = 100 << 20
_UNSAFE_FILE_CHARS = re.compile(r'[\\/:*?"<>|#%]')


def _current_user_id() -> uuid.UUID:
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": ""},
        )
    return user_id


def safe_file_name(name: str) -> str:
    """
    Make a browser-provided filename safe for SharePoint.

    Windows browsers may submit a full C:\\... path, so backslashes are
    normalised before taking the basename.
    """
    name = (name or "").strip().replace("\\", "/")
    name = posixpath.basename(name)
    name = _UNSAFE_FILE_CHARS.sub("_", name)
    name = name.lstrip(".")

    if not name:
        name = "file"

    if len(name) > 180:
        ext = posixpath.splitext(name)[1]
        if ext and len(ext) < 180:
            name = name[: 180 - len(ext)] + ext
        else:
            name = name[:180]

    return name


def attachment_folder(
    project_folder: str | None,
    task_ref: str,
) -> str:
    base = (project_folder or "").strip("/")
    if not base:
        base = "Projects"
    return f"{base}/04_Tasks/{task_ref}"


def _confined_subpath(raw: str) -> str:
    """
    Resolve '.', '..' and repeated slashes while keeping the result relative.

    Prefixing '/' before normpath means '../../x' becomes '/x', after which the
    leading slash is removed and the result is joined below the project root.
    """
    cleaned = posixpath.normpath(
        "/" + (raw or "").strip("/")
    )
    if cleaned in {"/", "."}:
        return ""
    return cleaned.lstrip("/")


async def browse_project_files(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
) -> dict:
    user_id = _current_user_id()
    project, _role = await self.require_project_access(
        project_id,
        user_id,
    )

    if self.sharepoint is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "storage_unavailable",
                "message": "SharePoint chưa được cấu hình",
            },
        )

    root = (project.sharepoint_folder_path or "").strip("/")
    if not root:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_folder",
                "message": "dự án chưa có thư mục SharePoint",
            },
        )

    sub = _confined_subpath(
        request.query_params.get("path", "")
    )
    target = root if not sub else f"{root}/{sub}"

    try:
        items = await self.sharepoint.list_folder(target)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "browse_failed",
                "message": str(exc),
            },
        ) from exc

    return {
        "root": root,
        "path": sub,
        "items": items,
    }


async def list_attachments(
    self: "Handlers",
    task_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    task, _role = await self.require_task_access(
        task_id,
        user_id,
    )

    try:
        attachments = await self.store.attachments.list_by_task(
            task.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "list_failed",
                "message": str(exc),
            },
        ) from exc

    return {"attachments": attachments or []}


async def upload_attachment(
    self: "Handlers",
    task_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    task, role = await self.require_task_access(
        task_id,
        user_id,
    )

    if role == "guest":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "guests cannot upload files",
            },
        )

    if self.sharepoint is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "storage_unavailable",
                "message": "SharePoint chưa được cấu hình",
            },
        )

    try:
        form = await request.form()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_form",
                "message": str(exc),
            },
        ) from exc

    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_file",
                "message": "thiếu trường 'file'",
            },
        )

    # Read one byte beyond the application limit so the limit is enforced even
    # when Content-Length/header metadata is absent or incorrect.
    try:
        content = await upload.read(MAX_ATTACHMENT_BYTES + 1)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "read_failed",
                "message": str(exc),
            },
        ) from exc
    finally:
        await upload.close()

    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "file_too_large",
                "message": f"tối đa {MAX_ATTACHMENT_BYTES >> 20} MB",
            },
        )

    try:
        project = await self.store.projects.get_by_id(
            task.project_id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "project not found",
            },
        ) from exc

    task_ref = project.key
    if task.number is not None:
        task_ref = f"{project.key}-{task.number}"

    folder = attachment_folder(
        project.sharepoint_folder_path,
        task_ref,
    )
    name = safe_file_name(upload.filename or "")

    try:
        await self.sharepoint.ensure_folder(folder)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "folder_failed",
                "message": str(exc),
            },
        ) from exc

    try:
        item = await self.sharepoint.upload_large_file(
            f"{folder}/{name}",
            content,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "upload_failed",
                "message": str(exc),
            },
        ) from exc

    record = Attachment(
        id=None,
        task_id=task.id,
        uploaded_by=user_id,
        name=name,
        size_bytes=len(content),
        content_type=upload.content_type or "",
        drive_item_id=item.id,
        web_url=item.web_url,
        folder_path=folder,
    )

    try:
        saved = await self.store.attachments.create(record)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "save_failed",
                "message": str(exc),
            },
        ) from exc

    try:
        await self.store.tasks.record_activity(
            task.id,
            user_id,
            "attachment_added",
            {"name": name},
        )
    except Exception:
        pass

    # Keep the same fan-out path as the other task-mutating handlers:
    # realtime + integrations + outgoing webhooks.
    try:
        await self.emit(
            task.project_id,
            user_id,
            "task.updated",
            {"taskId": task.id},
        )
    except Exception:
        pass

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(saved),
    )


async def delete_attachment(
    self: "Handlers",
    task_id: uuid.UUID,
    attachment_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    task, role = await self.require_task_access(
        task_id,
        user_id,
    )

    if role == "guest":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "guests cannot remove files",
            },
        )

    try:
        await self.store.attachments.delete(
            task.id,
            attachment_id,
        )
    except AttachmentNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "attachment not found",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "delete_failed",
                "message": str(exc),
            },
        ) from exc

    # Intentionally do NOT delete the SharePoint copy. The source Go handler
    # treats SharePoint as the team's document store and only unlinks metadata.
    return {"ok": True}
