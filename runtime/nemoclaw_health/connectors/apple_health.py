"""
Apple Health Export (export.zip / export.xml) -> raw_events (Phase 2).

Uses streaming iterparse inside the ZIP archive; dedupes via connector_idempotency.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nemoclaw_health.data_entry import utc_now_iso
from nemoclaw_health.db import (
    Database,
    fetch_connector_state,
    idempotency_seen,
    insert_raw_event,
    put_connector_state,
    record_idempotency,
)

CONNECTOR_APPLE = "apple_health"

HK_MAP: dict[str, str] = {
    "HKQuantityTypeIdentifierHeartRate": "hk_heart_rate",
    "HKQuantityTypeIdentifierRestingHeartRate": "hk_resting_heart_rate",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hk_hrv_sdnn",
    "HKQuantityTypeIdentifierWalkingHeartRateAverage": "hk_walking_hr_avg",
    "HKQuantityTypeIdentifierStepCount": "hk_steps",
    "HKQuantityTypeIdentifierDistanceWalkingRunning": "hk_distance_walking_running",
    "HKQuantityTypeIdentifierDistanceCycling": "hk_distance_cycling",
    "HKQuantityTypeIdentifierDistanceSwimming": "hk_distance_swimming",
    "HKQuantityTypeIdentifierFlightsClimbed": "hk_flights_climbed",
    "HKQuantityTypeIdentifierActiveEnergyBurned": "hk_active_energy",
    "HKQuantityTypeIdentifierBasalEnergyBurned": "hk_basal_energy",
    "HKQuantityTypeIdentifierAppleExerciseTime": "hk_exercise_time",
    "HKQuantityTypeIdentifierAppleStandTime": "hk_stand_time",
    "HKQuantityTypeIdentifierAppleWalkingSteadiness": "hk_walking_steadiness",
    "HKQuantityTypeIdentifierWalkingSpeed": "hk_walking_speed",
    "HKQuantityTypeIdentifierWalkingStepLength": "hk_walking_step_length",
    "HKQuantityTypeIdentifierWalkingAsymmetryPercentage": "hk_walking_asymmetry_pct",
    "HKQuantityTypeIdentifierRunningSpeed": "hk_running_speed",
    "HKQuantityTypeIdentifierVO2Max": "hk_vo2_max",
    "HKQuantityTypeIdentifierRespiratoryRate": "hk_respiratory_rate",
    "HKQuantityTypeIdentifierOxygenSaturation": "hk_spo2",
    "HKQuantityTypeIdentifierBodyTemperature": "hk_body_temperature",
    "HKQuantityTypeIdentifierBodyMass": "hk_body_mass",
    "HKQuantityTypeIdentifierBodyMassIndex": "hk_bmi",
    "HKQuantityTypeIdentifierBodyFatPercentage": "hk_body_fat_pct",
    "HKQuantityTypeIdentifierLeanBodyMass": "hk_lean_body_mass",
    "HKQuantityTypeIdentifierHeight": "hk_height",
    "HKQuantityTypeIdentifierWaistCircumference": "hk_waist_circumference",
    "HKQuantityTypeIdentifierBloodPressureSystolic": "hk_bp_systolic",
    "HKQuantityTypeIdentifierBloodPressureDiastolic": "hk_bp_diastolic",
    "HKQuantityTypeIdentifierBloodGlucose": "hk_blood_glucose",
    "HKQuantityTypeIdentifierDietaryEnergyConsumed": "hk_dietary_energy",
    "HKQuantityTypeIdentifierDietaryProtein": "hk_dietary_protein",
    "HKQuantityTypeIdentifierDietaryCarbohydrates": "hk_dietary_carbs",
    "HKQuantityTypeIdentifierDietaryFatTotal": "hk_dietary_fat",
    "HKQuantityTypeIdentifierDietarySugar": "hk_dietary_sugar",
    "HKQuantityTypeIdentifierDietaryFiber": "hk_dietary_fiber",
    "HKQuantityTypeIdentifierDietaryWater": "hk_dietary_water",
    "HKQuantityTypeIdentifierDietaryCaffeine": "hk_dietary_caffeine",
    "HKCategoryTypeIdentifierSleepAnalysis": "hk_sleep_analysis",
    "HKCategoryTypeIdentifierMindfulSession": "hk_mindful_session",
    "HKCategoryTypeIdentifierAppleStandHour": "hk_stand_hour",
}


def apple_record_date_to_iso(s: str | None) -> str:
    if not s:
        return utc_now_iso()
    txt = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S.%f %z"):
        try:
            dt = datetime.strptime(txt, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            utc = dt.astimezone(timezone.utc).replace(microsecond=0)
            return utc.isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    try:
        iso_txt = txt.replace(" ", "T", 1) if txt[:1].isdigit() else txt
        if iso_txt.endswith("Z"):
            iso_txt = iso_txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(iso_txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        utc = dt.astimezone(timezone.utc).replace(microsecond=0)
        return utc.isoformat().replace("+00:00", "Z")
    except ValueError:
        return utc_now_iso()


def _local_name(tag: str) -> str:
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def _metadata_sync_identifier(elem: ET.Element) -> str | None:
    for child in list(elem):
        if _local_name(child.tag) != "MetadataEntry":
            continue
        key_raw = ""
        val_raw = ""
        for ak, av in child.attrib.items():
            lk = ak.lower()
            if lk == "key":
                key_raw = str(av or "")
            elif lk == "value":
                val_raw = str(av or "")
        if "SyncIdentifier" in key_raw and val_raw.strip():
            return val_raw.strip()
    return None


def _fallback_dedupe_key(typ: str, start: str, end: str, value: str, unit: str, src: str) -> str:
    raw = "|".join((typ, start, end, value, unit, src))
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"apple_health::{h}"


def locate_export_xml_in_zip(zp: zipfile.ZipFile) -> str | None:
    names = sorted(
        n for n in zp.namelist() if not n.endswith("/") and n.lower().endswith("export.xml")
    )
    if not names:
        return None
    for pref in ("apple_health_export/export.xml", "AppleHealthExport/export.xml", "Export.xml"):
        if pref in names:
            return pref
    return min(names, key=len)


def _workout_metric_slug(activity_type: str) -> str:
    if not activity_type:
        return "hk_workout_unknown"
    base = activity_type.replace("HKWorkoutActivityType", "").strip("_") or "unknown"
    return f"hk_workout_{base.lower()}"


def _elem_attr(elem: ET.Element, *keys: str) -> str:
    for k in keys:
        v = elem.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _ingest_apple_workout(
    database: Database,
    elem: ET.Element,
    stats: dict[str, Any],
) -> None:
    stats["records_seen"] += 1
    act = _elem_attr(elem, "workoutActivityType", "WorkoutActivityType")
    start_raw = _elem_attr(elem, "startDate", "StartDate")
    end_raw = _elem_attr(elem, "endDate", "EndDate") or start_raw
    src = _elem_attr(elem, "sourceName", "SourceName")
    duration = _elem_attr(elem, "duration", "Duration")
    dist = _elem_attr(elem, "totalDistance", "TotalDistance")
    energy = _elem_attr(elem, "totalEnergyBurned", "TotalEnergyBurned")
    creation = _elem_attr(elem, "creationDate", "CreationDate")

    if not start_raw:
        elem.clear()
        stats["records_skipped_type"] += 1
        return

    sync_uuid = _metadata_sync_identifier(elem)
    start_iso = apple_record_date_to_iso(start_raw)
    end_iso = apple_record_date_to_iso(end_raw)

    dk_base = sync_uuid.strip() if sync_uuid else ""
    dk = (
        f"apple_health:workout:uuid:{dk_base}"
        if dk_base
        else _fallback_dedupe_key(f"Workout:{act}", start_iso, end_iso, duration, dist, src)
    )

    with database.transaction() as chk:
        if idempotency_seen(chk, CONNECTOR_APPLE, dk):
            stats["records_skipped_duplicate"] += 1
            elem.clear()
            return

    act_short = _workout_metric_slug(act)
    metric = act_short

    payload = {
        "hk_workout_activity_type": act,
        "metric": metric,
        "duration_seconds": duration,
        "total_distance": dist,
        "total_energy_burned": energy,
        "start": start_iso,
        "end": end_iso,
        "source": src,
        "creation_date": creation,
    }
    provenance = {"connector": "apple_health", "had_sync_uuid": bool(dk_base), "kind": "workout"}

    with database.transaction() as cur:
        raw_id_val = insert_raw_event(
            cur,
            occurred_at=start_iso,
            source="healthkit_export",
            event_type="healthkit_workout",
            domain_slug="apple_health_workouts",
            payload=payload,
            confidence=0.93,
            provenance=provenance,
        )
        record_idempotency(cur, CONNECTOR_APPLE, dk, raw_id_val)

    stats["records_ingested"] += 1
    elem.clear()


def ingest_apple_health_export_from_zip(database: Database, zip_path: Path) -> dict[str, Any]:
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise FileNotFoundError(str(zip_path))

    stats: dict[str, Any] = {
        "records_seen": 0,
        "records_ingested": 0,
        "records_skipped_type": 0,
        "records_skipped_duplicate": 0,
    }
    member: str | None = None

    with zipfile.ZipFile(zip_path) as zp:
        member = locate_export_xml_in_zip(zp)
        if not member:
            raise ValueError(
                "ZIP does not contain export.xml (expected e.g. apple_health_export/export.xml).",
            )
        with zp.open(member, "r") as raw_xml:
            for _event, elem in ET.iterparse(raw_xml, events=("end",)):
                tag = _local_name(elem.tag)
                if tag == "Workout":
                    _ingest_apple_workout(database, elem, stats)
                    continue
                if tag != "Record":
                    continue

                stats["records_seen"] += 1
                typ_attr = elem.get("type") or elem.get("Type") or ""
                typ_s = str(typ_attr)
                mapped_kind = HK_MAP.get(typ_s)
                if not mapped_kind:
                    stats["records_skipped_type"] += 1
                    elem.clear()
                    continue

                sync_uuid = _metadata_sync_identifier(elem)
                start_raw = elem.get("startDate") or elem.get("StartDate") or ""
                end_raw = elem.get("endDate") or elem.get("EndDate") or start_raw
                unit_val = elem.get("unit") or elem.get("Unit") or ""
                src = elem.get("sourceName") or elem.get("SourceName") or ""
                raw_val = str(elem.get("value") or elem.get("Value") or "")
                start_iso = apple_record_date_to_iso(str(start_raw) if start_raw else None)
                end_iso = apple_record_date_to_iso(str(end_raw) if end_raw else None)

                dk_base = sync_uuid.strip() if sync_uuid else ""
                dk = (
                    f"apple_health:uuid:{dk_base}"
                    if dk_base
                    else _fallback_dedupe_key(typ_s, start_iso, end_iso, raw_val, unit_val, src)
                )

                with database.transaction() as chk:
                    if idempotency_seen(chk, CONNECTOR_APPLE, dk):
                        stats["records_skipped_duplicate"] += 1
                        elem.clear()
                        continue

                payload = {
                    "hk_type": typ_s,
                    "metric": mapped_kind,
                    "unit": unit_val,
                    "value": raw_val,
                    "start": start_iso,
                    "end": end_iso,
                    "source": src,
                }
                provenance = {
                    "connector": "apple_health",
                    "had_sync_uuid": bool(dk_base),
                }

                with database.transaction() as cur:
                    raw_id_val = insert_raw_event(
                        cur,
                        occurred_at=start_iso,
                        source="healthkit_export",
                        event_type="healthkit_quantity",
                        domain_slug="apple_health_metrics",
                        payload=payload,
                        confidence=0.92,
                        provenance=provenance,
                    )
                    record_idempotency(cur, CONNECTOR_APPLE, dk, raw_id_val)

                stats["records_ingested"] += 1
                elem.clear()

    with database.transaction() as cur_fin:
        st_w = fetch_connector_state(cur_fin, CONNECTOR_APPLE)
        imp = dict(st_w.get("import") or {})
        imp["last_run_at"] = utc_now_iso()
        imp["zip_name"] = zip_path.name
        imp["zip_member_used"] = member
        imp["last_stats_json"] = json.dumps(stats)
        st_w["import"] = imp
        put_connector_state(cur_fin, CONNECTOR_APPLE, st_w)

    stats["zip_member_used"] = member
    stats["connector"] = CONNECTOR_APPLE
    return {"ok": True, **stats}


def apple_health_connector_status(database: Database) -> dict[str, Any]:
    with database.transaction() as cur_f:
        st = fetch_connector_state(cur_f, CONNECTOR_APPLE)
    return {"connector": CONNECTOR_APPLE, "import": dict(st.get("import") or {})}
