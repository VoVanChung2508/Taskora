"""Loads runtime configuration from environment variables.

Python port of the Go `config` package. Requires:
    pip install python-dotenv

Field names use snake_case; `AzureConfig` keeps the `.configured()`,
`.issuer()`, `.client_id`, `.client_secret`, `.redirect_url` shape expected
by `azure_provider.py`'s `AzureProvider.new(cfg)`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List

from dotenv import load_dotenv


@dataclass
class AzureConfig:
    """Azure AD (OIDC) credentials for SSO login."""

    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_url: str = ""

    def issuer(self) -> str:
        """Return the OIDC issuer URL for the configured tenant."""
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    def configured(self) -> bool:
        """Report whether the minimum Azure AD settings are present."""
        return bool(
            self.tenant_id and self.client_id and self.client_secret and self.redirect_url
        )


@dataclass
class SharePointConfig:
    """Microsoft Graph credentials and the root folder used for syncing
    project files. May reuse the Azure app registration.
    """

    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    site_url: str = ""       # e.g. "contoso.sharepoint.com:/sites/Projects"
    root_folder: str = ""    # e.g. "/Flowie"

    def configured(self) -> bool:
        """Report whether the minimum SharePoint settings are present."""
        return bool(self.tenant_id and self.client_id and self.client_secret and self.site_url)


@dataclass
class Config:
    """All runtime configuration for the API server."""

    env: str = "development"
    port: str = "8081"
    base_url: str = "http://localhost:8081"
    frontend_url: str = "http://localhost:3000"

    database_url: str = ""

    session_secret: str = ""
    session_ttl: timedelta = field(default_factory=lambda: timedelta(hours=24))

    system_admin_emails: List[str] = field(default_factory=list)

    azure: AzureConfig = field(default_factory=AzureConfig)
    sharepoint: SharePointConfig = field(default_factory=SharePointConfig)


def load() -> Config:
    """Read configuration from the environment, applying sane defaults."""
    # Load environment variables from a .env file. Missing .env is fine —
    # load_dotenv simply returns False and system env vars are still used.
    load_dotenv()

    ttl_hours = _get_int("SESSION_TTL_HOURS", 24)

    admin_emails_str = _get_env("SYSTEM_ADMIN_EMAILS", "")
    admin_emails: List[str] = []
    if admin_emails_str:
        for e in admin_emails_str.split(","):
            e = e.strip().strip("\"'")
            if e:
                admin_emails.append(e)

    cfg = Config(
        env=_get_env("APP_ENV", "development"),
        port=_get_env("APP_PORT", "8081"),
        base_url=_get_env("APP_BASE_URL", "http://localhost:8081"),
        frontend_url=_get_env("FRONTEND_URL", "http://localhost:3000"),
        database_url=_get_env("DATABASE_URL", ""),
        session_secret=_get_env("SESSION_SECRET", ""),
        session_ttl=timedelta(hours=ttl_hours),
        system_admin_emails=admin_emails,
        azure=AzureConfig(
            tenant_id=_get_env("AZURE_AD_TENANT_ID", ""),
            client_id=_get_env("AZURE_AD_CLIENT_ID", ""),
            client_secret=_get_env("AZURE_AD_CLIENT_SECRET", ""),
            redirect_url=_get_env("AZURE_AD_REDIRECT_URL", ""),
        ),
        sharepoint=SharePointConfig(
            tenant_id=_get_env("GRAPH_TENANT_ID", ""),
            client_id=_get_env("GRAPH_CLIENT_ID", ""),
            client_secret=_get_env("GRAPH_CLIENT_SECRET", ""),
            site_url=_get_env("SHAREPOINT_SITE_URL", ""),
            root_folder=_get_env("SHAREPOINT_ROOT_FOLDER", "/Flowie"),
        ),
    )

    if not cfg.database_url:
        raise ValueError("DATABASE_URL is required")
    if len(cfg.session_secret) < 32:
        raise ValueError("SESSION_SECRET must be at least 32 bytes")

    return cfg


def _get_env(key: str, fallback: str) -> str:
    v = os.environ.get(key)
    return v if v else fallback


def _get_int(key: str, fallback: int) -> int:
    v = os.environ.get(key)
    if v is not None:
        try:
            return int(v)
        except ValueError:
            pass
    return fallback
