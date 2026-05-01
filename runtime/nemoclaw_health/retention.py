from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nemoclaw_health.db import Database, prune_delegation_metadata, prune_raw_events_and_linked_dyn_rows


def cutoff_iso(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_raw_event_prune(db: Database, retention_days: int, *, dry_run: bool = False) -> dict[str, int | str]:
    """Prune raw_events older than retention_days and linked evt_dyn_* rows. Never touches goals/profile/summaries."""
    cut = cutoff_iso(retention_days)
    with db.transaction() as cur:
        stats = prune_raw_events_and_linked_dyn_rows(cur, cut, dry_run=dry_run)
    out: dict[str, int | str] = {"cutoff_occurred_at": cut}
    out.update({k: int(v) for k, v in stats.items()})
    return out


def run_delegation_metadata_prune(
    db: Database,
    retention_days: int | None,
    *,
    dry_run: bool = False,
) -> dict[str, int | str | bool]:
    """Optional pruning for delegation_events / agent_runs when retention_days is set."""
    if retention_days is None or retention_days <= 0:
        return {"skipped": True, "reason": "delegation_metadata_retention_days unset or non-positive"}
    cut = cutoff_iso(retention_days)
    with db.transaction() as cur:
        stats = prune_delegation_metadata(cur, cut, dry_run=dry_run)
    out: dict[str, int | str | bool] = {"cutoff_occurred_at": cut}
    out.update({k: int(v) for k, v in stats.items()})
    return out
