"""Bridge to vendored OpenClaw ``health_db`` — used only from DataEntryService."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_HEALTH_COACH_ROOT = Path(__file__).resolve().parent.parent.parent / "vendor/openclaw-health/skills/health-coach"


def configure_health_coach_db(path: Path) -> None:
    """Register skill dir for imports and pin health.db path before any health_db use."""
    root_s = str(_HEALTH_COACH_ROOT)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)

    import health_db as hb

    hb.set_db_path(path)


def health_store_bootstrap() -> dict[str, Any]:
    import health_db as hb

    hb.bootstrap_db()
    return {"ok": True, "path": str(hb.resolved_db_path())}


def upsert_biometric_sample(
    *,
    sample_date: str,
    source: str,
    hrv_rmssd_milli: float | None = None,
    resting_hr: float | None = None,
    sleep_hours: float | None = None,
    sleep_performance_pct: float | None = None,
    recovery_score: float | None = None,
    avg_strain: float | None = None,
    workout_kcal: float | None = None,
    workout_count: int | None = None,
    body_weight_kg: float | None = None,
) -> None:
    import health_db as hb

    hb.upsert_biometric(
        sample_date,
        hrv_rmssd_milli=hrv_rmssd_milli,
        resting_hr=resting_hr,
        sleep_hours=sleep_hours,
        sleep_performance_pct=sleep_performance_pct,
        recovery_score=recovery_score,
        avg_strain=avg_strain,
        workout_kcal=workout_kcal,
        workout_count=workout_count,
        body_weight_kg=body_weight_kg,
        source=source,
    )


def sqlite_tables_with_counts(path: Path) -> list[dict[str, Any]]:
    """Best-effort row counts for user tables (read-only connection)."""
    import sqlite3
    import urllib.parse

    if not path.is_file():
        return []

    q = urllib.parse.quote(str(path.resolve()))
    uri = f"file:{q}?mode=ro"
    out: list[dict[str, Any]] = []
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """,
        ).fetchall()
        for (name,) in rows:
            try:
                n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            except sqlite3.Error:
                n = -1
            out.append({"name": name, "row_count": int(n)})
    finally:
        conn.close()
    return out


def mirror_ingest_payload_to_biometric(*, payload: dict[str, Any], source: str, occurred_at: str) -> dict[str, Any]:
    """Map structured ingest payloads into ``biometric_samples`` when fields allow."""
    keys = (
        "hrv_rmssd_milli",
        "resting_hr",
        "sleep_hours",
        "sleep_performance_pct",
        "recovery_score",
        "avg_strain",
        "workout_kcal",
        "workout_count",
        "body_weight_kg",
    )

    sample_date: str | None = None
    for k in ("sample_date", "date", "day"):
        v = payload.get(k)
        if isinstance(v, str) and len(v) >= 10:
            sample_date = v[:10]
            break
    if sample_date is None and occurred_at and len(str(occurred_at)) >= 10:
        sample_date = str(occurred_at)[:10]

    if not sample_date:
        return {"ok": True, "skipped": True, "reason": "no_sample_date"}

    kwargs: dict[str, Any] = {}
    for k in keys:
        if k not in payload:
            continue
        val = payload[k]
        if val is None or isinstance(val, bool):
            continue
        if k == "workout_count":
            try:
                kwargs[k] = int(val)
            except (TypeError, ValueError):
                continue
        else:
            try:
                kwargs[k] = float(val)
            except (TypeError, ValueError):
                continue

    if not kwargs:
        return {"ok": True, "skipped": True, "reason": "no_biometric_fields"}

    try:
        upsert_biometric_sample(sample_date=sample_date, source=source, **kwargs)
        return {"ok": True, "mirrored": True, "sample_date": sample_date}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _clamp_days_meals_limits(days: int, meals_row_limit: int) -> tuple[int, int]:
    d = max(1, min(90, int(days)))
    m = max(1, min(200, int(meals_row_limit)))
    return d, m


def health_db_meals_window(*, days: int, meals_row_limit: int = 50) -> dict[str, Any]:
    """Meals from health.db over inclusive calendar-day window (UTC dates)."""
    import health_db as hb

    days_i, lim = _clamp_days_meals_limits(days, meals_row_limit)
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_i - 1)
    start_s = start.isoformat()
    end_s = today.isoformat()
    try:
        rows = hb.get_meals_range(start_s, end_s)
    except Exception as e:
        return {
            "available": False,
            "start_date": start_s,
            "end_date": end_s,
            "count": 0,
            "total_calories": 0.0,
            "recent": [],
            "error": str(e),
        }
    total_cal = sum(float(r.get("calories") or 0) for r in rows)
    tail = rows[-lim:] if len(rows) > lim else rows
    recent: list[dict[str, Any]] = []
    for r in tail:
        recent.append(
            {
                "id": r.get("id"),
                "meal_ts": r.get("meal_ts"),
                "meal_date": r.get("meal_date"),
                "description": r.get("description"),
                "protein_g": r.get("protein_g"),
                "carbs_g": r.get("carbs_g"),
                "fats_g": r.get("fats_g"),
                "fiber_g": r.get("fiber_g"),
                "calories": r.get("calories"),
                "input_type": r.get("input_type"),
                "source_ref": r.get("source_ref"),
            },
        )
    return {
        "available": True,
        "start_date": start_s,
        "end_date": end_s,
        "count": len(rows),
        "total_calories": round(total_cal, 4),
        "recent": recent,
    }


def health_db_biometrics_window(*, days: int) -> dict[str, Any]:
    """Biometric sample rows from health.db indexed by sample_date in the window."""
    import health_db as hb

    days_i, _ = _clamp_days_meals_limits(days, 1)
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days_i - 1)
    start_s = start.isoformat()
    end_s = today.isoformat()
    try:
        rows = hb.get_biometrics_range(start_s, end_s)
    except Exception as e:
        return {
            "available": False,
            "start_date": start_s,
            "end_date": end_s,
            "row_count": 0,
            "by_source": {},
            "error": str(e),
        }
    by_source: dict[str, int] = {}
    for r in rows:
        src = str(r.get("source") or "unknown")
        by_source[src] = by_source.get(src, 0) + 1
    slim = []
    for r in rows:
        slim.append(
            {
                "sample_date": r.get("sample_date"),
                "source": r.get("source"),
                "recovery_score": r.get("recovery_score"),
                "sleep_hours": r.get("sleep_hours"),
                "hrv_rmssd_milli": r.get("hrv_rmssd_milli"),
                "resting_hr": r.get("resting_hr"),
                "sleep_performance_pct": r.get("sleep_performance_pct"),
                "avg_strain": r.get("avg_strain"),
                "workout_kcal": r.get("workout_kcal"),
                "workout_count": r.get("workout_count"),
                "body_weight_kg": r.get("body_weight_kg"),
            },
        )
    return {
        "available": True,
        "start_date": start_s,
        "end_date": end_s,
        "row_count": len(rows),
        "by_source": by_source,
        "rows": slim,
    }
