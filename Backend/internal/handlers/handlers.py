from __future__ import annotations

from typing import Optional

from ..auth.azure import AzureProvider
from ..auth.session import SessionManager
from ..config.config import Config
from ..realtime.hub import Hub
from ..storage.sharepoint.upload import Client as SharePointClient
from ..store.store import Store

from .health import health

from .notification_handler import (
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
)

from .rbac import (
    require_project_access,
    require_task_access,
    require_workspace_member,
)

from .permission import (
    default_role_grants,
    has_permission,
    require_permission,
)

from .savedview_handler import (
    list_saved_views,
    create_saved_view,
    delete_saved_view,
)

from .workspace_handler import (
    create_workspace,
    list_workspaces,
    get_workspace,
)

from .member_handler import (
    list_members,
    project_members,
    add_member,
    update_member,
    set_member_rate,
)

from .project_handler import (
    create_project,
    list_projects,
    get_project,
    provision_project_folder,
)

from .sprint_handler import (
    create_sprint,
    list_sprints,
    sprint_burndown,
    sprint_capacity,
    project_velocity,
    update_sprint,
    set_task_sprint,
    require_sprint_access,
    sprint_name,
)

from .workflow_handler import (
    list_statuses,
    create_status,
    update_status,
    delete_status,
    check_wip_limit,
)

from .task_handler import (
    create_task,
    delete_task,
    list_tasks,
    update_task_status,
    get_task,
    update_task,
    add_comment,
    add_checklist_item,
    toggle_checklist_item,
    list_labels,
    create_label,
    set_task_label,
)

from .dependency_handler import (
    list_task_dependencies,
    add_task_dependency,
    remove_task_dependency,
)

from .worklog_handler import (
    log_work,
    list_task_worklogs,
    my_timesheet,
    project_timesheet,
    my_calendar,
    submit_timesheet,
    set_worklog_state,
)

from .timer_handler import (
    start_timer,
    get_active_timer,
    stop_timer,
    cancel_timer,
)

from .customfield_handler import (
    list_custom_fields,
    create_custom_field,
    delete_custom_field,
    set_task_custom_field,
)

from .iam_handler import (
    require_workspace_manager,
    list_permissions,
    list_custom_roles,
    create_custom_role,
    update_custom_role,
    delete_custom_role,
    assign_custom_role,
    list_teams,
    create_team,
    delete_team,
    set_team_member,
)

from .integration_handler import (
    event_headline,
    chat_payload,
    dispatch_integrations,
    list_integrations,
    create_integration,
    delete_integration,
)

from .webhook_handler import (
    sign_payload,
    dispatch_webhooks,
    emit,
    list_webhooks,
    create_webhook,
    delete_webhook,
)

from .apikey_handler import (
    list_api_keys,
    create_api_key,
    revoke_api_key,
    require_api_key,
    api_projects,
    api_tasks,
    api_create_task,
)

from .scm_handler import (
    verify_scm_signature,
    refs_for,
    first_line,
    scm_notes,
    link_scm_note,
    scm_webhook,
)

from .auth_handler import (
    azure_login,
    azure_callback,
    dev_login,
    logout,
    list_sessions,
    revoke_session,
    dev_make_admin,
    me,
    record_session,
    ensure_development_workspace,
)

from .session_rotate import refresh_session

from .twofactor_handler import (
    pending_user_id,
    two_factor_status,
    start_two_factor,
    enable_two_factor,
    disable_two_factor,
    verify_two_factor,
)

from .privacy_handler import (
    export_my_data,
    delete_my_data,
)

from .invite_handler import (
    list_invites,
    create_invite,
    revoke_invite,
    accept_invite,
)

from .audit_handler import (
    client_ip,
    audit,
    audit_for,
    list_audit_log,
    admin_audit_log,
)

from .admin_handler import (
    atoi_or,
    clamp_int,
    require_admin,
    admin_list_users,
    admin_toggle_user,
    admin_list_workspaces,
    admin_create_workspace,
    admin_delete_workspace,
    admin_sync_azure_users,
)

from .dashboard_handler import (
    require_dashboard_access,
    list_dashboards,
    create_dashboard,
    delete_dashboard,
    add_widget,
    delete_widget,
)

from .stats_handler import (
    dashboard,
    project_stats,
    workspace_overview,
    project_overview,
    project_critical_path,
)

from .report_handler import (
    build_report_text,
    send_report,
    start_report_scheduler,
    list_reports,
    create_report,
    delete_report,
    run_report_now,
)

from .automation_engine import (
    task_field_value,
    eval_condition,
    conditions_met,
    apply_action,
    run_automations,
)

from .automation_handler import (
    list_automations,
    create_automation,
    create_automation_v2,
    delete_automation,
)

from .chat_handler import (
    require_channel_access,
    list_channels,
    create_channel,
    delete_channel,
    list_messages,
    post_message,
    mark_channel_read,
    notify_chat_mentions,
)

from .events_handler import (
    project_events,
    publish,
)

from .attachment_handler import (
    safe_file_name,
    attachment_folder,
    browse_project_files,
    list_attachments,
    upload_attachment,
    delete_attachment,
)


class Handlers:
    """Gom các dependency dùng chung cho toàn bộ HTTP handlers."""

    # Health
    health = health

    # Notifications
    list_notifications = list_notifications
    mark_all_notifications_read = mark_all_notifications_read
    mark_notification_read = mark_notification_read

    # RBAC
    require_project_access = require_project_access
    require_task_access = require_task_access
    require_workspace_member = require_workspace_member

    # Fine-grained permissions
    default_role_grants = staticmethod(default_role_grants)
    has_permission = has_permission
    require_permission = require_permission

    # Saved views
    list_saved_views = list_saved_views
    create_saved_view = create_saved_view
    delete_saved_view = delete_saved_view

    # Workspaces
    create_workspace = create_workspace
    list_workspaces = list_workspaces
    get_workspace = get_workspace

    # Members
    list_members = list_members
    project_members = project_members
    add_member = add_member
    update_member = update_member
    set_member_rate = set_member_rate

    # Projects
    create_project = create_project
    list_projects = list_projects
    get_project = get_project
    provision_project_folder = provision_project_folder

    # Sprints
    create_sprint = create_sprint
    list_sprints = list_sprints
    sprint_burndown = sprint_burndown
    sprint_capacity = sprint_capacity
    project_velocity = project_velocity
    update_sprint = update_sprint
    set_task_sprint = set_task_sprint
    require_sprint_access = require_sprint_access
    sprint_name = sprint_name

    # Workflow
    list_statuses = list_statuses
    create_status = create_status
    update_status = update_status
    delete_status = delete_status
    check_wip_limit = check_wip_limit

    # Tasks
    create_task = create_task
    delete_task = delete_task
    list_tasks = list_tasks
    update_task_status = update_task_status
    get_task = get_task
    update_task = update_task
    add_comment = add_comment
    add_checklist_item = add_checklist_item
    toggle_checklist_item = toggle_checklist_item
    list_labels = list_labels
    create_label = create_label
    set_task_label = set_task_label

    # Task dependencies
    list_task_dependencies = list_task_dependencies
    add_task_dependency = add_task_dependency
    remove_task_dependency = remove_task_dependency

    # Worklogs / timesheets
    log_work = log_work
    list_task_worklogs = list_task_worklogs
    my_timesheet = my_timesheet
    project_timesheet = project_timesheet
    my_calendar = my_calendar
    submit_timesheet = submit_timesheet
    set_worklog_state = set_worklog_state

    # Timers
    start_timer = start_timer
    get_active_timer = get_active_timer
    stop_timer = stop_timer
    cancel_timer = cancel_timer

    # Custom fields
    list_custom_fields = list_custom_fields
    create_custom_field = create_custom_field
    delete_custom_field = delete_custom_field
    set_task_custom_field = set_task_custom_field

    # IAM: permissions, custom roles, teams
    require_workspace_manager = require_workspace_manager
    list_permissions = list_permissions
    list_custom_roles = list_custom_roles
    create_custom_role = create_custom_role
    update_custom_role = update_custom_role
    delete_custom_role = delete_custom_role
    assign_custom_role = assign_custom_role
    list_teams = list_teams
    create_team = create_team
    delete_team = delete_team
    set_team_member = set_team_member

    # Integrations
    event_headline = staticmethod(event_headline)
    chat_payload = staticmethod(chat_payload)
    dispatch_integrations = dispatch_integrations
    list_integrations = list_integrations
    create_integration = create_integration
    delete_integration = delete_integration

    # Outgoing webhooks / event fan-out
    sign_payload = staticmethod(sign_payload)
    dispatch_webhooks = dispatch_webhooks
    emit = emit
    list_webhooks = list_webhooks
    create_webhook = create_webhook
    delete_webhook = delete_webhook

    # Workspace API keys / public API
    list_api_keys = list_api_keys
    create_api_key = create_api_key
    revoke_api_key = revoke_api_key
    require_api_key = require_api_key
    api_projects = api_projects
    api_tasks = api_tasks
    api_create_task = api_create_task

    # SCM webhooks
    verify_scm_signature = staticmethod(verify_scm_signature)
    refs_for = staticmethod(refs_for)
    first_line = staticmethod(first_line)
    scm_notes = staticmethod(scm_notes)
    link_scm_note = link_scm_note
    scm_webhook = scm_webhook

    # Auth / account
    azure_login = azure_login
    azure_callback = azure_callback
    dev_login = dev_login
    logout = logout
    me = me
    record_session = record_session
    ensure_development_workspace = ensure_development_workspace
    dev_make_admin = dev_make_admin

    # Session management
    refresh_session = refresh_session
    list_sessions = list_sessions
    revoke_session = revoke_session

    # 2FA
    pending_user_id = pending_user_id
    two_factor_status = two_factor_status
    start_two_factor = start_two_factor
    enable_two_factor = enable_two_factor
    disable_two_factor = disable_two_factor
    verify_two_factor = verify_two_factor

    # Privacy / GDPR
    export_my_data = export_my_data
    delete_my_data = delete_my_data

    # Workspace invites
    list_invites = list_invites
    create_invite = create_invite
    revoke_invite = revoke_invite
    accept_invite = accept_invite

    # Audit
    client_ip = staticmethod(client_ip)
    audit = audit
    audit_for = audit_for
    list_audit_log = list_audit_log
    admin_audit_log = admin_audit_log

    # System admin
    atoi_or = staticmethod(atoi_or)
    clamp_int = staticmethod(clamp_int)
    require_admin = require_admin
    admin_list_users = admin_list_users
    admin_toggle_user = admin_toggle_user
    admin_list_workspaces = admin_list_workspaces
    admin_create_workspace = admin_create_workspace
    admin_delete_workspace = admin_delete_workspace
    admin_sync_azure_users = admin_sync_azure_users

    # Dashboards
    require_dashboard_access = require_dashboard_access
    list_dashboards = list_dashboards
    create_dashboard = create_dashboard
    delete_dashboard = delete_dashboard
    add_widget = add_widget
    delete_widget = delete_widget

    # Stats / overview / critical path
    dashboard = dashboard
    project_stats = project_stats
    workspace_overview = workspace_overview
    project_overview = project_overview
    project_critical_path = project_critical_path

    # Scheduled reports
    build_report_text = build_report_text
    send_report = send_report
    start_report_scheduler = start_report_scheduler
    list_reports = list_reports
    create_report = create_report
    delete_report = delete_report
    run_report_now = run_report_now

    # Automation engine / rules
    task_field_value = staticmethod(task_field_value)
    eval_condition = staticmethod(eval_condition)
    conditions_met = staticmethod(conditions_met)
    apply_action = apply_action
    run_automations = run_automations
    list_automations = list_automations
    create_automation = create_automation
    create_automation_v2 = create_automation_v2
    delete_automation = delete_automation

    # Project chat
    require_channel_access = require_channel_access
    list_channels = list_channels
    create_channel = create_channel
    delete_channel = delete_channel
    list_messages = list_messages
    post_message = post_message
    mark_channel_read = mark_channel_read
    notify_chat_mentions = notify_chat_mentions

    # Realtime SSE
    project_events = project_events
    publish = publish

    # Attachments / SharePoint
    safe_file_name = staticmethod(safe_file_name)
    attachment_folder = staticmethod(attachment_folder)
    browse_project_files = browse_project_files
    list_attachments = list_attachments
    upload_attachment = upload_attachment
    delete_attachment = delete_attachment

    def __init__(
        self,
        cfg: Config,
        store: Store,
        sessions: SessionManager,
        azure: Optional[AzureProvider],
        sharepoint: Optional[SharePointClient],
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.sessions = sessions
        self.azure = azure
        self.sharepoint = sharepoint
        self.hub = Hub()


def new(
    cfg: Config,
    store: Store,
    sessions: SessionManager,
    azure: Optional[AzureProvider],
    sharepoint: Optional[SharePointClient],
) -> Handlers:
    return Handlers(
        cfg=cfg,
        store=store,
        sessions=sessions,
        azure=azure,
        sharepoint=sharepoint,
    )
