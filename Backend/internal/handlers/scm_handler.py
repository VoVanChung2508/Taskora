from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from .handlers import Handlers


TASK_REF_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")


@dataclass
class SCMNote:
    task_number: str
    text: str


def verify_scm_signature(
    secret: str,
    body: bytes,
    header: str,
) -> bool:
    if not secret:
        return True

    digest = hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    expected = "sha256=" + digest
    return hmac.compare_digest(expected, header)


def refs_for(
    text: str,
    project_key: str,
) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    for match in TASK_REF_RE.finditer(text or ""):
        key = match.group(1)
        number = match.group(2)

        if key.lower() != project_key.lower():
            continue
        if number in seen:
            continue

        seen.add(number)
        out.append(number)

    return out


def first_line(text: str) -> str:
    return (text or "").split("\n", 1)[0]


def scm_notes(
    payload: dict[str, Any],
    project_key: str,
) -> list[SCMNote]:
    out: list[SCMNote] = []

    commits = payload.get("commits") or []
    if isinstance(commits, list):
        for commit in commits:
            if not isinstance(commit, dict):
                continue

            message = str(commit.get("message") or "")
            commit_id = str(commit.get("id") or "")
            short = commit_id[:7]
            url = str(commit.get("url") or "")

            author = commit.get("author") or {}
            author_name = (
                str(author.get("name") or "")
                if isinstance(author, dict)
                else ""
            )

            for ref in refs_for(message, project_key):
                out.append(
                    SCMNote(
                        task_number=ref,
                        text=(
                            f"🔗 Commit `{short}` bởi {author_name}: "
                            f"{first_line(message)}\n{url}"
                        ),
                    )
                )

    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        action = str(payload.get("action") or "")
        if bool(pull_request.get("merged")):
            action = "merged"

        title = str(pull_request.get("title") or "")
        number = pull_request.get("number", 0)
        url = str(pull_request.get("html_url") or "")

        for ref in refs_for(title, project_key):
            out.append(
                SCMNote(
                    task_number=ref,
                    text=(
                        f"🔗 Pull request #{number} {action}: "
                        f"{title}\n{url}"
                    ),
                )
            )

    object_attributes = payload.get("object_attributes")
    if (
        payload.get("object_kind") == "merge_request"
        and isinstance(object_attributes, dict)
    ):
        title = str(object_attributes.get("title") or "")
        iid = object_attributes.get("iid", 0)
        state = str(object_attributes.get("state") or "")
        url = str(object_attributes.get("url") or "")

        for ref in refs_for(title, project_key):
            out.append(
                SCMNote(
                    task_number=ref,
                    text=(
                        f"🔗 Merge request !{iid} {state}: "
                        f"{title}\n{url}"
                    ),
                )
            )

    return out


async def link_scm_note(
    self: "Handlers",
    project_id: uuid.UUID,
    note: SCMNote,
) -> bool:
    try:
        task = await self.store.tasks.by_project_number(
            project_id,
            note.task_number,
        )
    except Exception:
        return False

    try:
        await self.store.tasks.add_system_comment(
            task.id,
            note.text,
        )
    except Exception:
        return False

    try:
        await self.store.tasks.record_activity(
            task.id,
            uuid.UUID(int=0),
            "scm_linked",
            {"note": first_line(note.text)},
        )
    except Exception:
        pass

    await self.emit(
        project_id,
        uuid.UUID(int=0),
        "task.commented",
        {
            "taskId": task.id,
            "via": "scm",
        },
    )
    return True


async def scm_webhook(
    self: "Handlers",
    project_id: uuid.UUID,
    request: Request,
) -> dict:
    try:
        project = await self.store.projects.get_by_id(project_id)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": "project not found",
            },
        ) from exc

    try:
        body = await request.body()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "read_failed",
                "message": str(exc),
            },
        ) from exc

    # Match Go io.LimitReader(..., 1<<20): at most the first 1 MiB is parsed.
    body = body[: 1 << 20]

    secret = request.query_params.get("secret", "")
    signature = request.headers.get(
        "X-Hub-Signature-256",
        "",
    )

    if not verify_scm_signature(
        secret,
        body,
        signature,
    ):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "bad_signature",
                "message": "chữ ký không hợp lệ",
            },
        )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "payload không phải JSON hợp lệ",
            },
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_body",
                "message": "payload không phải JSON hợp lệ",
            },
        )

    linked = 0
    for note in scm_notes(payload, project.key):
        if await link_scm_note(
            self,
            project_id,
            note,
        ):
            linked += 1

    return {"linked": linked}
