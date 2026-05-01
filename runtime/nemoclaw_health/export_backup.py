"""Local backup helpers: JSONL snapshot of raw_events + SQLite path reference."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nemoclaw_health.db import Database


def export_raw_events_jsonl(db: Database, dest_path: Path) -> dict[str, Any]:
    """
    Write one JSON object per line for each raw_events row (column names as keys).
    Returns counts and resolved paths for UI/scripts.
    """
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with db.transaction() as cur:
        rows = cur.execute(
            """
            SELECT id, occurred_at, source, event_type, domain_slug,
                   payload_json, confidence, provenance_json
            FROM raw_events
            ORDER BY datetime(occurred_at) ASC
            """
        ).fetchall()
    lines: list[str] = []
    for r in rows:
        obj = {
            "id": r["id"],
            "occurred_at": r["occurred_at"],
            "source": r["source"],
            "event_type": r["event_type"],
            "domain_slug": r["domain_slug"],
            "payload_json": r["payload_json"],
            "confidence": r["confidence"],
            "provenance_json": r["provenance_json"],
        }
        lines.append(json.dumps(obj, ensure_ascii=False))
        n += 1
    dest_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return {
        "raw_events_written": n,
        "jsonl_path": str(dest_path.resolve()),
        "sqlite_path": str(Path(db.sqlite_path).resolve()),
    }
