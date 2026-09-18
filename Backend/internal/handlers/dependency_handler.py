from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..auth.session import get_user_id
from ..store.taskdeps import DependencyCycleError, SelfDependencyError
from ..store.tasks import NotFoundError as TaskNotFoundError

if TYPE_CHECKING:
    from .handlers import Handlers


def _current_user_id() -> uuid.UUID:
    user_id, ok = get_user_id()
    if not ok or user_id is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "unauthenticated",
                "message": "missing authenticated user",
            },
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


async def list_task_dependencies(
    self: "Handlers",
    task_id: uuid.UUID,
):
    user_id = _current_user_id()
    task, _role = await self.require_task_access(task_id, user_id)

    try:
        return await self.store.tasks.list_dependencies(task.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc


async def add_task_dependency(
    self: "Handlers",
    task_id: uuid.UUID,
    request: Request,
):
    user_id = _current_user_id()
    task, role = await self.require_task_access(task_id, user_id)

    if role == "guest":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "guests cannot edit dependencies",
            },
        )

    data = await _read_json_object(request, {"dependsOnId"})
    raw = data.get("dependsOnId")

    if not isinstance(raw, str):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "dependsOnId is required",
            },
        )

    try:
        depends_on_id = uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "dependsOnId must be a UUID",
            },
        ) from exc

    if depends_on_id.int == 0:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "dependsOnId is required",
            },
        )

    try:
        dependency = await self.store.tasks.get_by_id(depends_on_id)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "dependency task not found",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "lookup_failed", "message": str(exc)},
        ) from exc

    if dependency.project_id != task.project_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation",
                "message": "dependency must be in the same project",
            },
        )

    try:
        await self.store.tasks.add_dependency(
            task.id,
            depends_on_id,
        )
    except SelfDependencyError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "self_dependency",
                "message": str(exc) or "task cannot depend on itself",
            },
        ) from exc
    except DependencyCycleError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "dependency_cycle",
                "message": "adding this dependency would create a cycle",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "add_failed", "message": str(exc)},
        ) from exc

    try:
        await self.store.tasks.record_activity(
            task.id,
            user_id,
            "dependency_added",
            {
                "dependsOnId": str(depends_on_id),
                "title": dependency.title,
            },
        )
    except Exception:
        pass

    try:
        dependencies = await self.store.tasks.list_dependencies(task.id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "list_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=201,
        content=jsonable_encoder(dependencies),
    )


async def remove_task_dependency(
    self: "Handlers",
    task_id: uuid.UUID,
    dep_id: uuid.UUID,
) -> dict:
    user_id = _current_user_id()
    task, role = await self.require_task_access(task_id, user_id)

    if role == "guest":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "forbidden",
                "message": "guests cannot edit dependencies",
            },
        )

    try:
        await self.store.tasks.remove_dependency(
            task.id,
            dep_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "remove_failed", "message": str(exc)},
        ) from exc

    try:
        await self.store.tasks.record_activity(
            task.id,
            user_id,
            "dependency_removed",
            {"dependsOnId": str(dep_id)},
        )
    except Exception:
        pass

    return {"ok": True}
