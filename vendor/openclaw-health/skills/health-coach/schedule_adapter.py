from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from models import ScheduleDay

SKILL_DIR = Path(__file__).resolve().parent
GCAL_SCRIPT = SKILL_DIR.parent / "google-calendar" / "google_calendar.py"
GTASKS_SCRIPT = SKILL_DIR.parent / "google-tasks-reminder" / "query_tasks.py"


def _run_json(command: List[str]) -> Tuple[List[Dict[str, Any]], str]:
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0 or not stdout:
        return [], stderr
    try:
        payload = json.loads(stdout)
        if isinstance(payload, list):
            return payload, stderr
        return [], stderr
    except json.JSONDecodeError:
        return [], stderr


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_busy_hours(events: List[Dict[str, Any]]) -> float:
    total_seconds = 0.0
    for ev in events:
        start = _parse_iso(str(ev.get("start", "")))
        end = _parse_iso(str(ev.get("end", "")))
        if start and end and end > start:
            total_seconds += (end - start).total_seconds()
    return total_seconds / 3600.0


def load_schedule_day(date_str: str) -> ScheduleDay:
    events, _ = _run_json([sys.executable, str(GCAL_SCRIPT), "list_events", "--account", "both", "--days", "1"])
    due_today, _ = _run_json([sys.executable, str(GTASKS_SCRIPT), "list", "--scope", "today", "--status", "active", "--limit", "50"])
    overdue, _ = _run_json([sys.executable, str(GTASKS_SCRIPT), "list", "--scope", "overdue", "--status", "active", "--limit", "50"])

    busy_hours = _event_busy_hours(events)
    event_count = len(events)
    due_today_count = len(due_today)
    overdue_count = len(overdue)

    # 0-100 scalar where high = more loaded day
    score = min(
        100.0,
        (event_count * 6.0) + (busy_hours * 8.0) + (due_today_count * 5.0) + (overdue_count * 7.0),
    )

    day = ScheduleDay(
        date=date_str,
        event_count=event_count,
        busy_hours=round(busy_hours, 2),
        due_today_count=due_today_count,
        overdue_count=overdue_count,
        schedule_load_score=round(score, 1),
    )

    # Dual-write allostatic score to SQLite
    try:
        from db_ingest import ingest_schedule_day
        ingest_schedule_day(day)
    except Exception:
        pass

    return day
