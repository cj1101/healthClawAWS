from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from whoop_oauth import get_valid_access_token

load_dotenv()

WHOOP_API_BASE = os.getenv("WHOOP_API_BASE", "https://api.prod.whoop.com/developer")
MAX_RETRIES = int(os.getenv("WHOOP_HTTP_MAX_RETRIES", "4"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class WhoopClient:
    def __init__(self) -> None:
        self.session = requests.Session()

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {get_valid_access_token()}"}

    def _request(self, method: str, endpoint: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{WHOOP_API_BASE.rstrip('/')}/{endpoint.lstrip('/')}"
        attempts = 0
        while True:
            attempts += 1
            resp = self.session.request(method, url, headers=self._headers(), params=params, timeout=30)
            if resp.status_code == 429 and attempts <= MAX_RETRIES:
                sleep_s = min(2 ** attempts, 20)
                time.sleep(sleep_s)
                continue
            if resp.status_code >= 500 and attempts <= MAX_RETRIES:
                time.sleep(min(2 ** attempts, 20))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"WHOOP request failed ({resp.status_code}) {endpoint}: {resp.text[:300]}")
            if not resp.text.strip():
                return {}
            return resp.json()

    def _paginate(self, endpoint: str, *, start: Optional[str] = None, end: Optional[str] = None, limit: int = 25) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        next_token: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"limit": limit}
            if start:
                params["start"] = start
            if end:
                params["end"] = end
            if next_token:
                params["nextToken"] = next_token

            payload = self._request("GET", endpoint, params=params)
            chunk = payload.get("records", []) if isinstance(payload, dict) else []
            records.extend(chunk)
            next_token = payload.get("next_token") if isinstance(payload, dict) else None
            if not next_token:
                break
        return records

    def get_workouts(self, *, start: Optional[str] = None, end: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._paginate("v2/activity/workout", start=start, end=end)

    def get_sleep(self, *, start: Optional[str] = None, end: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._paginate("v2/activity/sleep", start=start, end=end)

    def get_recovery(self, *, start: Optional[str] = None, end: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._paginate("v2/recovery", start=start, end=end)

    def get_cycles(self, *, start: Optional[str] = None, end: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._paginate("v2/cycle", start=start, end=end)

    def get_body_measurement(self) -> Dict[str, Any]:
        payload = self._request("GET", "v2/user/measurement/body")
        return payload if isinstance(payload, dict) else {}


def default_window(days: int = 7) -> Dict[str, str]:
    now = datetime.now(timezone.utc)
    return {"start": _iso(now - timedelta(days=days)), "end": _iso(now)}
