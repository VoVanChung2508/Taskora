"""
Module server: khởi tạo cấu hình, middleware và các route handler
thành một ứng dụng FastAPI (tương đương bản Go dùng chi router).
"""

from __future__ import annotations

import time
from typing import Callable

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from ..auth.session import SessionManager
from ..config.config import Config
from ..handlers.handlers import Handlers


# ──────────────────────────────────────────────────────────────────────────
# Middleware phụ trợ
# ──────────────────────────────────────────────────────────────────────────

def is_streaming_path(path: str) -> bool:
    """Kiểm tra xem request có phải là một stream dài hạn (SSE) hay không —
    loại request này không được áp dụng timeout toàn cục."""
    return path.endswith("/events")


class TimeoutExceptMiddleware(BaseHTTPMiddleware):
    """Áp dụng timeout cho mọi request, trừ những request khớp với `skip`.

    Các endpoint streaming phải tồn tại lâu hơn timeout thông thường, nếu
    không kết nối SSE sẽ bị ngắt mỗi 30 giây.
    """

    def __init__(
        self,
        app: ASGIApp,
        timeout_seconds: float,
        skip: Callable[[Request], bool],
    ) -> None:
        super().__init__(app)
        self.timeout_seconds = timeout_seconds
        self.skip = skip

    async def dispatch(self, request: Request, call_next):
        if self.skip(request):
            return await call_next(request)

        import asyncio

        try:
            return await asyncio.wait_for(
                call_next(request), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            return Response(status_code=504, content="request timeout")


class SecureHeadersMiddleware(BaseHTTPMiddleware):
    """Thêm các header bảo mật cơ bản khi không ở môi trường development."""

    def __init__(self, app: ASGIApp, enabled: bool) -> None:
        super().__init__(app)
        self.enabled = enabled

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if self.enabled:
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response


class RateLimitByIPMiddleware(BaseHTTPMiddleware):
    """Giới hạn 300 request/phút theo địa chỉ IP (bộ nhớ trong tiến trình).

    Trong sản xuất nên thay bằng Redis (ví dụ dùng slowapi + Redis backend)
    để hoạt động đúng khi chạy nhiều worker/instance.
    """

    def __init__(self, app: ASGIApp, limit: int = 300, window_seconds: int = 60) -> None:
        super().__init__(app)
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = {}

    async def dispatch(self, request: Request, call_next):
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window_start = now - self.window_seconds

        hits = [t for t in self._hits.get(ip, []) if t > window_start]
        if len(hits) >= self.limit:
            return Response(status_code=429, content="too many requests")

        hits.append(now)
        self._hits[ip] = hits
        return await call_next(request)


# ──────────────────────────────────────────────────────────────────────────
# Khởi tạo ứng dụng / router
# ──────────────────────────────────────────────────────────────────────────

def new_app(cfg: Config, h: Handlers, sm: SessionManager) -> FastAPI:
    """Xây dựng ứng dụng FastAPI với toàn bộ route và middleware."""

    app = FastAPI()

    # ── Middleware (thứ tự tương ứng bản Go: outer → inner) ──
    is_dev = cfg.env == "development"

    app.add_middleware(SecureHeadersMiddleware, enabled=not is_dev)
    app.add_middleware(RateLimitByIPMiddleware, limit=300, window_seconds=60)
    app.add_middleware(
        TimeoutExceptMiddleware,
        timeout_seconds=30,
        skip=lambda request: is_streaming_path(request.url.path),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[cfg.frontend_url],
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Authorization", "Content-Type"],
        allow_credentials=True,
        max_age=300,
    )

    app.add_api_route("/healthz", h.health, methods=["GET"])

    # API công khai cho tích hợp bên thứ ba. Xác thực bằng workspace API key
    # thay vì session cookie, nên nằm ngoài /api/v1.
    # Webhook SCM đến (GitHub/GitLab). Machine-to-machine: xác thực bằng một
    # secret riêng cho từng project trong query string, không dùng session.
    app.add_api_route(
        "/api/scm/v1/projects/{project_id}/webhook",
        h.scm_webhook,
        methods=["POST"],
    )

    # ── /api/public/v1 (xác thực bằng API key) ──
    public_v1 = APIRouter(
        prefix="/api/public/v1",
        dependencies=[Depends(h.require_api_key)],
    )
    public_v1.add_api_route("/projects", h.api_projects, methods=["GET"])
    public_v1.add_api_route("/projects/{project_id}/tasks", h.api_tasks, methods=["GET"])
    public_v1.add_api_route(
        "/projects/{project_id}/tasks", h.api_create_task, methods=["POST"]
    )
    app.include_router(public_v1)

    # ── /api/v1 ──
    api_v1 = APIRouter(prefix="/api/v1")

    # Route auth công khai
    auth_router = APIRouter(prefix="/auth")
    auth_router.add_api_route("/azure/login", h.azure_login, methods=["GET"])
    auth_router.add_api_route("/azure/callback", h.azure_callback, methods=["GET"])
    auth_router.add_api_route("/logout", h.logout, methods=["POST"])
    # Hoàn tất thử thách MFA; tự đọc token đang chờ vì RequireAuth từ chối
    # các session mới xác thực một nửa.
    auth_router.add_api_route("/2fa/verify", h.verify_two_factor, methods=["POST"])
    # Chỉ dùng cho dev: được chặn bên trong handler (trả 404 ngoài môi
    # trường development).
    auth_router.add_api_route("/dev-login", h.dev_login, methods=["GET"])
    api_v1.include_router(auth_router)

    # ── Route yêu cầu xác thực ──
    authed = APIRouter(dependencies=[Depends(sm.require_auth)])

    authed.add_api_route("/me", h.me, methods=["GET"])
    # GDPR: quyền di chuyển dữ liệu + xoá dữ liệu
    authed.add_api_route("/me/export", h.export_my_data, methods=["GET"])
    authed.add_api_route("/me/delete", h.delete_my_data, methods=["POST"])
    authed.add_api_route("/me/2fa", h.two_factor_status, methods=["GET"])
    authed.add_api_route("/me/2fa/start", h.start_two_factor, methods=["POST"])
    authed.add_api_route("/me/2fa/enable", h.enable_two_factor, methods=["POST"])
    authed.add_api_route("/me/2fa/disable", h.disable_two_factor, methods=["POST"])
    authed.add_api_route("/me/session/refresh", h.refresh_session, methods=["POST"])
    authed.add_api_route("/me/sessions", h.list_sessions, methods=["GET"])
    authed.add_api_route(
        "/me/sessions/{session_id}", h.revoke_session, methods=["DELETE"]
    )
    authed.add_api_route("/invites/accept", h.accept_invite, methods=["POST"])
    authed.add_api_route("/permissions", h.list_permissions, methods=["GET"])
    authed.add_api_route("/me/timer", h.get_active_timer, methods=["GET"])
    authed.add_api_route("/me/timer/stop", h.stop_timer, methods=["POST"])
    authed.add_api_route("/me/timer", h.cancel_timer, methods=["DELETE"])
    authed.add_api_route("/me/timesheet", h.my_timesheet, methods=["GET"])
    authed.add_api_route("/me/calendar", h.my_calendar, methods=["GET"])
    authed.add_api_route("/me/dashboard", h.dashboard, methods=["GET"])
    authed.add_api_route("/me/notifications", h.list_notifications, methods=["GET"])
    authed.add_api_route(
        "/me/notifications/read", h.mark_all_notifications_read, methods=["POST"]
    )
    authed.add_api_route(
        "/notifications/{notif_id}/read", h.mark_notification_read, methods=["PATCH"]
    )
    authed.add_api_route(
        "/me/timesheet/submit", h.submit_timesheet, methods=["POST"]
    )
    authed.add_api_route(
        "/worklogs/{worklog_id}", h.set_worklog_state, methods=["PATCH"]
    )
    authed.add_api_route("/dev-make-admin", h.dev_make_admin, methods=["POST"])

    # -- /admin --
    admin = APIRouter(prefix="/admin")
    admin.add_api_route("/users", h.admin_list_users, methods=["GET"])
    admin.add_api_route("/users/sync-azure", h.admin_sync_azure_users, methods=["POST"])
    admin.add_api_route("/users/{user_id}", h.admin_toggle_user, methods=["PUT"])
    admin.add_api_route("/audit-log", h.admin_audit_log, methods=["GET"])
    admin.add_api_route("/workspaces", h.admin_list_workspaces, methods=["GET"])
    admin.add_api_route("/workspaces", h.admin_create_workspace, methods=["POST"])
    admin.add_api_route(
        "/workspaces/{workspace_id}", h.admin_delete_workspace, methods=["DELETE"]
    )
    authed.include_router(admin)

    # -- /workspaces --
    workspaces = APIRouter(prefix="/workspaces")
    workspaces.add_api_route("/", h.list_workspaces, methods=["GET"])
    workspaces.add_api_route("/", h.create_workspace, methods=["POST"])

    ws_detail = APIRouter(prefix="/{workspace_id}")
    ws_detail.add_api_route("/", h.get_workspace, methods=["GET"])
    ws_detail.add_api_route("/projects", h.list_projects, methods=["GET"])
    ws_detail.add_api_route("/projects", h.create_project, methods=["POST"])
    ws_detail.add_api_route("/overview", h.workspace_overview, methods=["GET"])
    ws_detail.add_api_route("/invites", h.list_invites, methods=["GET"])
    ws_detail.add_api_route("/invites", h.create_invite, methods=["POST"])
    ws_detail.add_api_route(
        "/invites/{invite_id}", h.revoke_invite, methods=["DELETE"]
    )
    ws_detail.add_api_route("/audit-log", h.list_audit_log, methods=["GET"])
    ws_detail.add_api_route("/reports", h.list_reports, methods=["GET"])
    ws_detail.add_api_route("/reports", h.create_report, methods=["POST"])
    ws_detail.add_api_route(
        "/reports/{report_id}", h.delete_report, methods=["DELETE"]
    )
    ws_detail.add_api_route(
        "/reports/{report_id}/run", h.run_report_now, methods=["POST"]
    )
    ws_detail.add_api_route("/api-keys", h.list_api_keys, methods=["GET"])
    ws_detail.add_api_route("/api-keys", h.create_api_key, methods=["POST"])
    ws_detail.add_api_route(
        "/api-keys/{key_id}", h.revoke_api_key, methods=["DELETE"]
    )
    ws_detail.add_api_route("/dashboards", h.list_dashboards, methods=["GET"])
    ws_detail.add_api_route("/dashboards", h.create_dashboard, methods=["POST"])
    ws_detail.add_api_route("/members", h.list_members, methods=["GET"])
    ws_detail.add_api_route("/members", h.add_member, methods=["POST"])
    ws_detail.add_api_route(
        "/members/{user_id}", h.update_member, methods=["PATCH"]
    )
    ws_detail.add_api_route(
        "/members/{user_id}/rate", h.set_member_rate, methods=["PUT"]
    )
    ws_detail.add_api_route(
        "/members/{user_id}/custom-role", h.assign_custom_role, methods=["PUT"]
    )

    ws_detail.add_api_route("/roles", h.list_custom_roles, methods=["GET"])
    ws_detail.add_api_route("/roles", h.create_custom_role, methods=["POST"])
    ws_detail.add_api_route("/roles/{role_id}", h.update_custom_role, methods=["PUT"])
    ws_detail.add_api_route(
        "/roles/{role_id}", h.delete_custom_role, methods=["DELETE"]
    )

    ws_detail.add_api_route("/teams", h.list_teams, methods=["GET"])
    ws_detail.add_api_route("/teams", h.create_team, methods=["POST"])
    ws_detail.add_api_route("/teams/{team_id}", h.delete_team, methods=["DELETE"])
    ws_detail.add_api_route(
        "/teams/{team_id}/members", h.set_team_member, methods=["POST"]
    )

    workspaces.include_router(ws_detail)
    authed.include_router(workspaces)

    # -- /projects/{project_id} --
    project = APIRouter(prefix="/projects/{project_id}")
    project.add_api_route("/", h.get_project, methods=["GET"])
    project.add_api_route("/tasks", h.list_tasks, methods=["GET"])
    project.add_api_route("/tasks", h.create_task, methods=["POST"])
    project.add_api_route("/timesheet", h.project_timesheet, methods=["GET"])
    project.add_api_route("/labels", h.list_labels, methods=["GET"])
    project.add_api_route("/labels", h.create_label, methods=["POST"])
    project.add_api_route("/sprints", h.list_sprints, methods=["GET"])
    project.add_api_route("/sprints", h.create_sprint, methods=["POST"])
    project.add_api_route("/stats", h.project_stats, methods=["GET"])
    project.add_api_route("/overview", h.project_overview, methods=["GET"])
    project.add_api_route("/velocity", h.project_velocity, methods=["GET"])
    project.add_api_route("/critical-path", h.project_critical_path, methods=["GET"])
    project.add_api_route("/members", h.project_members, methods=["GET"])
    project.add_api_route("/automations", h.list_automations, methods=["GET"])
    project.add_api_route("/automations", h.create_automation, methods=["POST"])
    project.add_api_route(
        "/automations/v2", h.create_automation_v2, methods=["POST"]
    )
    project.add_api_route("/events", h.project_events, methods=["GET"])
    project.add_api_route("/integrations", h.list_integrations, methods=["GET"])
    project.add_api_route("/integrations", h.create_integration, methods=["POST"])
    project.add_api_route(
        "/integrations/{integration_id}", h.delete_integration, methods=["DELETE"]
    )
    project.add_api_route("/webhooks", h.list_webhooks, methods=["GET"])
    project.add_api_route("/webhooks", h.create_webhook, methods=["POST"])
    project.add_api_route(
        "/webhooks/{webhook_id}", h.delete_webhook, methods=["DELETE"]
    )
    project.add_api_route("/files", h.browse_project_files, methods=["GET"])
    project.add_api_route("/views", h.list_saved_views, methods=["GET"])
    project.add_api_route("/views", h.create_saved_view, methods=["POST"])
    project.add_api_route("/views/{view_id}", h.delete_saved_view, methods=["DELETE"])
    project.add_api_route("/statuses", h.list_statuses, methods=["GET"])
    project.add_api_route("/statuses", h.create_status, methods=["POST"])
    project.add_api_route(
        "/statuses/{status_id}", h.update_status, methods=["PUT"]
    )
    project.add_api_route(
        "/statuses/{status_id}", h.delete_status, methods=["DELETE"]
    )
    project.add_api_route("/channels", h.list_channels, methods=["GET"])
    project.add_api_route("/channels", h.create_channel, methods=["POST"])
    project.add_api_route("/custom-fields", h.list_custom_fields, methods=["GET"])
    project.add_api_route("/custom-fields", h.create_custom_field, methods=["POST"])
    project.add_api_route(
        "/custom-fields/{field_id}", h.delete_custom_field, methods=["DELETE"]
    )
    authed.include_router(project)

    authed.add_api_route(
        "/automations/{rule_id}", h.delete_automation, methods=["DELETE"]
    )

    # -- /dashboards/{dashboard_id} --
    dashboard_router = APIRouter(prefix="/dashboards/{dashboard_id}")
    dashboard_router.add_api_route("/", h.delete_dashboard, methods=["DELETE"])
    dashboard_router.add_api_route("/widgets", h.add_widget, methods=["POST"])
    dashboard_router.add_api_route(
        "/widgets/{widget_id}", h.delete_widget, methods=["DELETE"]
    )
    authed.include_router(dashboard_router)

    # -- /channels/{channel_id} --
    channel_router = APIRouter(prefix="/channels/{channel_id}")
    channel_router.add_api_route("/", h.delete_channel, methods=["DELETE"])
    channel_router.add_api_route("/messages", h.list_messages, methods=["GET"])
    channel_router.add_api_route("/messages", h.post_message, methods=["POST"])
    channel_router.add_api_route("/read", h.mark_channel_read, methods=["POST"])
    authed.include_router(channel_router)

    authed.add_api_route("/sprints/{sprint_id}", h.update_sprint, methods=["PATCH"])
    authed.add_api_route(
        "/sprints/{sprint_id}/burndown", h.sprint_burndown, methods=["GET"]
    )
    authed.add_api_route(
        "/sprints/{sprint_id}/capacity", h.sprint_capacity, methods=["GET"]
    )

    # -- /tasks/{task_id} --
    task_router = APIRouter(prefix="/tasks/{task_id}")
    task_router.add_api_route("/", h.get_task, methods=["GET"])
    task_router.add_api_route("/", h.update_task, methods=["PATCH"])
    task_router.add_api_route("/", h.delete_task, methods=["DELETE"])
    task_router.add_api_route("/status", h.update_task_status, methods=["PATCH"])
    task_router.add_api_route("/sprint", h.set_task_sprint, methods=["PATCH"])
    task_router.add_api_route("/comments", h.add_comment, methods=["POST"])
    task_router.add_api_route("/checklist", h.add_checklist_item, methods=["POST"])
    task_router.add_api_route(
        "/checklist/{item_id}", h.toggle_checklist_item, methods=["PATCH"]
    )
    task_router.add_api_route("/labels", h.set_task_label, methods=["POST"])
    task_router.add_api_route("/worklogs", h.list_task_worklogs, methods=["GET"])
    task_router.add_api_route("/worklogs", h.log_work, methods=["POST"])
    task_router.add_api_route("/timer/start", h.start_timer, methods=["POST"])
    task_router.add_api_route("/attachments", h.list_attachments, methods=["GET"])
    task_router.add_api_route("/attachments", h.upload_attachment, methods=["POST"])
    task_router.add_api_route(
        "/attachments/{attachment_id}", h.delete_attachment, methods=["DELETE"]
    )
    task_router.add_api_route(
        "/dependencies", h.list_task_dependencies, methods=["GET"]
    )
    task_router.add_api_route(
        "/dependencies", h.add_task_dependency, methods=["POST"]
    )
    task_router.add_api_route(
        "/dependencies/{dep_id}", h.remove_task_dependency, methods=["DELETE"]
    )
    task_router.add_api_route(
        "/custom-fields", h.set_task_custom_field, methods=["PUT"]
    )
    authed.include_router(task_router)

    api_v1.include_router(authed)
    app.include_router(api_v1)

    return app