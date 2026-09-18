from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.overview import TrendRange
from ..store.reports import NotFoundError as ReportNotFoundError

if TYPE_CHECKING:
    from .handlers import Handlers


logger = logging.getLogger(__name__)
VALID_PROVIDERS = {"slack", "teams"}


def _current_user_id() -> uuid.UUID:
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": ""},
        )
    return user_id


async def _read_json_object(
    request: Request,
    allowed_fields: set[str],
) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": str(exc)},
        ) from exc

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "expected JSON object"},
        )

    unknown = set(data) - allowed_fields
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": f"unknown field(s): {sorted(unknown)}",
            },
        )

    return data


async def build_report_text(
    self: "Handlers",
    report,
) -> str:
    overview = await self.store.tasks.workspace_overview(
        report.workspace_id,
        TrendRange(unit="month", count=1),
    )

    period = "Tuần này" if report.frequency == "weekly" else "Hôm nay"

    lines = [
        f"*{report.name} · {period}*",
        f"• Tổng công việc: {overview.total_tasks} (hoàn thành {overview.done_tasks})",
        f"• Đang làm: {overview.in_progress_task} · Chưa bắt đầu: {overview.backlog_tasks}",
    ]

    if overview.overdue_tasks > 0:
        lines.append(f"• ⚠️ Quá hạn: {overview.overdue_tasks}")

    lines.append(f"• Giờ đã log: {overview.hours_logged:.1f}h")

    if overview.projects:
        project_lines: list[str] = []
        for project in overview.projects:
            if (
                report.project_id is not None
                and project.project_id != report.project_id
            ):
                continue

            percent = 0
            if project.total > 0:
                percent = project.done * 100 // project.total

            line = (
                f"• {project.key}: "
                f"{project.done}/{project.total} ({percent}%)"
            )
            if project.overdue > 0:
                line += f" · quá hạn {project.overdue}"
            project_lines.append(line)

        if project_lines:
            lines.append("")
            lines.append("*Theo dự án*")
            lines.extend(project_lines)

    return "\n".join(lines) + "\n"


async def send_report(
    self: "Handlers",
    report,
) -> None:
    try:
        text = await self.build_report_text(report)
    except Exception as exc:
        await self.store.reports.mark_run(
            report.id,
            0,
            str(exc),
        )
        return

    try:
        payload = self.chat_payload(report.provider, text)
    except Exception as exc:
        await self.store.reports.mark_run(
            report.id,
            0,
            str(exc),
        )
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                report.channel_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except Exception as exc:
        await self.store.reports.mark_run(
            report.id,
            0,
            str(exc),
        )
        return

    message = (
        "non-2xx response"
        if response.status_code >= 300
        else ""
    )
    await self.store.reports.mark_run(
        report.id,
        response.status_code,
        message,
    )


async def _report_scheduler_loop(
    self: "Handlers",
) -> None:
    while True:
        await asyncio.sleep(600)
        try:
            hour = datetime.now(timezone.utc).hour
            due = await self.store.reports.due_now(hour)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("report scheduler query failed")
            continue

        for report in due:
            try:
                await self.send_report(report)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "report scheduler delivery failed id=%s",
                    getattr(report, "id", None),
                )


def start_report_scheduler(
    self: "Handlers",
) -> asyncio.Task:
    return asyncio.create_task(
        _report_scheduler_loop(self),
        name="taskora-report-scheduler",
    )


async def list_reports(
    self: "Handlers",
    workspace_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    try:
        reports = await self.store.reports.list_by_workspace(
            workspace.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return {"reports": reports or []}


async def create_report(
    self: "Handlers",
    workspace_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    data = await _read_json_object(
        request,
        {
            "name",
            "projectId",
            "frequency",
            "channelUrl",
            "provider",
            "hourUtc",
        },
    )

    name = data.get("name", "")
    raw_project_id = data.get("projectId")
    frequency = data.get("frequency", "")
    channel_url = data.get("channelUrl", "")
    provider = data.get("provider", "")
    hour_utc = data.get("hourUtc", 0)

    if not isinstance(name, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "name must be a string"},
        )
    if not isinstance(frequency, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "frequency must be a string"},
        )
    if not isinstance(channel_url, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "channelUrl must be a string"},
        )
    if not isinstance(provider, str):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "provider must be a string"},
        )
    if isinstance(hour_utc, bool) or not isinstance(hour_utc, int):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_body", "message": "hourUtc must be an integer"},
        )

    project_id: uuid.UUID | None = None
    if raw_project_id not in (None, ""):
        if not isinstance(raw_project_id, str):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": "projectId must be a UUID"},
            )
        try:
            project_id = uuid.UUID(raw_project_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_body", "message": "projectId must be a UUID"},
            ) from exc

    name = name.strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail={"error": "validation", "message": "name is required"},
        )

    if frequency not in {"daily", "weekly"}:
        frequency = "weekly"

    if not provider:
        provider = "slack"

    if provider not in VALID_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "provider must be slack or teams",
            },
        )

    channel_url = channel_url.strip()
    parsed = urlparse(channel_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "channelUrl phải là URL https hợp lệ",
            },
        )

    if hour_utc < 0 or hour_utc > 23:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "hourUtc phải trong khoảng 0–23",
            },
        )

    try:
        report = await self.store.reports.create(
            workspace.id,
            project_id,
            name,
            frequency,
            channel_url,
            provider,
            hour_utc,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "create_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(report),
    )


async def delete_report(
    self: "Handlers",
    workspace_id: uuid.UUID,
    report_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    try:
        await self.store.reports.delete(
            workspace.id,
            report_id,
        )
    except ReportNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "report not found"},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "delete_failed", "message": str(exc)},
        ) from exc

    return {"ok": True}


async def run_report_now(
    self: "Handlers",
    workspace_id: uuid.UUID,
    report_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    workspace = await self.require_workspace_manager(
        workspace_id,
        user_id,
    )

    try:
        reports = await self.store.reports.list_by_workspace(
            workspace.id
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "lookup_failed", "message": str(exc)},
        ) from exc

    for report in reports:
        if report.id == report_id:
            await self.send_report(report)
            return {"sent": True}

    raise HTTPException(
        status_code=404,
        detail={"error": "not_found", "message": "report not found"},
    )
