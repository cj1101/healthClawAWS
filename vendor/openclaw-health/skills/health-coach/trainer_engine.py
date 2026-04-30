#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from result_store import write_agent_result


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _pick_intensity(readiness_score: float) -> str:
    if readiness_score >= 67:
        return "high"
    if readiness_score >= 34:
        return "moderate"
    return "low"


def build_training_plan(
    request_text: str,
    activity: Optional[Dict[str, Any]] = None,
    date_str: Optional[str] = None,
) -> Dict[str, Any]:
    request = (request_text or "").lower()
    date = date_str or _today()
    activity = activity or {}
    readiness = float(activity.get("readiness_score") or 50.0)
    sleep_hours = float(activity.get("sleep_hours") or 0.0)
    workout_kcal = float(activity.get("workout_kcal") or 0.0)
    intensity = _pick_intensity(readiness)

    modality = "mixed conditioning"
    if any(k in request for k in ["climb", "boulder"]):
        modality = "climbing"
    elif any(k in request for k in ["bike", "cycling"]):
        modality = "bike"
    elif "yoga" in request:
        modality = "yoga"

    base_duration = 60 if intensity == "high" else 45 if intensity == "moderate" else 30
    if sleep_hours and sleep_hours < 6.0:
        base_duration = max(20, base_duration - 15)

    plan = {
        "date": date,
        "modality": modality,
        "intensity": intensity,
        "duration_minutes": base_duration,
        "warmup_minutes": 10,
        "main_set_minutes": max(base_duration - 20, 10),
        "cooldown_minutes": 10,
        "notes": [
            "Adapt volume down if soreness or pain appears.",
            "Hydrate and include carbs around training windows.",
        ],
        "context": {
            "readiness_score": readiness,
            "sleep_hours": sleep_hours,
            "recent_workout_kcal": workout_kcal,
        },
    }
    return plan


def cmd_plan(args: argparse.Namespace) -> int:
    activity = {}
    if args.activity_json:
        activity = json.loads(args.activity_json)
    result = {
        "agent_id": "dick",
        "role": "trainer",
        "date": args.date or _today(),
        "request": args.request,
        "plan": build_training_plan(args.request, activity=activity, date_str=args.date),
    }
    output_path = write_agent_result("dick", result) if args.emit_result else None
    if output_path:
        result["result_path"] = str(output_path)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trainer engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="Build workout plan from Popeye context")
    p_plan.add_argument("--request", required=True, help="Training request text")
    p_plan.add_argument("--activity-json", default="", help="Popeye activity summary as JSON string")
    p_plan.add_argument("--date", default=None)
    p_plan.add_argument("--emit-result", action="store_true")
    p_plan.set_defaults(func=cmd_plan)
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
