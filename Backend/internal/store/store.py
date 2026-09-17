"""Aggregate repository handle built on top of an asyncpg connection pool."""

import asyncpg


class NotFoundError(Exception):
    """Raised when a queried row does not exist."""
    pass


class InviteEmailMismatchError(Exception):
    """Raised when an invite link is redeemed by someone other than the
    address it was issued to."""
    pass


class Store:
    """The aggregate repository handle."""

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
    """Builds a Store from an asyncpg pool."""
    return Store(pool)