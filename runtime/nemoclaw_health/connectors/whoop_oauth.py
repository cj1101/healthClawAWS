from __future__ import annotations

import secrets
import time
import urllib.parse
from typing import Any

import httpx

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


def require_whoop_config(settings: Settings) -> None:
    settings.validate_whoop_oauth_urls()
    if not settings.whoop_client_id or not settings.whoop_client_secret:
        raise WhoopConfigError("NEMOWLAW_WHOOP_CLIENT_ID and NEMOWLAW_WHOOP_CLIENT_SECRET are required.")
    if not settings.whoop_redirect_uri:
        raise WhoopConfigError("NEMOWLAW_WHOOP_REDIRECT_URI is required.")


def _normalize_token_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    expires_in = int(payload.get("expires_in", 0) or 0)
    now = int(time.time())
    out["expires_at"] = now + expires_in if expires_in else 0
    return out


def token_exchange_authorization_code(settings: Settings, *, code: str) -> dict[str, Any]:
    require_whoop_config(settings)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.whoop_redirect_uri or "",
        "client_id": settings.whoop_client_id,
        "client_secret": settings.whoop_client_secret,
    }
    with httpx.Client() as cli:
        r = cli.post(str(settings.whoop_token_url), data=data, timeout=30)
    if r.status_code >= 400:
        raise WhoopOAuthError(f"token_exchange_failed_{r.status_code}: {r.text[:400]}")
    return _normalize_token_payload(r.json())


def token_exchange_refresh(settings: Settings, *, refresh_token: str) -> dict[str, Any]:
    require_whoop_config(settings)
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


def build_authorization_url(database: Database, settings: Settings, scopes: list[str] | None = None) -> str:
    """Generate WHOOP authorize URL and persist oauth_pending CSRF state in connector_states."""
    require_whoop_config(settings)
    scopes_list = scopes or WHOOP_DEFAULT_SCOPES[:]
    # WHOOP OAuth docs: state must be eight characters when you generate it yourself.
    state = secrets.token_hex(4)
    with database.transaction() as cur:
        st = fetch_connector_state(cur, "whoop")
        st["oauth_pending"] = {"state": state, "created_at_unix": int(time.time())}
        put_connector_state(cur, "whoop", st)

    qs = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": settings.whoop_client_id,
            "redirect_uri": settings.whoop_redirect_uri or "",
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
    require_whoop_config(settings)

    with database.transaction() as cur:
        stored = fetch_connector_state(cur, "whoop").get("oauth_pending") or {}
        saved_state = stored.get("state")
    if saved_state != state:
        raise WhoopStateError("oauth state mismatch — restart authorize flow.")

    token = token_exchange_authorization_code(settings, code=code)
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
    require_whoop_config(settings)

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
