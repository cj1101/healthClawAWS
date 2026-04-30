from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nemoclaw_health.db import Database, prune_raw_events


def cutoff_iso(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_raw_event_prune(db: Database, retention_days: int, *, dry_run: bool = False) -> dict[str, int | str]:
    """Prune raw_events older than retention_days. Never touches goals/profile/summaries."""
    cut = cutoff_iso(retention_days)
    with db.transaction() as cur:
        deleted_or_count = prune_raw_events(cur, cut, dry_run=dry_run)
    return {"cutoff_occurred_at": cut, "raw_events_affected": int(deleted_or_count)}
