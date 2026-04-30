"""
Dual-write ingestion layer: pushes data from each stream into the SQLite
health DB while leaving the existing JSON/CSV stores intact.

All functions are idempotent (safe to call multiple times for the same date).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from health_db import (
    get_biometrics,
    get_latest_aqi,
    get_latest_weather,
    get_meals,
    get_sleep_cycles,
    insert_aqi,
    insert_meal,
    insert_qualitative_modifier,
    insert_weather,
    upsert_allostatic_score,
    upsert_biometric,
    upsert_sleep_cycle,
)
from models import ScheduleDay, WhoopDay


def _load_thresholds() -> Dict[str, Any]:
    from pathlib import Path
    p = Path(__file__).resolve().parent / "thresholds.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Biometrics (from WhoopDay)
# ---------------------------------------------------------------------------

def ingest_whoop_day(whoop: WhoopDay) -> None:
    """Write a WhoopDay aggregate into biometric_samples."""
    upsert_biometric(
        whoop.date,
        hrv_rmssd_milli=whoop.hrv_rmssd_milli,
        resting_hr=whoop.resting_hr,
        sleep_hours=whoop.sleep_hours,
        sleep_performance_pct=whoop.sleep_performance_pct,
        recovery_score=whoop.recovery_score,
        avg_strain=whoop.avg_strain,
        workout_kcal=whoop.workout_kcal,
        workout_count=whoop.workout_count,
        body_weight_kg=whoop.body_weight_kg,
        source="whoop",
    )


def ingest_whoop_sleep_records(sleep_records: List[Dict[str, Any]]) -> None:
    """Parse raw WHOOP sleep API records into the sleep_cycles table."""
    for rec in sleep_records:
        try:
            sleep_id = str(rec.get("id", ""))
            start = rec.get("start") or rec.get("created_at", "")
            end = rec.get("end") or rec.get("updated_at", "")
            if not start or not end:
                continue

            sleep_date = start[:10]
            score = rec.get("score") or {}
            stage = score.get("stage_summary") or {}

            def ms_to_h(ms: Any) -> Optional[float]:
                if isinstance(ms, (int, float)) and ms > 0:
                    return round(float(ms) / 3_600_000.0, 3)
                return None

            total_ms = stage.get("total_in_bed_time_milli")
            rem_ms = stage.get("total_rem_sleep_time_milli")
            deep_ms = stage.get("total_slow_wave_sleep_time_milli")
            light_ms = stage.get("total_light_sleep_time_milli")
            perf = score.get("sleep_performance_percentage")

            upsert_sleep_cycle(
                sleep_id or None,
                start,
                end,
                sleep_date,
                total_hours=ms_to_h(total_ms),
                rem_hours=ms_to_h(rem_ms),
                deep_hours=ms_to_h(deep_ms),
                light_hours=ms_to_h(light_ms),
                performance_pct=float(perf) if isinstance(perf, (int, float)) else None,
                raw_json=json.dumps(rec),
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Meals (from food-macro-tracker state.json entries)
# ---------------------------------------------------------------------------

def ingest_meal_entries(entries: List[Dict[str, Any]], meal_date: str) -> List[int]:
    """Ingest a list of food-macro-tracker entries into the meals table.
    Returns list of inserted/existing meal IDs."""
    ids: List[int] = []
    existing = get_meals(meal_date)
    existing_keys = {(m["meal_ts"], m["description"]) for m in existing}

    for entry in entries:
        ts = entry.get("timestamp", _now_iso())
        desc = entry.get("description", "")
        if (ts, desc) in existing_keys:
            continue
        mid = insert_meal(
            ts,
            meal_date,
            description=desc,
            protein_g=float(entry.get("protein_g", 0)),
            carbs_g=float(entry.get("carbs_g", 0)),
            fats_g=float(entry.get("fats_g", 0)),
            fiber_g=float(entry.get("fiber_g", 0)),
            calories=float(entry.get("calories", 0)),
            input_type=entry.get("input_type"),
            source_ref=entry.get("source", ""),
        )
        ids.append(mid)
    return ids


# ---------------------------------------------------------------------------
# Schedule / Allostatic load (from ScheduleDay)
# ---------------------------------------------------------------------------

def ingest_schedule_day(schedule: ScheduleDay) -> None:
    """Convert a ScheduleDay into an allostatic_scores record."""
    thr = _load_thresholds().get("allostatic_load", {})
    scale = float(thr.get("scale_to", 10.0))
    max_raw = float(thr.get("max_raw", 100.0))

    score_1_10 = round((schedule.schedule_load_score / max_raw) * scale, 2)
    score_1_10 = max(1.0, min(10.0, score_1_10))

    components = {
        "event_count": schedule.event_count,
        "busy_hours": schedule.busy_hours,
        "due_today_count": schedule.due_today_count,
        "overdue_count": schedule.overdue_count,
        "schedule_raw_score": schedule.schedule_load_score,
    }

    upsert_allostatic_score(
        schedule.date,
        score_1_10,
        event_count=schedule.event_count,
        busy_hours=schedule.busy_hours,
        due_today_count=schedule.due_today_count,
        overdue_count=schedule.overdue_count,
        schedule_raw_score=schedule.schedule_load_score,
        components=components,
    )


# ---------------------------------------------------------------------------
# Meal-sleep linker
# ---------------------------------------------------------------------------

def link_meals_to_sleep_cycles(meal_date: str) -> int:
    """
    Match meals from meal_date to the next sleep cycle that starts after the last
    meal of the day, then bucket by how close to sleep the meal occurred.

    Window buckets:
      - ">4h before sleep"  delta > 240 min
      - "2-4h before sleep" 120 <= delta <= 240
      - "<2h before sleep"  delta < 120

    Returns count of links created.
    """
    from health_db import link_meal_to_sleep
    from datetime import datetime as dt

    meals = get_meals(meal_date)
    if not meals:
        return 0

    # Sleep cycles that start on meal_date or the following day
    from health_db import db_conn
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, sleep_start_ts FROM sleep_cycles
            WHERE sleep_date=? OR sleep_date=date(?, '+1 day')
            ORDER BY sleep_start_ts ASC LIMIT 1
            """,
            (meal_date, meal_date),
        ).fetchall()
        sleep_cycles_local = [dict(r) for r in rows]

    if not sleep_cycles_local:
        return 0

    sc = sleep_cycles_local[0]
    sleep_start = dt.fromisoformat(sc["sleep_start_ts"].replace("Z", "+00:00"))

    linked = 0
    for meal in meals:
        try:
            meal_ts = dt.fromisoformat(meal["meal_ts"].replace("Z", "+00:00"))
            if meal_ts >= sleep_start:
                continue
            delta = int((sleep_start - meal_ts).total_seconds() / 60)
            if delta > 240:
                bucket = ">4h before sleep"
            elif delta >= 120:
                bucket = "2-4h before sleep"
            else:
                bucket = "<2h before sleep"
            link_meal_to_sleep(meal["id"], sc["id"], delta, bucket)
            linked += 1
        except Exception:
            pass
    return linked


# ---------------------------------------------------------------------------
# Weather & AQI
# ---------------------------------------------------------------------------

def ingest_weather_record(
    obs_ts: str,
    *,
    temp_c: Optional[float],
    feels_like_c: Optional[float],
    humidity_pct: Optional[float],
    condition: Optional[str],
    wind_kph: Optional[float],
    location: str = "Fort Greene, Brooklyn",
    lat: float = 40.6892,
    lon: float = -73.9442,
    raw: Optional[Dict[str, Any]] = None,
) -> None:
    obs_date = obs_ts[:10]
    insert_weather(
        obs_ts,
        obs_date,
        location=location,
        lat=lat,
        lon=lon,
        temp_c=temp_c,
        feels_like_c=feels_like_c,
        humidity_pct=humidity_pct,
        condition=condition,
        wind_kph=wind_kph,
        raw_json=json.dumps(raw) if raw else None,
    )


def ingest_aqi_record(
    obs_ts: str,
    *,
    aqi: Optional[int],
    pm25: Optional[float],
    pm10: Optional[float],
    category: Optional[str],
    location: str = "Fort Greene, Brooklyn",
    source: str = "open-meteo",
    raw: Optional[Dict[str, Any]] = None,
) -> None:
    obs_date = obs_ts[:10]
    insert_aqi(
        obs_ts,
        obs_date,
        location=location,
        aqi=aqi,
        pm25=pm25,
        pm10=pm10,
        category=category,
        source=source,
        raw_json=json.dumps(raw) if raw else None,
    )


# ---------------------------------------------------------------------------
# Qualitative modifiers
# ---------------------------------------------------------------------------

_INJURY_KEYWORDS = [
    "tweaky", "tweak", "pain", "hurt", "sore", "ache", "stiff",
    "tight", "strain", "sprain", "inflamed", "swollen", "bruised",
]
_FATIGUE_KEYWORDS = [
    "exhausted", "wiped", "drained", "tired", "fatigued", "depleted",
    "sluggish", "flat", "heavy",
]
_ENERGY_KEYWORDS = [
    "energized", "great", "strong", "fresh", "rested", "recovered",
    "good", "awesome", "excellent",
]


def _classify_note(text: str) -> tuple[str, str]:
    """Return (modifier_type, severity) for a qualitative note."""
    lower = text.lower()
    for kw in _INJURY_KEYWORDS:
        if kw in lower:
            return "injury_risk", "high"
    for kw in _FATIGUE_KEYWORDS:
        if kw in lower:
            return "fatigue", "moderate"
    for kw in _ENERGY_KEYWORDS:
        if kw in lower:
            return "energy_positive", "low"
    return "general", "low"


def ingest_qualitative_note(note_text: str, note_ts: Optional[str] = None) -> Dict[str, Any]:
    """Classify and persist a free-form physical condition note."""
    modifier_type, severity = _classify_note(note_text)
    nid = insert_qualitative_modifier(note_text, modifier_type, severity, note_ts)
    return {"id": nid, "modifier_type": modifier_type, "severity": severity}
