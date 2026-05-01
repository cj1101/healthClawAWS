from __future__ import annotations

import secrets
import time
import urllib.parse
from typing import Any

import httpx
from starlette.requests import Request

from nemoclaw_health.db import (
    Database,
    fetch_connector_state,
    put_connector_state,
)
from nemoclaw_health.settings import Settings

WHOOP_DEFAULT_SCOPES = [
    "offline",
    "read:workout",
    "read:sleep",
    "read:recovery",
    "read:cycles",
    "read:profile",
    "read:body_measurement",
]


class WhoopConfigError(ValueError):
    pass


class WhoopOAuthError(RuntimeError):
    pass


class WhoopStateError(WhoopOAuthError):
    pass


def callback_url_from_request(request: Request) -> str:
    """Public redirect_uri for WHOOP OAuth, from the browser's view of this service.

    Uses X-Forwarded-Proto / X-Forwarded-Host when set (typical behind TLS reverse proxy).
    """
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or "").strip()
    if forwarded_proto and forwarded_host:
        scheme = forwarded_proto.split(",")[0].strip()
        host = forwarded_host.split(",")[0].strip()
        base = f"{scheme}://{host}".rstrip("/")
    else:
        base = str(request.base_url).rstrip("/")
    return f"{base}/v1/connectors/whoop/callback"


def resolve_whoop_redirect_uri(settings: Settings, request: Request) -> tuple[str, str]:
    """Pick redirect_uri for browser OAuth: env override, else request-derived.

    Returns ``(redirect_uri, provenance)`` where provenance is one of
    ``\"env\"``, ``\"derived\"``, or ``\"ignored_placeholder_env\"`` when
    ``NEMOWLAW_WHOOP_REDIRECT_URI`` is still the copy-paste placeholder
    from ``ec2.env.example`` (hostname ``your_domain``), which WHOOP will
    reject — we fall back to the same origin as the Authorize request.
    """
    raw = (settings.whoop_redirect_uri or "").strip()
    derived = callback_url_from_request(request)
    if not raw:
        return derived, "derived"
    try:
        host = (urllib.parse.urlparse(raw).hostname or "").lower()
    except ValueError:
        return derived, "derived"
    if host == "your_domain":
        return derived, "ignored_placeholder_env"
    return raw, "env"


def whoop_authorize_dashboard_hint(redirect_uri: str) -> str:
    """User-facing reminder: WHOOP matches redirect_uri exactly to dashboard entries."""
    exact = (
        "Copy redirect_uri below into your WHOOP app’s Redirect URLs "
        "(developer-dashboard.whoop.com) — the value must match exactly, including scheme and path."
    )
    try:
        p = urllib.parse.urlparse((redirect_uri or "").strip())
    except ValueError:
        return exact
    host = (p.hostname or "").lower()
    if p.scheme == "http" and host in ("localhost", "127.0.0.1"):
        return (
            exact
            + " WHOOP’s OAuth examples use https:// or whoop://; if the dashboard refuses this http:// URL, "
            "use HTTPS in front of this app (or a tunnel), set NEMOWLAW_WHOOP_REDIRECT_URI to that https callback, "
            "register the same URL at WHOOP, then Open authorize URL again."
        )
    return exact


def require_whoop_oauth_client(settings: Settings) -> None:
    settings.validate_whoop_oauth_urls()
    if not settings.whoop_client_id or not settings.whoop_client_secret:
        raise WhoopConfigError(
            "WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET are required "
            "(or legacy NEMOWLAW_WHOOP_CLIENT_ID / NEMOWLAW_WHOOP_CLIENT_SECRET)."
        )


def require_whoop_config(settings: Settings) -> None:
    """Require client credentials and a static redirect URI (e.g. for callers without Request)."""
    require_whoop_oauth_client(settings)
    if not (settings.whoop_redirect_uri or "").strip():
        raise WhoopConfigError("NEMOWLAW_WHOOP_REDIRECT_URI is required.")


def _normalize_token_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    expires_in = int(payload.get("expires_in", 0) or 0)
    now = int(time.time())
    out["expires_at"] = now + expires_in if expires_in else 0
    return out


def token_exchange_authorization_code(
    settings: Settings,
    *,
    code: str,
    redirect_uri: str,
) -> dict[str, Any]:
    require_whoop_oauth_client(settings)
    rd = (redirect_uri or "").strip()
    if not rd:
        raise WhoopOAuthError("missing redirect_uri for authorization_code token exchange.")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": rd,
        "client_id": settings.whoop_client_id,
        "client_secret": settings.whoop_client_secret,
    }
    with httpx.Client() as cli:
        r = cli.post(str(settings.whoop_token_url), data=data, timeout=30)
    if r.status_code >= 400:
        raise WhoopOAuthError(f"token_exchange_failed_{r.status_code}: {r.text[:400]}")
    return _normalize_token_payload(r.json())


def token_exchange_refresh(settings: Settings, *, refresh_token: str) -> dict[str, Any]:
    require_whoop_oauth_client(settings)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.whoop_client_id,
        "client_secret": settings.whoop_client_secret,
        "scope": "offline",
    }
    with httpx.Client() as cli:
        r = cli.post(str(settings.whoop_token_url), data=data, timeout=30)
    if r.status_code >= 400:
        raise WhoopOAuthError(f"token_refresh_failed_{r.status_code}: {r.text[:400]}")
    refreshed = r.json()
    if "refresh_token" not in refreshed:
        refreshed["refresh_token"] = refresh_token
    return _normalize_token_payload(refreshed)


def build_authorization_url(
    database: Database,
    settings: Settings,
    *,
    redirect_uri: str,
    scopes: list[str] | None = None,
) -> str:
    """Generate WHOOP authorize URL and persist oauth_pending (state + redirect_uri) in connector_states."""
    require_whoop_oauth_client(settings)
    rd = (redirect_uri or "").strip()
    if not rd:
        raise WhoopConfigError(
            "WHOOP redirect_uri is empty. Set NEMOWLAW_WHOOP_REDIRECT_URI to the exact URL "
            "registered at https://developer-dashboard.whoop.com/apps (e.g. "
            "https://your-host/v1/connectors/whoop/callback), or omit it and open Authorize "
            "from the same origin you use in the browser so it can be derived automatically."
        )
    scopes_list = scopes or WHOOP_DEFAULT_SCOPES[:]
    # WHOOP OAuth docs: state must be eight characters when you generate it yourself.
    state = secrets.token_hex(4)
    with database.transaction() as cur:
        st = fetch_connector_state(cur, "whoop")
        st["oauth_pending"] = {
            "state": state,
            "created_at_unix": int(time.time()),
            "redirect_uri": rd,
        }
        put_connector_state(cur, "whoop", st)

    qs = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": settings.whoop_client_id,
            "redirect_uri": rd,
            "scope": " ".join(scopes_list),
            "state": state,
        },
    )
    return f"{str(settings.whoop_auth_url).rstrip('/')}?{qs}"


def exchange_callback_code(
    database: Database,
    settings: Settings,
    *,
    code: str | None,
    state: str | None,
) -> dict[str, Any]:
    if not code:
        raise WhoopOAuthError("missing OAuth code.")
    require_whoop_oauth_client(settings)

    with database.transaction() as cur:
        stored = fetch_connector_state(cur, "whoop").get("oauth_pending") or {}
        saved_state = stored.get("state")
        pending_redirect = (stored.get("redirect_uri") or "").strip()
    if saved_state != state:
        raise WhoopStateError("oauth state mismatch — restart authorize flow.")

    redirect_uri = pending_redirect or (settings.whoop_redirect_uri or "").strip()
    if not redirect_uri:
        raise WhoopConfigError(
            "OAuth callback missing redirect_uri — start again from GET /v1/connectors/whoop/authorize-url."
        )

    token = token_exchange_authorization_code(settings, code=code, redirect_uri=redirect_uri)
    persist_oauth(database, token)

    with database.transaction() as cur:
        full_state = fetch_connector_state(cur, "whoop")
    return {"ok": True, **oauth_status_from_state(full_state)}


def oauth_status_from_state(whoop_json: dict[str, Any]) -> dict[str, Any]:
    oauth = whoop_json.get("oauth") or {}
    sync = whoop_json.get("sync") or {}
    if not oauth:
        return {
            "connected": False,
            "last_sync_success_at": sync.get("last_success_at"),
            "last_sync_attempt_at": sync.get("last_attempt_at"),
            "last_sync_ok": sync.get("last_sync_ok"),
            "last_error": sync.get("last_error"),
        }
    expires_at = int(oauth.get("expires_at") or 0)
    now = int(time.time())
    return {
        "connected": True,
        "expires_at": expires_at,
        "expired": bool(expires_at and now >= expires_at),
        "has_refresh_token": bool(oauth.get("refresh_token")),
        "scopes": oauth.get("scope"),
        "last_sync_success_at": sync.get("last_success_at"),
        "last_sync_attempt_at": sync.get("last_attempt_at"),
        "last_sync_ok": sync.get("last_sync_ok"),
        "last_error": sync.get("last_error"),
    }


def disconnect_whoop(database: Database) -> dict[str, Any]:
    """Clear WHOOP OAuth tokens; preserve last sync timestamps but drop errors."""
    with database.transaction() as cur:
        st = fetch_connector_state(cur, "whoop")
        st.pop("oauth", None)
        st.pop("oauth_pending", None)
        sync = st.get("sync")
        if isinstance(sync, dict):
            sync.pop("last_error", None)
            sync.pop("last_sync_ok", None)
            st["sync"] = sync
        put_connector_state(cur, "whoop", st)
    return {"ok": True}


def persist_oauth(database: Database, oauth_payload: dict[str, Any]) -> dict[str, Any]:
    oauth_payload = _normalize_token_payload(oauth_payload)
    oauth_storage = {
        "access_token": oauth_payload.get("access_token"),
        "refresh_token": oauth_payload.get("refresh_token"),
        "expires_at": oauth_payload.get("expires_at"),
        "scope": oauth_payload.get("scope"),
        "token_type": oauth_payload.get("token_type"),
    }
    # Never echo secrets to logs from callers — store only internally.
    with database.transaction() as cur:
        st = fetch_connector_state(cur, "whoop")
        st["oauth"] = oauth_storage
        st.pop("oauth_pending", None)
        put_connector_state(cur, "whoop", st)
        return fetch_connector_state(cur, "whoop")


def ensure_whoop_access_token(database: Database, settings: Settings) -> str:
    """Return valid access_token, refreshing in-place when nearing expiry."""
    require_whoop_oauth_client(settings)

    def _expires_in_window(oauth_blob: dict[str, Any]) -> bool:
        exp = int(oauth_blob.get("expires_at") or 0)
        now_t = int(time.time())
        return bool(exp and now_t >= (exp - 120))

    with database.transaction() as cur:
        st = fetch_connector_state(cur, "whoop")
        oauth = st.get("oauth") or {}

    access = oauth.get("access_token")
    refresh_tok = oauth.get("refresh_token")

    if access and oauth and not _expires_in_window(oauth):
        return str(access)

    if not refresh_tok:
        raise WhoopOAuthError("WHOOP session missing refresh_token — reconnect via authorize flow.")

    new_token = token_exchange_refresh(settings, refresh_token=str(refresh_tok))

    merged_oauth = {
        "access_token": new_token.get("access_token"),
        "refresh_token": new_token.get("refresh_token"),
        "expires_at": new_token.get("expires_at"),
        "scope": new_token.get("scope", oauth.get("scope")),
        "token_type": new_token.get("token_type"),
    }

    with database.transaction() as cur:
        st = fetch_connector_state(cur, "whoop")
        st["oauth"] = merged_oauth
        put_connector_state(cur, "whoop", st)

    tok = merged_oauth.get("access_token")
    if not tok:
        raise WhoopOAuthError("WHOOP refresh did not yield access_token.")
    return str(tok)
