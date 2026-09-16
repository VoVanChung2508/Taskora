"""Core entity types shared across layers.

Python port of the Go `domain` package. Requires:
    pip install pydantic>=2

Every model's Python field names are snake_case; JSON serialization uses
lowerCamelCase keys that match the Go `json` tags exactly (via a custom
alias generator), so `model.model_dump(by_alias=True)` round-trips with
the original API's wire format.

Go's `json:"foo,omitempty"` has no single Pydantic equivalent — the closest
match is calling `model_dump(by_alias=True, exclude_none=True)` when
serializing, which drops `None` fields the same way `omitempty` drops zero
values for pointer fields here.

Go's struct embedding (e.g. `TaskListItem` embeds `Task`) is modeled as
Python subclassing, which flattens the parent's fields into the subclass
the same way Go's embedding flattens fields into the JSON object.

`json.RawMessage` fields (arbitrary, already-encoded JSON) are typed
`Any` here rather than preserved as raw bytes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


def _to_camel(name: str) -> str:
    """snake_case -> lowerCamelCase, matching the Go json tags."""
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


class DomainModel(BaseModel):
    """Base class for all domain models: lowerCamelCase JSON keys, but
    snake_case attributes in Python code.
    """

    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True)


# --------------------------------------------------------------------------
# Users, sessions, audit
# --------------------------------------------------------------------------


class User(DomainModel):
    """A person authenticated via Azure AD SSO."""

    id: uuid.UUID
    azure_oid: str = Field(default="", exclude=True)  # json:"-"
    email: str
    display_name: str
    avatar_url: str
    is_system_admin: bool
    is_active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class AuditEntry(DomainModel):
    """One security-relevant action recorded for compliance review."""

    id: uuid.UUID
    actor_id: Optional[uuid.UUID] = None
    actor_email: str
    workspace_id: Optional[uuid.UUID] = None
    action: str
    target: str
    ip: str
    meta: Dict[str, Any]
    created_at: datetime


class UserSession(DomainModel):
    """An issued login session, shown in the device list so users can
    revoke access remotely (Module 1.1).
    """

    id: uuid.UUID
    device: str
    ip: str
    last_seen: datetime
    expires_at: datetime
    created_at: datetime
    current: bool


# --------------------------------------------------------------------------
# Workspaces, roles, members, teams
# --------------------------------------------------------------------------


class WorkspaceRole(str, Enum):
    """Organisation-level roles (Module 1.2)."""

    OWNER = "owner"
    ADMIN = "admin"
    BILLING = "billing"
    MEMBER = "member"
    GUEST = "guest"


class Workspace(DomainModel):
    """The top-level organisation container."""

    id: uuid.UUID
    name: str
    slug: str
    share_point_folder_path: str
    share_point_item_id: str = Field(default="", exclude=True)  # json:"-"
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class MemberInfo(DomainModel):
    """A workspace member with user profile + billing rate."""

    user_id: uuid.UUID
    display_name: str
    email: str
    avatar_url: Optional[str] = None
    role: WorkspaceRole
    hourly_rate: float
    currency: str


# Permission is a fine-grained capability that a custom role may grant
# (Module 1.2). Kept as a plain string so roles stay data-driven.
Permission = str

# AllPermissions is the catalogue offered when building a custom role.
AllPermissions: List[Permission] = [
    "task.create", "task.edit", "task.delete",
    "comment.create", "comment.delete",
    "worklog.log", "worklog.approve",
    "sprint.manage", "automation.manage",
    "member.manage", "role.manage", "team.manage",
    "budget.view", "project.manage",
]


class CustomRole(DomainModel):
    """A workspace-defined role with an explicit permission set."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    permissions: List[Permission]
    created_at: datetime


class WorkspaceInvite(DomainModel):
    """Pre-authorises an email address to join a workspace."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    email: str
    role: WorkspaceRole
    invited_by: Optional[uuid.UUID] = None
    expires_at: datetime
    accepted_at: Optional[datetime] = None
    expired: bool
    created_at: datetime


class TeamMember(DomainModel):
    """A user belonging to a team."""

    user_id: uuid.UUID
    display_name: str
    email: str


class Team(DomainModel):
    """Groups workspace members into a department (Module 1.3)."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    created_at: datetime
    members: List[TeamMember]


# --------------------------------------------------------------------------
# Projects, tasks, sprints
# --------------------------------------------------------------------------


class ProjectStatus(str, Enum):
    """Project lifecycle states."""

    ACTIVE = "active"
    ARCHIVED = "archived"


class ProjectRole(str, Enum):
    """Project-level roles (Module 1.2)."""

    MANAGER = "manager"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"


class Project(DomainModel):
    """A project inside a workspace."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    portfolio_id: Optional[uuid.UUID] = None
    name: str
    key: str
    description: str
    status: ProjectStatus
    share_point_folder_path: str
    share_point_item_id: str = Field(default="", exclude=True)  # json:"-"
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime


class Task(DomainModel):
    """A unit of work within a project."""

    id: uuid.UUID
    project_id: uuid.UUID
    # number is the per-project counter used in references like "SAP-12".
    number: Optional[int] = None
    parent_task_id: Optional[uuid.UUID] = None
    title: str
    description: str
    status: str
    priority: str
    assignee_id: Optional[uuid.UUID] = None
    reporter_id: Optional[uuid.UUID] = None
    participant_ids: Optional[List[uuid.UUID]] = None
    story_points: Optional[float] = None
    start_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    position: float
    sprint_id: Optional[uuid.UUID] = None
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None

    # Backlog prioritisation (Module 3.2)
    moscow: Optional[str] = None  # must | should | could | wont
    rice_reach: Optional[float] = None
    rice_impact: Optional[float] = None
    rice_confidence: Optional[float] = None
    rice_effort: Optional[float] = None
    rice_score: Optional[float] = None  # generated column

    created_at: datetime
    updated_at: datetime


class SprintState(str, Enum):
    """Sprint lifecycle states."""

    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"


class Sprint(DomainModel):
    """A time-boxed iteration within a project."""

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    goal: str
    state: SprintState
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    position: float
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Analytics: burndown, velocity, capacity, dashboards
# --------------------------------------------------------------------------


class BurndownPoint(DomainModel):
    """One day on a sprint burndown chart (Module 5.1)."""

    date: str  # "2026-07-25"
    remaining: float
    remaining_tasks: int
    ideal: float


class SprintBurndown(DomainModel):
    """The burndown series for one sprint."""

    sprint_id: uuid.UUID
    name: str
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    total_points: float
    total_tasks: int
    done_points: float
    done_tasks: int
    points: List[BurndownPoint]


class VelocityPoint(DomainModel):
    """One completed sprint on the velocity chart."""

    sprint_id: uuid.UUID
    name: str
    state: str
    committed: float
    completed: float
    committed_tasks: int
    completed_tasks: int


class AssigneeCapacity(DomainModel):
    """One person's share of a sprint."""

    user_id: Optional[uuid.UUID] = None
    display_name: str
    points: float
    tasks: int
    done_tasks: int


class SprintCapacity(DomainModel):
    """Summarises a sprint's load, overall and per assignee."""

    sprint_id: uuid.UUID
    total_points: float
    total_tasks: int
    done_points: float
    done_tasks: int
    by_assignee: List[AssigneeCapacity]


class SavedView(DomainModel):
    """Stores a board's filter/sort state so it can be recalled (Module 4)."""

    id: uuid.UUID
    project_id: uuid.UUID
    owner_id: Optional[uuid.UUID] = None
    shared: bool
    name: str
    config: Dict[str, Any]
    created_at: datetime


class WorkflowStatus(DomainModel):
    """A board column defined per project (Module 3.1)."""

    id: uuid.UUID
    project_id: uuid.UUID
    key: str
    name: str
    category: str  # todo | in_progress | done
    color: str
    position: float
    wip_limit: Optional[int] = None
    # Count of tasks currently in this column (filled by the board endpoint).
    task_count: int


class Label(DomainModel):
    """A project-scoped tag."""

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    color: str


class TaskListItem(Task):
    """A Task enriched with aggregates for board/list rendering."""

    labels: List[Label]
    comment_count: int
    checklist_total: int
    checklist_done: int
    subtask_count: int


class ProjectStats(DomainModel):
    """Aggregates a project's metrics for analytics dashboards."""

    by_status: Dict[str, int]
    by_priority: Dict[str, int]
    total: int
    done: int
    story_points_total: float
    story_points_done: float
    hours_logged: float
    cost_actual: float


class DashboardStats(DomainModel):
    """Aggregates a user's cross-workspace metrics."""

    workspace_count: int
    project_count: int
    open_tasks: int
    due_soon: int
    hours_this_week: float


class DashboardWidget(DomainModel):
    """One card on a custom dashboard (Module 5.1)."""

    id: uuid.UUID
    dashboard_id: uuid.UUID
    type: str
    title: str
    config: Dict[str, Any]
    position: float
    width: int


class Dashboard(DomainModel):
    """A user-defined (or workspace-shared) set of widgets."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    owner_id: Optional[uuid.UUID] = None
    shared: bool
    name: str
    created_at: datetime
    widgets: List[DashboardWidget]


class TrendPoint(DomainModel):
    """One month of activity used by dashboard charts (Module 5.1)."""

    month: str  # "2026-07"
    created: int
    completed: int
    in_work: int
    hours: float


class ProjectSummary(DomainModel):
    """A per-project rollup shown on the workspace dashboard."""

    project_id: uuid.UUID
    key: str
    name: str
    status: str
    total: int
    done: int
    in_progress: int
    todo: int
    overdue: int
    hours_logged: float
    cost_actual: float


class AssigneeLoad(DomainModel):
    """A per-person rollup shown on the project dashboard.

    Avatars are intentionally omitted: they are stored as base64 data URIs
    (~20KB each) and would dominate the dashboard payload.
    """

    user_id: Optional[uuid.UUID] = None
    display_name: str
    total: int
    done: int
    overdue: int
    hours_logged: float


class StatusMeta(DomainModel):
    """A workflow column's display name and colour so charts can render a
    project-defined status correctly.

    Without it the frontend fell back to a hard-coded map of the four
    built-in statuses, so any column added in project settings charted as a
    grey slice labelled with its raw key.
    """

    key: str
    label: str
    color: str  # hex ("#3b82f6"), or a legacy palette name


class WorkspaceOverview(DomainModel):
    """Powers the workspace dashboard charts (Module 5.1)."""

    total_tasks: int
    done_tasks: int
    in_progress_tasks: int
    backlog_tasks: int
    overdue_tasks: int
    project_count: int
    member_count: int
    hours_logged: float
    cost_actual: float
    created_delta: float  # % vs previous 30d
    completed_delta: float  # % vs previous 30d
    by_status: Dict[str, int]
    by_priority: Dict[str, int]
    projects: List[ProjectSummary]
    trend: List[TrendPoint]
    status_meta: List[StatusMeta]


class ProjectOverview(ProjectStats):
    """Powers the per-project dashboard (Module 5.1)."""

    overdue_tasks: int
    created_delta: float
    completed_delta: float
    trend: List[TrendPoint]
    assignees: List[AssigneeLoad]
    status_meta: List[StatusMeta]


class CalendarItem(DomainModel):
    """A lightweight task projection for calendar/timeline views."""

    id: uuid.UUID
    title: str
    status: str
    priority: str
    start_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    assignee_id: Optional[uuid.UUID] = None
    assignee_name: Optional[str] = None
    assignee_avatar: Optional[str] = None
    participant_ids: Optional[List[uuid.UUID]] = None
    project_id: uuid.UUID
    project_key: str
    project_name: str


# --------------------------------------------------------------------------
# Custom fields, dependencies, critical path
# --------------------------------------------------------------------------


class CustomFieldDef(DomainModel):
    """A project-scoped custom field definition (Module 3.4)."""

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    field_type: str  # text, number, dropdown, date, url
    options: Optional[Any] = None


class CustomFieldValue(DomainModel):
    """A field definition paired with a task's value (nullable)."""

    field_id: uuid.UUID
    name: str
    field_type: str
    options: Optional[Any] = None
    value: Optional[Any] = None


class CriticalPathItem(DomainModel):
    """One task's CPM schedule figures (Module 4.3)."""

    task_id: uuid.UUID
    title: str
    status: str
    duration: float  # days
    earliest_start: float
    earliest_finish: float
    latest_start: float
    latest_finish: float
    slack: float
    critical: bool


class CriticalPath(DomainModel):
    """The CPM analysis of a project's dependency graph."""

    project_duration_days: float
    critical_task_ids: List[uuid.UUID]
    items: List[CriticalPathItem]


class TaskDependencyItem(DomainModel):
    """A task referenced by a dependency edge (Module 3.4)."""

    id: uuid.UUID
    title: str
    status: str
    priority: str
    project_key: str


class TaskDependencies(DomainModel):
    """Groups a task's dependency edges: tasks that block it (blocked_by)
    and tasks it blocks (blocks).
    """

    blocked_by: List[TaskDependencyItem]
    blocks: List[TaskDependencyItem]


# --------------------------------------------------------------------------
# Comments, checklists, worklogs, timers
# --------------------------------------------------------------------------


class Comment(DomainModel):
    """A message on a task."""

    id: uuid.UUID
    task_id: uuid.UUID
    author_id: Optional[uuid.UUID] = None
    author_name: str
    author_email: str
    body: str
    created_at: datetime


class ChecklistItem(DomainModel):
    """A sub-item of a task's checklist."""

    id: uuid.UUID
    task_id: uuid.UUID
    title: str
    done: bool
    position: float
    created_at: datetime


class Worklog(DomainModel):
    """A time entry logged against a task."""

    id: uuid.UUID
    task_id: uuid.UUID
    user_id: uuid.UUID
    minutes: int
    note: str
    logged_on: datetime
    source: str
    state: str
    created_at: datetime


class ActiveTimer(DomainModel):
    """A running stopwatch on a task (Module 3.3)."""

    user_id: uuid.UUID
    task_id: uuid.UUID
    task_title: str
    project_id: uuid.UUID
    project_key: str
    note: str
    started_at: datetime
    elapsed_secs: int


class TimesheetEntry(Worklog):
    """A worklog enriched with task/project context for grids."""

    task_title: str
    project_id: uuid.UUID
    project_name: str
    project_key: str
    user_display_name: Optional[str] = None
    user_email: Optional[str] = None


# --------------------------------------------------------------------------
# Automation
# --------------------------------------------------------------------------

# Automation trigger types (Module 6.1).
TRIGGER_STATUS_CHANGED = "status_changed"
TRIGGER_TASK_CREATED = "task_created"


class AutomationCondition(DomainModel):
    """Narrows when a rule fires, e.g. {priority, eq, "high"}.
    An empty condition list means "always".
    """

    field: str  # priority | status | assignee | story_points
    op: str  # eq | neq | is_empty | not_empty | gt | lt
    value: str


class AutomationAction(DomainModel):
    """One effect applied when a rule fires."""

    type: str  # assign | set_status | set_priority | notify
    user_id: Optional[str] = None  # assign / notify target
    value: Optional[str] = None  # set_status / set_priority value
    message: Optional[str] = None


class AutomationRule(DomainModel):
    """A Trigger -> Condition -> Action rule.

    trigger_status/action_type/action_assignee_id are the original v1
    columns, kept so existing rules and the old UI keep working; the
    engine reads `actions`.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    trigger_type: str

    trigger_status: str
    action_type: str
    action_assignee_id: Optional[uuid.UUID] = None

    conditions: List[AutomationCondition]
    actions: List[AutomationAction]

    active: bool
    created_at: datetime


class ScheduledReport(DomainModel):
    """Delivers a recurring summary to a chat channel (Module 5.2)."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    project_id: Optional[uuid.UUID] = None
    name: str
    frequency: str  # daily | weekly
    channel_url: str
    provider: str  # slack | teams
    hour_utc: int
    active: bool
    last_run_at: Optional[datetime] = None
    last_status: Optional[int] = None
    last_error: Optional[str] = None
    created_at: datetime


class Integration(DomainModel):
    """Posts project events to Slack or MS Teams (Module 6.3)."""

    id: uuid.UUID
    project_id: uuid.UUID
    provider: str  # slack | teams
    webhook_url: str
    events: List[str]  # empty = all
    active: bool
    last_status: Optional[int] = None
    last_error: Optional[str] = None
    created_at: datetime


class APIKey(DomainModel):
    """A workspace-scoped credential for third-party access (Module 6.2).
    The secret itself is never returned after creation.
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    prefix: str
    scopes: List[str]
    active: bool
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: datetime


class Webhook(DomainModel):
    """An outgoing HTTP endpoint notified of project events (Module 6.2)."""

    id: uuid.UUID
    project_id: uuid.UUID
    url: str
    events: List[str]  # empty = all events
    active: bool
    last_status: Optional[int] = None
    last_error: Optional[str] = None
    last_sent_at: Optional[datetime] = None
    created_at: datetime
    # secret is write-only: never returned to clients.
    secret: str = Field(default="", exclude=True)  # json:"-"
    # has_secret tells the UI whether signing is configured.
    has_secret: bool


class Notification(DomainModel):
    """An in-app notification for a user."""

    id: uuid.UUID
    user_id: uuid.UUID
    type: str
    title: str
    body: str
    task_id: Optional[uuid.UUID] = None
    # link is the frontend path to open, e.g. "/projects/{id}?task={id}".
    link: Optional[str] = None
    read_at: Optional[datetime] = None
    created_at: datetime


class Attachment(DomainModel):
    """A file stored in SharePoint and linked to a task (Module 3.5)."""

    id: uuid.UUID
    task_id: uuid.UUID
    uploaded_by: Optional[uuid.UUID] = None
    uploader_name: str
    name: str
    size_bytes: int
    content_type: str
    drive_item_id: str
    web_url: str
    folder_path: str
    created_at: datetime


# --------------------------------------------------------------------------
# Chat, activity
# --------------------------------------------------------------------------


class ChatChannel(DomainModel):
    """A per-project conversation (Module 7.2)."""

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    created_at: datetime
    unread: int


class ChatMessage(DomainModel):
    """A single message in a channel."""

    id: uuid.UUID
    channel_id: uuid.UUID
    author_id: Optional[uuid.UUID] = None
    author_name: str
    author_email: str
    body: str
    created_at: datetime


class ActivityEvent(DomainModel):
    """An audit entry on a task."""

    id: uuid.UUID
    task_id: uuid.UUID
    actor_id: Optional[uuid.UUID] = None
    actor_name: str
    verb: str
    meta: Optional[Any] = None
    created_at: datetime