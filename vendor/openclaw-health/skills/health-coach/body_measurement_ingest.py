"""
Fetch latest body measurements from Whoop and persist to SQLite.

Usage:
    python body_measurement_ingest.py          # fetch + persist today
    python body_measurement_ingest.py --date YYYY-MM-DD   # backfill a specific date
    python body_measurement_ingest.py --status           # show latest stored record
    python body_measurement_ingest.py --history          # show all stored records
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

# Ensure the health-coach dir is on the path for imports
sys.path.insert(0, __file__)

from health_db import (
    get_body_measurements_range,
    get_latest_body_measurement,
    upsert_body_measurement,
)
from whoop_client import WhoopClient


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def ingest(date_str: str | None = None) -> dict:
    """
    Fetch body measurement from Whoop API and upsert to SQLite.

    Args:
        date_str: Measurement date to store (defaults to today in UTC).
                  Whoop returns the *current* body record regardless of date,
                  so this just stamps the record for that day.
    """
    date_str = date_str or _date_str(datetime.now(timezone.utc))
    ts = _now_iso()

    client = WhoopClient()
    raw = client.get_body_measurement()

    height_meter = raw.get("height_meter")
    weight_kilogram = raw.get("weight_kilogram")
    max_heart_rate = raw.get("max_heart_rate")

    upsert_body_measurement(
        measurement_date=date_str,
        measurement_ts=ts,
        height_meter=height_meter,
        weight_kilogram=weight_kilogram,
        max_heart_rate=max_heart_rate,
        source="whoop",
        raw_json=json.dumps(raw),
    )

    return {
        "date": date_str,
        "height_meter": height_meter,
        "weight_kilogram": weight_kilogram,
        "max_heart_rate": max_heart_rate,
    }


def status() -> None:
    """Print the latest stored body measurement record."""
    row = get_latest_body_measurement(source="whoop")
    if row:
        print(f"Latest record: {row['measurement_date']}")
        print(f"  Height:       {row['height_meter']} m")
        print(f"  Weight:       {row['weight_kilogram']} kg")
        print(f"  Max HR:       {row['max_heart_rate']} bpm")
        print(f"  Ingested at:  {row['ingest_ts']}")
        print(f"  Source:       {row['source']}")
    else:
        print("No body measurement records found. Run without --status to ingest.")


def history() -> None:
    """Print all stored body measurement records."""
    rows = get_body_measurements_range("2000-01-01", "2100-01-01")
    if not rows:
        print("No body measurement records found.")
        return
    print(f"{'Date':<12} {'Height(m)':<12} {'Weight(kg)':<12} {'MaxHR':<8} {'Ingested'}")
    print("-" * 70)
    for r in rows:
        print(
            f"{r['measurement_date']:<12} "
            f"{str(r['height_meter'] or ''):<12} "
            f"{str(r['weight_kilogram'] or ''):<12} "
            f"{str(r['max_heart_rate'] or ''):<8} "
            f"{r['ingest_ts']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Whoop body measurement ingest")
    parser.add_argument("--date", help="Measurement date YYYY-MM-DD (default: today)")
    parser.add_argument("--status", action="store_true", help="Show latest stored record")
    parser.add_argument("--history", action="store_true", help="Show all stored records")
    args = parser.parse_args()

    if args.status:
        status()
        return

    if args.history:
        history()
        return

    result = ingest(args.date)
    print(
        f"Saved body measurement for {result['date']}:\n"
        f"  Height:     {result['height_meter']} m\n"
        f"  Weight:     {result['weight_kilogram']} kg\n"
        f"  Max HR:     {result['max_heart_rate']} bpm"
    )


if __name__ == "__main__":
    main()
