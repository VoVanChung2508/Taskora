"""
Integration tests — Python port of `store_test` (Go) in
`internal/store/integration_test.go`.

These run against a real PostgreSQL instance so the SQL itself is
exercised — unit tests elsewhere only cover pure logic.

They are skipped unless TEST_DATABASE_URL is set, e.g.

    TEST_DATABASE_URL='postgresql://flowie:...@localhost:5432/flowie_test' \\
        pytest internal/store/test_integration.py

Point it at a throwaway database: every test creates and removes its own
workspace, and the schema is migrated on connect.

Assumes a `flowie` package that mirrors the Go layout:
  - flowie.db: connect(dsn) -> asyncpg.Pool, migrate(pool)
  - flowie.store: Store(pool) aggregating sub-stores (.users, .workspaces,
    .projects, .tasks, .worklogs, .sessions, .api_keys), plus
    generate_api_key() and dataclasses like CreateProjectParams,
    CreateTaskParams, StatusUpdateFields.
Adjust the imports below to match your actual module layout.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from flowie import db
from flowie.store import (
    Store,
    CreateProjectParams,
    CreateTaskParams,
    StatusUpdateFields,
    generate_api_key,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Pool + fixture setup
# ---------------------------------------------------------------------------

async def _test_pool():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set — skipping integration tests")
    pool = await db.connect(dsn)
    await db.migrate(pool)
    return pool


@dataclass
class Fixture:
    """
    Isolated workspace + project + user for one test; removed afterwards so
    tests never see each other's rows.
    """

    st: Store
    user_id: uuid.UUID
    ws_id: uuid.UUID
    project_id: uuid.UUID

    async def new_task(self, title: str):
        task = await self.st.tasks.create(
            CreateTaskParams(project_id=self.project_id, title=title, reporter_id=self.user_id)
        )
        assert task is not None, "create task"
        return task


async def _new_fixture(pool) -> Fixture:
    st = Store(pool)

    suffix = uuid.uuid4().hex[:8]
    user = await st.users.upsert_from_azure(
        f"test|{suffix}", f"itest-{suffix}@flowie.test", "Integration Test", "", False
    )
    ws = await st.workspaces.create(f"IT WS {suffix}", f"it-ws-{suffix}", user.id)
    proj = await st.projects.create(
        CreateProjectParams(
            workspace_id=ws.id,
            name="IT Project",
            key=f"IT{suffix[:3]}",
            created_by=user.id,
        )
    )

    return Fixture(st=st, user_id=user.id, ws_id=ws.id, project_id=proj.id)


@pytest_asyncio.fixture
async def fx():
    """Yields a Fixture, then cleans up: workspace delete cascades to
    projects, tasks and memberships."""
    pool = await _test_pool()
    f = await _new_fixture(pool)
    try:
        yield f
    finally:
        try:
            await f.st.workspaces.delete(f.ws_id)
        except Exception:
            pass
        await pool.execute("DELETE FROM users WHERE id = $1", f.user_id)
        await pool.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_integration_task_lifecycle(fx: Fixture):
    task = await fx.new_task("First task")
    assert task.number == 1, f"first task number = {task.number}, want 1 (trigger should assign it)"

    second = await fx.new_task("Second task")
    assert second.number == 2, f"second task number = {second.number}, want 2"

    # The per-project number must resolve back to the same task.
    found = await fx.st.tasks.by_project_number(fx.project_id, "2")
    assert found.id == second.id, f"by_project_number returned {found.id}, want {second.id}"

    await fx.st.tasks.update_status(task.id, "done")

    stats = await fx.st.tasks.project_stats(fx.project_id)
    assert stats.total == 2 and stats.done == 1, (
        f"stats total/done = {stats.total}/{stats.done}, want 2/1"
    )


async def test_integration_dependency_cycle_is_rejected(fx: Fixture):
    a = await fx.new_task("A")
    b = await fx.new_task("B")

    await fx.st.tasks.add_dependency(a.id, b.id)

    # B -> A would close the loop and must be refused by the recursive check.
    with pytest.raises(Exception):
        await fx.st.tasks.add_dependency(b.id, a.id)

    with pytest.raises(Exception):
        await fx.st.tasks.add_dependency(a.id, a.id)

    deps = await fx.st.tasks.list_dependencies(a.id)
    assert len(deps.blocked_by) == 1 and deps.blocked_by[0].id == b.id, (
        f"blocked_by = {deps.blocked_by}, want exactly B"
    )


async def test_integration_workflow_statuses_seed_and_wip(fx: Fixture):
    await fx.st.tasks.seed_default_statuses(fx.project_id)
    statuses = await fx.st.tasks.list_statuses(fx.project_id)
    assert len(statuses) == 4, f"got {len(statuses)} statuses, want the 4 defaults"

    # Seeding twice must stay idempotent (ON CONFLICT DO NOTHING).
    await fx.st.tasks.seed_default_statuses(fx.project_id)
    again = await fx.st.tasks.list_statuses(fx.project_id)
    assert len(again) == 4, f"re-seeding produced {len(again)} statuses, want 4"

    todo_id = None
    for s in statuses:
        if s.key == "todo":
            todo_id = s.id

    await fx.st.tasks.update_workflow_status(
        fx.project_id, todo_id, StatusUpdateFields(set_wip_limit=True, wip_limit=1)
    )
    got = await fx.st.tasks.wip_limit_for(fx.project_id, "todo")
    assert got == 1, f"wip_limit_for = {got}, want 1"


async def test_integration_timer_produces_worklog(fx: Fixture):
    task = await fx.new_task("Timed task")

    await fx.st.worklogs.start_timer(fx.user_id, task.id, "working")

    # A second timer for the same user must be refused.
    with pytest.raises(Exception):
        await fx.st.worklogs.start_timer(fx.user_id, task.id, "")

    wl = await fx.st.worklogs.stop_timer(fx.user_id, "")
    assert wl.source == "timer", f"worklog source = {wl.source!r}, want timer"
    assert wl.minutes >= 1, f"worklog minutes = {wl.minutes}, want at least 1"
    assert wl.note == "working", f"note = {wl.note!r}, want the note captured at start"

    # Stopping again has nothing to stop.
    with pytest.raises(Exception):
        await fx.st.worklogs.stop_timer(fx.user_id, "")


async def test_integration_custom_fields_scoped_to_project(fx: Fixture):
    task = await fx.new_task("With fields")

    field_def = await fx.st.tasks.create_custom_field_def(
        fx.project_id, "Env", "dropdown", '["DEV","PROD"]'
    )
    await fx.st.tasks.set_custom_field_value(task.id, field_def.id, '"PROD"')

    values = await fx.st.tasks.list_custom_field_values(task.id)
    assert len(values) == 1 and values[0].value == '"PROD"', (
        f"values = {values}, want one PROD value"
    )

    # A field id from another project must not be settable on this task.
    pool2 = await _test_pool()
    other = await _new_fixture(pool2)
    try:
        other_def = await other.st.tasks.create_custom_field_def(
            other.project_id, "X", "text", None
        )
        with pytest.raises(Exception):
            await fx.st.tasks.set_custom_field_value(task.id, other_def.id, '"leak"')
    finally:
        try:
            await other.st.workspaces.delete(other.ws_id)
        except Exception:
            pass
        await pool2.execute("DELETE FROM users WHERE id = $1", other.user_id)
        await pool2.close()


async def test_integration_session_revocation(fx: Fixture):
    token_hash = "integration-test-token-hash"

    await fx.st.sessions.create(
        fx.user_id,
        token_hash,
        "TestAgent",
        "127.0.0.1",
        datetime.now(timezone.utc) + timedelta(hours=1),
    )

    revoked = await fx.st.sessions.is_revoked(token_hash)
    assert not revoked, "new session should be active"

    sessions = await fx.st.sessions.list_for_user(fx.user_id)
    assert len(sessions) == 1, f"list_for_user = {len(sessions)} sessions"

    await fx.st.sessions.revoke(fx.user_id, sessions[0].id)

    revoked = await fx.st.sessions.is_revoked(token_hash)
    assert revoked, "session should read as revoked"

    # Unknown tokens must not be treated as revoked, so pre-existing JWTs work.
    r = await fx.st.sessions.is_revoked("never-seen")
    assert not r, "an unknown token must not be reported as revoked"


async def test_integration_api_key_resolve_and_revoke(fx: Fixture):
    plaintext = generate_api_key()

    key = await fx.st.api_keys.create(fx.ws_id, fx.user_id, "itest", ["read"], plaintext)
    assert key.active, "a freshly created key must be active"

    resolved = await fx.st.api_keys.resolve(plaintext)
    assert resolved.workspace_id == fx.ws_id, (
        f"key resolved to workspace {resolved.workspace_id}, want {fx.ws_id}"
    )
    assert resolved.has_scope("read") and not resolved.has_scope("write"), (
        f"scopes = {resolved.scopes}, want read only"
    )

    await fx.st.api_keys.revoke(fx.ws_id, key.id)

    with pytest.raises(Exception):
        await fx.st.api_keys.resolve(plaintext)


async def test_integration_gdpr_anonymisation_keeps_history(fx: Fixture):
    task = await fx.new_task("Task with history")
    await fx.st.tasks.add_comment(task.id, fx.user_id, "my comment")

    bundle = await fx.st.users.export_data(fx.user_id)
    for key in ("profile", "comments", "workspaceMemberships"):
        assert key in bundle, f"export is missing the {key!r} section"

    await fx.st.users.anonymise_account(fx.user_id)

    user = await fx.st.users.get_by_id(fx.user_id)
    assert user is not None, "user row must survive erasure"
    assert not user.is_active, "erased account must be deactivated"
    assert user.email and user.email.startswith("deleted-"), (
        f"email = {user.email!r}, want an anonymised placeholder"
    )

    # The comment stays for the team but loses its author.
    comments = await fx.st.tasks.list_comments(task.id)
    assert len(comments) == 1, f"comment history should survive, got {len(comments)}"
    assert comments[0].author_id is None, "comment author must be detached after erasure"