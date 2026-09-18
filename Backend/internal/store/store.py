"""Aggregate repository handle built on top of an asyncpg connection pool."""

from __future__ import annotations

import asyncpg

# ---------------------------------------------------------------------
# User store
# ---------------------------------------------------------------------

from .users import UserStore as CoreUserStore
from .privacy import UserStore as PrivacyUserStore
from .twofactor import UserStore as TwoFactorUserStore

# ---------------------------------------------------------------------
# Workspace store
# ---------------------------------------------------------------------

from .workspaces import WorkspaceStore as CoreWorkspaceStore
from .iam import WorkspaceStore as IAMWorkspaceStore

# ---------------------------------------------------------------------
# Project store
# ---------------------------------------------------------------------

from .projects import ProjectStore

# ---------------------------------------------------------------------
# Task store
#
# Trong Go, các method của TaskStore nằm rải ở nhiều file.
# Python phải ghép lại thành một class.
# ---------------------------------------------------------------------

from .tasks import TaskStore as CoreTaskStore
from .taskextras import TaskStore as TaskExtrasStore
from .taskdeps import TaskStore as TaskDepsStore
from .workflow import TaskStore as WorkflowTaskStore
from .stats import TaskStore as StatsTaskStore
from .overview import TaskStore as OverviewTaskStore
from .criticalpath import TaskStore as CriticalPathTaskStore
from .scm import TaskStore as SCMTaskStore
from .customfields import TaskStore as CustomFieldsTaskStore

# ---------------------------------------------------------------------
# Sprint store
# ---------------------------------------------------------------------

from .sprints import SprintStore as CoreSprintStore
from .agile import SprintStore as AgileSprintStore

# ---------------------------------------------------------------------
# Worklog store
# ---------------------------------------------------------------------

from .worklogs import WorklogStore as CoreWorklogStore
from .timers import WorklogStore as TimerWorklogStore

# ---------------------------------------------------------------------
# Stores chỉ có một implementation
# ---------------------------------------------------------------------

from .notifications import NotificationStore
from .automation import AutomationStore
from .chat import ChatStore
from .sessions import SessionStore
from .webhooks import WebhookStore
from .dashboards import DashboardStore
from .apikeys import APIKeyStore
from .integrations import IntegrationStore
from .savedviews import SavedViewStore
from .audit import AuditStore
from .reports import ReportStore
from .attachments import AttachmentStore
from .invites import InviteStore


class NotFoundError(Exception):
    """Raised when a queried row does not exist."""


class InviteEmailMismatchError(Exception):
    """Raised when an invite is redeemed by another email address."""


# ---------------------------------------------------------------------
# Unified Store classes
# ---------------------------------------------------------------------


class UserStore(
    CoreUserStore,
    PrivacyUserStore,
    TwoFactorUserStore,
):
    """Unified UserStore assembled from the original Go package files."""

    pass


class WorkspaceStore(
    CoreWorkspaceStore,
    IAMWorkspaceStore,
):
    """Unified WorkspaceStore."""

    pass


class TaskStore(
    CoreTaskStore,
    TaskExtrasStore,
    TaskDepsStore,
    WorkflowTaskStore,
    StatsTaskStore,
    OverviewTaskStore,
    CriticalPathTaskStore,
    SCMTaskStore,
    CustomFieldsTaskStore,
):
    """Unified TaskStore."""

    pass


class SprintStore(
    CoreSprintStore,
    AgileSprintStore,
):
    """Unified SprintStore."""

    pass


class WorklogStore(
    CoreWorklogStore,
    TimerWorklogStore,
):
    """Unified WorklogStore."""

    pass


class Store:
    """Aggregate repository handle."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

        self.users = UserStore(pool)
        self.workspaces = WorkspaceStore(pool)
        self.projects = ProjectStore(pool)

        self.tasks = TaskStore(pool)
        self.sprints = SprintStore(pool)
        self.worklogs = WorklogStore(pool)

        self.notifications = NotificationStore(pool)
        self.automations = AutomationStore(pool)
        self.chat = ChatStore(pool)
        self.sessions = SessionStore(pool)
        self.webhooks = WebhookStore(pool)
        self.dashboards = DashboardStore(pool)
        self.api_keys = APIKeyStore(pool)
        self.integrations = IntegrationStore(pool)
        self.saved_views = SavedViewStore(pool)
        self.audit = AuditStore(pool)
        self.reports = ReportStore(pool)
        self.attachments = AttachmentStore(pool)
        self.invites = InviteStore(pool)


def new(pool: asyncpg.Pool) -> Store:
    """Build a Store from an asyncpg connection pool."""

    return Store(pool)