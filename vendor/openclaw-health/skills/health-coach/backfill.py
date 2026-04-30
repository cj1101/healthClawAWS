#!/usr/bin/env python3
"""
Backfill script — migrates existing JSON/CSV health data into the SQLite DB.

All operations are IDEMPOTENT — safe to run multiple times.

Commands:
    python backfill.py all              # run all backfills
    python backfill.py biometrics       # from health_store.json
    python backfill.py meals            # from food-macro-tracker/state.json + nutrition_history.csv
    python backfill.py status           # show record counts in SQLite
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

SKILL_DIR = Path(__file__).resolve().parent
FOOD_DIR = SKILL_DIR.parent / "food-macro-tracker"


def backfill_biometrics() -> Dict[str, Any]:
    """Backfill WhoopDay aggregates from health_store.json."""
    from health_db import upsert_biometric
    from models import WhoopDay
    from db_ingest import ingest_whoop_day

    store_file = SKILL_DIR / "data" / "health_store.json"
    if not store_file.exists():
        return {"status": "skipped", "reason": "health_store.json not found"}

    store = json.loads(store_file.read_text(encoding="utf-8"))
    snapshots = store.get("snapshots", {})
    written = 0
    errors = 0

    for date_str, snap in snapshots.items():
        try:
            whoop_data = snap.get("whoop", {})
            whoop = WhoopDay(
                date=date_str,
                workout_kj=float(whoop_data.get("workout_kj") or 0),
                workout_kcal=float(whoop_data.get("workout_kcal") or 0),
                workout_count=int(whoop_data.get("workout_count") or 0),
                avg_strain=float(whoop_data.get("avg_strain") or 0),
                recovery_score=whoop_data.get("recovery_score"),
                resting_hr=whoop_data.get("resting_hr"),
                hrv_rmssd_milli=whoop_data.get("hrv_rmssd_milli"),
                sleep_hours=whoop_data.get("sleep_hours"),
                sleep_performance_pct=whoop_data.get("sleep_performance_pct"),
                body_weight_kg=whoop_data.get("body_weight_kg"),
            )
            ingest_whoop_day(whoop)
            written += 1
        except Exception as exc:
            errors += 1

    return {"status": "ok", "dates_written": written, "errors": errors, "source": "health_store.json"}


def backfill_meals() -> Dict[str, Any]:
    """Backfill meal entries from state.json and nutrition_history.csv."""
    from db_ingest import ingest_meal_entries

    written = 0
    errors = 0

    # --- From current state.json (today's entries with timestamps) ---
    state_file = FOOD_DIR / "state.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            entries = state.get("entries", [])
            date = state.get("current_date", "")
            if entries and date:
                ids = ingest_meal_entries(entries, date)
                written += len(ids)
        except Exception:
            errors += 1

    # --- From nutrition_history.csv (daily totals — no per-entry timestamps) ---
    csv_file = FOOD_DIR / "nutrition_history.csv"
    if csv_file.exists():
        try:
            with csv_file.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    date = row.get("date", "")
                    if not date:
                        continue
                    # Synthesize a single aggregated entry per day at noon
                    synthetic_ts = f"{date}T12:00:00-05:00"
                    synthetic_entry: Dict[str, Any] = {
                        "timestamp": synthetic_ts,
                        "description": "Daily total (backfilled from CSV)",
                        "protein_g": float(row.get("protein_g") or 0),
                        "carbs_g": float(row.get("carbs_g") or 0),
                        "fats_g": float(row.get("fats_g") or 0),
                        "fiber_g": float(row.get("fiber_g") or 0),
                        "calories": float(row.get("calories") or 0),
                        "input_type": "csv_backfill",
                    }
                    ids = ingest_meal_entries([synthetic_entry], date)
                    written += len(ids)
        except Exception:
            errors += 1

    return {"status": "ok", "entries_written": written, "errors": errors}


def backfill_all() -> Dict[str, Any]:
    results: Dict[str, Any] = {}
    results["biometrics"] = backfill_biometrics()
    results["meals"] = backfill_meals()
    return results


def db_status() -> Dict[str, Any]:
    """Return row counts for all SQLite tables."""
    from health_db import db_conn
    tables = [
        "biometric_samples", "sleep_cycles", "meals", "meal_sleep_links",
        "calendar_events", "tasks", "allostatic_scores", "weather_samples",
        "aqi_samples", "qualitative_modifiers", "goal_proposals",
    ]
    counts = {}
    with db_conn() as conn:
        for t in tables:
            try:
                row = conn.execute(f"SELECT COUNT(*) as n FROM {t}").fetchone()
                counts[t] = row["n"] if row else 0
            except Exception:
                counts[t] = "error"
    return {"db_path": str(SKILL_DIR / "data" / "health.db"), "table_counts": counts}


def cmd_all(_args: argparse.Namespace) -> int:
    result = backfill_all()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_biometrics(_args: argparse.Namespace) -> int:
    result = backfill_biometrics()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_meals(_args: argparse.Namespace) -> int:
    result = backfill_meals()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    result = db_status()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill existing data into SQLite health DB")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("all", help="Run all backfills").set_defaults(func=cmd_all)
    sub.add_parser("biometrics", help="Backfill WHOOP data").set_defaults(func=cmd_biometrics)
    sub.add_parser("meals", help="Backfill meal data").set_defaults(func=cmd_meals)
    sub.add_parser("status", help="Show DB row counts").set_defaults(func=cmd_status)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
