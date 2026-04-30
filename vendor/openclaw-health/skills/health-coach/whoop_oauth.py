from __future__ import annotations

import json
import os
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

SKILL_DIR = Path(__file__).resolve().parent
SKILLS_ROOT_DIR = SKILL_DIR.parent
TOKENS_DIR = SKILL_DIR / "tokens"
TOKEN_FILE = TOKENS_DIR / "whoop_token.json"
STATE_FILE = TOKENS_DIR / "whoop_oauth_state.json"

load_dotenv(dotenv_path=SKILL_DIR / ".env")
load_dotenv(dotenv_path=SKILLS_ROOT_DIR / ".env")
load_dotenv()

# WHOOP API v2 data lives under https://api.prod.whoop.com/developer/v2/... but OAuth is
# always on the separate /oauth/ host path (not under /developer/). See:
# https://developer.whoop.com/docs/developing/oauth
WHOOP_OAUTH_AUTH_DEFAULT = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_OAUTH_TOKEN_DEFAULT = "https://api.prod.whoop.com/oauth/oauth2/token"

WHOOP_AUTH_URL = (os.getenv("WHOOP_AUTH_URL") or WHOOP_OAUTH_AUTH_DEFAULT).rstrip("/")
WHOOP_TOKEN_URL = (os.getenv("WHOOP_TOKEN_URL") or WHOOP_OAUTH_TOKEN_DEFAULT).rstrip("/")


def _validate_oauth_endpoint_urls() -> None:
    """WHOOP REST v2 lives under /developer/...; OAuth is only on /oauth/... (never under /developer/)."""
    for var, url in (
        ("WHOOP_AUTH_URL", WHOOP_AUTH_URL),
        ("WHOOP_TOKEN_URL", WHOOP_TOKEN_URL),
    ):
        if "/developer" in url:
            raise RuntimeError(
                f"{var} is misconfigured ({url!r}). Use WHOOP's standalone OAuth URLs from "
                "https://developer.whoop.com/docs/developing/oauth — e.g. "
                f"{WHOOP_OAUTH_AUTH_DEFAULT} and {WHOOP_OAUTH_TOKEN_DEFAULT}."
            )
WHOOP_CLIENT_ID = os.getenv("WHOOP_CLIENT_ID", "")
WHOOP_CLIENT_SECRET = os.getenv("WHOOP_CLIENT_SECRET", "")
WHOOP_REDIRECT_URI = os.getenv("WHOOP_REDIRECT_URI", "http://localhost:8765/callback")

DEFAULT_SCOPES = [
    "offline",
    "read:workout",
    "read:sleep",
    "read:recovery",
    "read:cycles",
    "read:profile",
    "read:body_measurement",
]


def _require_client_config() -> None:
    if not WHOOP_CLIENT_ID or not WHOOP_CLIENT_SECRET:
        raise RuntimeError("WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET must be configured.")


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def build_auth_start_url(scopes: Optional[list[str]] = None) -> str:
    _validate_oauth_endpoint_urls()
    _require_client_config()
    scopes = scopes or DEFAULT_SCOPES
    state = secrets.token_urlsafe(16)
    _save_json(STATE_FILE, {"state": state, "created_at": int(time.time())})

    query = {
        "response_type": "code",
        "client_id": WHOOP_CLIENT_ID,
        "redirect_uri": WHOOP_REDIRECT_URI,
        "scope": " ".join(scopes),
        "state": state,
    }
    return f"{WHOOP_AUTH_URL}?{urllib.parse.urlencode(query)}"


def _normalize_token_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    expires_in = int(payload.get("expires_in", 0) or 0)
    now = int(time.time())
    payload["saved_at"] = now
    payload["expires_at"] = now + expires_in if expires_in else 0
    return payload


def exchange_code_for_token(code: str, state: Optional[str] = None) -> Dict[str, Any]:
    _validate_oauth_endpoint_urls()
    _require_client_config()
    saved_state = _load_json(STATE_FILE).get("state")
    if saved_state and state and saved_state != state:
        raise RuntimeError("OAuth state mismatch. Restart auth flow with auth_start.")

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": WHOOP_REDIRECT_URI,
        "client_id": WHOOP_CLIENT_ID,
        "client_secret": WHOOP_CLIENT_SECRET,
    }
    resp = requests.post(WHOOP_TOKEN_URL, data=body, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
    token = _normalize_token_payload(resp.json())
    _save_json(TOKEN_FILE, token)
    return token


def refresh_access_token(refresh_token: Optional[str] = None) -> Dict[str, Any]:
    _validate_oauth_endpoint_urls()
    _require_client_config()
    token = load_token()
    refresh_token = refresh_token or token.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("No refresh_token available. Re-run auth flow.")

    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": WHOOP_CLIENT_ID,
        "client_secret": WHOOP_CLIENT_SECRET,
    }
    resp = requests.post(WHOOP_TOKEN_URL, data=body, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Token refresh failed ({resp.status_code}): {resp.text[:300]}")
    refreshed = resp.json()
    if "refresh_token" not in refreshed:
        refreshed["refresh_token"] = refresh_token
    refreshed = _normalize_token_payload(refreshed)
    _save_json(TOKEN_FILE, refreshed)
    return refreshed


def load_token() -> Dict[str, Any]:
    return _load_json(TOKEN_FILE)


def auth_status() -> Dict[str, Any]:
    token = load_token()
    if not token:
        return {"authenticated": False, "reason": "token_missing"}

    now = int(time.time())
    expires_at = int(token.get("expires_at", 0) or 0)
    expired = bool(expires_at and now >= expires_at)
    return {
        "authenticated": True,
        "expired": expired,
        "expires_at": expires_at,
        "has_refresh_token": bool(token.get("refresh_token")),
        "scopes": token.get("scope", ""),
    }


def get_valid_access_token() -> str:
    token = load_token()
    if not token:
        raise RuntimeError("WHOOP token missing. Run auth_start then auth_finish.")

    now = int(time.time())
    expires_at = int(token.get("expires_at", 0) or 0)
    if expires_at and now >= (expires_at - 120):
        token = refresh_access_token(token.get("refresh_token"))
    access_token = token.get("access_token")
    if not access_token:
        raise RuntimeError("WHOOP access token missing. Re-run auth flow.")
    return access_token
