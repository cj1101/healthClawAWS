from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from nemoclaw_health.settings import Settings


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class WhoopAPIClient:
    """Paginated WHOOP developer v2 client (calls require a valid Bearer token)."""

    def __init__(
        self,
        settings: Settings,
        bearer_token_provider: Callable[[], str],
    ) -> None:
        self.settings = settings
        self._bearer_token_provider = bearer_token_provider

    def _headers(self) -> dict[str, str]:
        tok = self._bearer_token_provider()
        return {"Authorization": f"Bearer {tok}"}

    def _request(
        self,
        session: httpx.Client,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base = self.settings.whoop_api_base.rstrip("/")
        url = f"{base}/{endpoint.lstrip('/')}"
        retries = max(1, self.settings.whoop_http_max_retries)
        attempts = 0
        while True:
            attempts += 1
            resp = session.request(method, url, headers=self._headers(), params=params, timeout=45)
            if resp.status_code == 429 and attempts <= retries:
                time.sleep(min(2**attempts, 20))
                continue
            if resp.status_code >= 500 and attempts <= retries:
                time.sleep(min(2**attempts, 20))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"WHOOP {resp.status_code} {endpoint}: {resp.text[:400]}")
            if not resp.text.strip():
                return {}
            return resp.json()

    def paginate_all(
        self,
        endpoint: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        next_token: str | None = None
        with httpx.Client() as session:
            while True:
                params: dict[str, Any] = {"limit": limit}
                if start:
                    params["start"] = start
                if end:
                    params["end"] = end
                if next_token:
                    params["nextToken"] = next_token
                payload = self._request(session, "GET", endpoint, params=params)
                chunk = payload.get("records", []) if isinstance(payload, dict) else []
                records.extend(chunk)
                next_token = payload.get("next_token") if isinstance(payload, dict) else None
                if not next_token:
                    break
        return records

    def get_workouts(self, *, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        return self.paginate_all("v2/activity/workout", start=start, end=end)

    def get_sleep(self, *, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        return self.paginate_all("v2/activity/sleep", start=start, end=end)

    def get_recovery(self, *, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        return self.paginate_all("v2/recovery", start=start, end=end)

    def get_cycles(self, *, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        return self.paginate_all("v2/cycle", start=start, end=end)

    def get_body_measurement(self) -> dict[str, Any]:
        with httpx.Client() as session:
            payload = self._request(session, "GET", "v2/user/measurement/body")
        return payload if isinstance(payload, dict) else {}


def default_window_iso(days: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=max(1, days))
    return _utc_iso(start_dt.replace(microsecond=0)), _utc_iso(now.replace(microsecond=0))
