"""Azure AD (OIDC) authentication provider for Flowie.

Python port of the Go `auth.AzureProvider`. Requires:
    pip install requests "PyJWT[crypto]"
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import jwt
import requests
from jwt import PyJWKClient


@dataclass
class AzureClaims:
    """ID-token claims Flowie relies on."""

    oid: str = ""                    # stable Azure object id
    sub: str = ""                    # subject (fallback identity)
    email: str = ""                  # may be empty depending on tenant
    preferred_username: str = ""
    name: str = ""
    picture: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "AzureClaims":
        return cls(
            oid=data.get("oid", ""),
            sub=data.get("sub", ""),
            email=data.get("email", ""),
            preferred_username=data.get("preferred_username", ""),
            name=data.get("name", ""),
            picture=data.get("picture", ""),
        )

    def resolved_email(self) -> str:
        """Return the best available email for the user."""
        return self.email or self.preferred_username

    def resolved_oid(self) -> str:
        """Return a stable identifier, preferring oid over sub."""
        return self.oid or self.sub


class AzureProvider:
    """Wraps OIDC discovery + OAuth2 config for Azure AD login."""

    SCOPES = ["openid", "profile", "email", "User.Read"]

    def __init__(self, client_id: str, client_secret: str, redirect_url: str, issuer: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_url = redirect_url

        discovery = requests.get(
            f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=10
        )
        discovery.raise_for_status()
        meta = discovery.json()

        self.authorization_endpoint = meta["authorization_endpoint"]
        self.token_endpoint = meta["token_endpoint"]
        self.jwks_uri = meta["jwks_uri"]
        self.issuer = meta["issuer"]

        self._jwk_client = PyJWKClient(self.jwks_uri)

    @classmethod
    def new(cls, cfg) -> Optional["AzureProvider"]:
        """Build a provider from config.

        Mirrors the Go `NewAzureProvider`: performs OIDC discovery against
        the tenant and returns ``None`` (no error) when Azure is not
        configured, so the server can still boot for local development.
        Expects `cfg` to expose `.configured()`, `.client_id`,
        `.client_secret`, `.redirect_url`, and `.issuer()`.
        """
        if not cfg.configured():
            return None
        try:
            return cls(
                client_id=cfg.client_id,
                client_secret=cfg.client_secret,
                redirect_url=cfg.redirect_url,
                issuer=cfg.issuer(),
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"oidc discovery: {exc}") from exc

    def auth_code_url(self, state: str, nonce: str) -> str:
        """Build the Azure authorization URL for the given state + nonce."""
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_url,
            "scope": " ".join(self.SCOPES),
            "state": state,
            "nonce": nonce,
        }
        return f"{self.authorization_endpoint}?{urlencode(params)}"

    def exchange(self, code: str, nonce: str) -> AzureClaims:
        """Swap the authorization code for tokens and validate the ID token.

        Returns the verified claims, or raises RuntimeError on failure.
        """
        resp = requests.post(
            self.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_url,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"token exchange: {resp.status_code} {resp.text}")

        token = resp.json()
        raw_id_token = token.get("id_token")
        if not raw_id_token:
            raise RuntimeError("no id_token in token response")

        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(raw_id_token)
            payload = jwt.decode(
                raw_id_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=self.issuer,
            )
        except jwt.PyJWTError as exc:
            raise RuntimeError(f"verify id_token: {exc}") from exc

        if payload.get("nonce") != nonce:
            raise RuntimeError("nonce mismatch")

        claims = AzureClaims.from_dict(payload)

        # Fallback: if picture is missing in claims, fetch directly from Graph API
        if not claims.picture:
            claims.picture = _fetch_azure_photo(token.get("access_token", ""))

        return claims


def _fetch_azure_photo(access_token: str) -> str:
    """Call the Microsoft Graph API to download the user's profile photo."""
    if not access_token:
        return ""
    try:
        resp = requests.get(
            "https://graph.microsoft.com/v1.0/me/photo/$value",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
    except requests.RequestException:
        return ""

    if resp.status_code != 200:
        return ""

    mime = resp.headers.get("Content-Type") or "image/jpeg"
    encoded = base64.b64encode(resp.content).decode("ascii")
    return f"data:{mime};base64,{encoded}"