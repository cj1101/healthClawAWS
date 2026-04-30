"""Optional session cookie auth for /v1 API (single-user dashboard)."""

from __future__ import annotations

import hashlib
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from nemoclaw_health.settings import Settings


def session_secret_for(settings: Settings) -> str:
    if settings.session_secret:
        return settings.session_secret.strip()
    if settings.dashboard_password:
        return hashlib.sha256(
            ("nemoclaw.session." + settings.dashboard_password).encode("utf-8"),
        ).hexdigest()
    return "nemoclaw-dev-insecure-session"


def install_dashboard_auth(app: Any, settings: Settings) -> None:
    """SessionMiddleware is outermost so `request.session` exists before auth runs."""
    secret = session_secret_for(settings)

    class DashboardAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):  # type: ignore[override]
            if not settings.dashboard_password:
                return await call_next(request)
            p = request.url.path
            if p == "/healthz":
                return await call_next(request)
            if p.startswith("/v1/connectors/whoop/callback"):
                return await call_next(request)
            if p == "/v1/auth/login" and request.method == "POST":
                return await call_next(request)
            if p.startswith("/v1/"):
                sess = request.scope.get("session")
                if not isinstance(sess, dict) or not sess.get("authenticated"):
                    return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return await call_next(request)

    app.add_middleware(DashboardAuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="nemoclaw_session",
        same_site="lax",
        https_only=False,
        max_age=14 * 24 * 3600,
    )
