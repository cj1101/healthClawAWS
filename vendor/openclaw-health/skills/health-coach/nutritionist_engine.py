#!/usr/bin/env python3
"""
Nutritionist — Silent nutrient timing analyst.

Role contract:
  - REACTIVE + EXCEPTION ALERTS ONLY.
  - Never speaks to the user proactively.
  - Returns structured alerts when thresholds are breached.
  - Manager decides whether/how to surface these to the user.

Analyses performed:
  1. High-fat late-meal → poor REM sleep correlation
  2. Daily protein adequacy
  3. Carbohydrate availability vs training output
  4. Calorie floor breach detection

CLI:
    python nutritionist_engine.py analyze               # analyze today
    python nutritionist_engine.py analyze --date 2026-03-20
    python nutritionist_engine.py report --days 14      # trend + correlations
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from result_store import write_agent_result

SKILL_DIR = Path(__file__).resolve().parent


def _load_thresholds() -> Dict[str, Any]:
    p = SKILL_DIR / "thresholds.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _date_n_ago(n: int, base: Optional[str] = None) -> str:
    base_dt = datetime.fromisoformat(base) if base else datetime.now(timezone.utc)
    return (base_dt - timedelta(days=n)).strftime("%Y-%m-%d")


def _rolling_baseline(date: str, days: int = 7) -> Dict[str, float]:
    from collections import defaultdict
    from health_db import get_meals_range

    start = _date_n_ago(days - 1, date)
    rows = get_meals_range(start, date)
    grouped: Dict[str, Dict[str, float]] = defaultdict(lambda: {"calories": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fats_g": 0.0})
    for row in rows:
        d = str(row.get("meal_date") or "")
        grouped[d]["calories"] += float(row.get("calories") or 0.0)
        grouped[d]["protein_g"] += float(row.get("protein_g") or 0.0)
        grouped[d]["carbs_g"] += float(row.get("carbs_g") or 0.0)
        grouped[d]["fats_g"] += float(row.get("fats_g") or 0.0)

    if not grouped:
        return {}
    day_values = list(grouped.values())
    return {
        "days_used": float(len(day_values)),
        "calories": round(sum(v["calories"] for v in day_values) / len(day_values), 1),
        "protein_g": round(sum(v["protein_g"] for v in day_values) / len(day_values), 1),
        "carbs_g": round(sum(v["carbs_g"] for v in day_values) / len(day_values), 1),
        "fats_g": round(sum(v["fats_g"] for v in day_values) / len(day_values), 1),
    }


def _goal_contradiction_flags(total_calories: float, total_carbs: float, total_protein: float) -> List[str]:
    from health_db import get_all_proposals

    contradictions: List[str] = []
    approved = [p for p in get_all_proposals(limit=25) if p.get("status") == "approved"]
    text = " ".join(f"{p.get('title', '')} {p.get('description', '')} {p.get('modification_text', '')}".lower() for p in approved)
    if not text:
        return contradictions

    if any(k in text for k in ["cut", "fat loss", "deficit"]) and total_calories > 3200:
        contradictions.append("GOAL CONTRADICTION: intake appears too high for an active cut/fat-loss goal.")
    if any(k in text for k in ["bulk", "mass"]) and total_calories < 2200:
        contradictions.append("GOAL CONTRADICTION: intake appears too low for an active bulk/mass goal.")
    if any(k in text for k in ["low carb", "keto"]) and total_carbs > 150:
        contradictions.append("GOAL CONTRADICTION: carbs exceed probable low-carb goal bounds.")
    if any(k in text for k in ["high protein", "strength"]) and total_protein < 120:
        contradictions.append("GOAL CONTRADICTION: protein appears low for active high-protein/strength goal.")
    return contradictions


def analyze_nutrient_timing(date_str: Optional[str] = None) -> Dict[str, Any]:
    """
    Analyze nutrient timing for a given date.

    Returns:
        alerts          list[str]  — threshold breaches to surface
        correlations    list[dict] — meal-sleep pattern findings
        daily_summary   dict       — macro totals snapshot
        flags_count     int        — number of actionable alerts
    """
    from health_db import (
        get_biometrics,
        get_meal_sleep_correlations,
        get_meals,
        get_sleep_cycles,
    )

    thr = _load_thresholds().get("nutrient_timing", {})
    date = date_str or _today()
    alerts: List[str] = []
    correlations: List[Dict[str, Any]] = []

    # --- Daily macro totals ---
    meals = get_meals(date)
    total_protein = sum(float(m.get("protein_g", 0)) for m in meals)
    total_carbs = sum(float(m.get("carbs_g", 0)) for m in meals)
    total_fats = sum(float(m.get("fats_g", 0)) for m in meals)
    total_calories = sum(float(m.get("calories", 0)) for m in meals)

    daily_summary = {
        "date": date,
        "total_protein_g": round(total_protein, 1),
        "total_carbs_g": round(total_carbs, 1),
        "total_fats_g": round(total_fats, 1),
        "total_calories": round(total_calories, 1),
        "meal_count": len(meals),
    }

    baseline = _rolling_baseline(date, days=7)
    baseline_deviation_alerts: List[str] = []
    severe_deviation = False
    deviation_threshold = 30.0
    if baseline:
        metrics = {
            "calories": total_calories,
            "protein_g": total_protein,
            "carbs_g": total_carbs,
            "fats_g": total_fats,
        }
        for metric, actual in metrics.items():
            b = float(baseline.get(metric) or 0.0)
            if b <= 0:
                continue
            delta_pct = ((actual - b) / b) * 100.0
            if abs(delta_pct) > deviation_threshold:
                baseline_deviation_alerts.append(
                    f"BASELINE DEVIATION: {metric} changed {delta_pct:+.1f}% vs rolling 7-day baseline ({actual:.1f} vs {b:.1f})."
                )
                if abs(delta_pct) >= 45.0:
                    severe_deviation = True
    alerts.extend(baseline_deviation_alerts)

    # --- 1. Protein adequacy ---
    protein_min = float(thr.get("protein_daily_min_g", 120))
    if meals and total_protein < protein_min:
        alerts.append(
            f"PROTEIN LOW: {total_protein:.0f}g tracked today "
            f"(minimum {protein_min:.0f}g for performance). "
            f"Add ~{protein_min - total_protein:.0f}g more."
        )

    # --- 2. Carbohydrate availability vs workout output ---
    bio = get_biometrics(date)
    if bio and meals:
        workout_kcal = float(bio.get("workout_kcal") or 0)
        carb_threshold = float(thr.get("carb_refuel_threshold_g", 150))
        if workout_kcal > 400 and total_carbs < carb_threshold:
            alerts.append(
                f"CARB DEFICIT: Only {total_carbs:.0f}g carbs after {workout_kcal:.0f} kcal workout. "
                f"Target ≥{carb_threshold:.0f}g on training days."
            )

    # --- 3. Calorie floor ---
    calorie_floor = float(thr.get("calorie_floor_kcal", 1800))
    if meals and total_calories < calorie_floor:
        alerts.append(
            f"CALORIE FLOOR BREACH: {total_calories:.0f} kcal today "
            f"(floor {calorie_floor:.0f} kcal). Risk of CNS fatigue if repeated."
        )

    # --- 4. High-fat late meal → sleep impact ---
    high_fat_g = float(thr.get("high_fat_pre_sleep_threshold_g", 25))
    late_hour = int(thr.get("late_meal_cutoff_hour", 21))
    sleep_cycles = get_sleep_cycles(date)

    for meal in meals:
        fats = float(meal.get("fats_g", 0))
        meal_ts = meal.get("meal_ts", "")
        if not meal_ts:
            continue
        try:
            meal_hour = int(meal_ts[11:13])
        except (IndexError, ValueError):
            continue

        if fats >= high_fat_g and meal_hour >= late_hour:
            corr: Dict[str, Any] = {
                "meal_ts": meal_ts,
                "description": meal.get("description", ""),
                "fats_g": fats,
                "meal_hour": meal_hour,
                "finding": f"High-fat meal ({fats:.0f}g fat) at {meal_hour:02d}:xx may impair REM sleep",
            }

            # Check if next sleep performance was poor
            if sleep_cycles:
                sc = sleep_cycles[0]
                perf = sc.get("performance_pct")
                if perf and float(perf) < float(thr.get("poor_sleep_correlation_pct", 65)):
                    corr["sleep_performance_pct"] = perf
                    corr["finding"] += f". Next-night sleep was {perf:.0f}% (below {thr.get('poor_sleep_correlation_pct', 65)}% threshold)."
                    alerts.append(
                        f"MEAL-SLEEP CORRELATION: {fats:.0f}g fat at {meal_hour:02d}:xx → "
                        f"sleep performance {perf:.0f}%. Consider lighter fat intake after {late_hour}:00."
                    )
            correlations.append(corr)

    # --- 5. Historical pattern: high-fat-before-sleep → poor REM (14-day) ---
    historical = get_meal_sleep_correlations(days_back=14)
    late_fat_sleeps = [
        h for h in historical
        if h.get("window_bucket") in ("<2h before sleep", "2-4h before sleep")
        and float(h.get("fats_g", 0)) >= high_fat_g
        and h.get("performance_pct") is not None
    ]

    if len(late_fat_sleeps) >= 3:
        avg_perf = sum(float(h["performance_pct"]) for h in late_fat_sleeps) / len(late_fat_sleeps)
        poor_threshold = float(thr.get("poor_sleep_correlation_pct", 65))
        if avg_perf < poor_threshold:
            correlations.append({
                "finding": (
                    f"14-day pattern: high-fat meals within 4h of sleep averaged "
                    f"{avg_perf:.0f}% sleep performance (n={len(late_fat_sleeps)}). "
                    f"Threshold: {poor_threshold:.0f}%."
                ),
                "n": len(late_fat_sleeps),
                "avg_sleep_performance_pct": round(avg_perf, 1),
            })

    goal_contradictions = _goal_contradiction_flags(total_calories, total_carbs, total_protein)
    alerts.extend(goal_contradictions)
    if goal_contradictions and len(goal_contradictions) >= 2:
        severe_deviation = True

    return {
        "date": date,
        "agent_id": "stan",
        "role": "nutritionist",
        "alerts": alerts,
        "flags_count": len(alerts),
        "notify_joy": severe_deviation,
        "baseline": baseline or None,
        "correlations": correlations,
        "daily_summary": daily_summary,
    }


def trend_report(days: int = 14, end_date: Optional[str] = None) -> Dict[str, Any]:
    """Multi-day nutritional trend with rolling averages."""
    end = end_date or _today()
    analyses = []
    for i in range(days - 1, -1, -1):
        d = _date_n_ago(i, end)
        try:
            analyses.append(analyze_nutrient_timing(d))
        except Exception as exc:
            analyses.append({"date": d, "error": str(exc)})

    total_alerts = sum(a.get("flags_count", 0) for a in analyses)
    avg_protein = [a["daily_summary"]["total_protein_g"] for a in analyses if "daily_summary" in a]
    avg_cals = [a["daily_summary"]["total_calories"] for a in analyses if "daily_summary" in a]

    def _safe_avg(vals: List[float]) -> float:
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    return {
        "period_days": days,
        "end_date": end,
        "total_alerts": total_alerts,
        "avg_protein_g": _safe_avg(avg_protein),
        "avg_calories": _safe_avg(avg_cals),
        "analyses": analyses,
    }


def cmd_analyze(args: argparse.Namespace) -> int:
    result = analyze_nutrient_timing(getattr(args, "date", None))
    if args.emit_result:
        path = write_agent_result("stan", result)
        result["result_path"] = str(path)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    result = trend_report(days=args.days)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Nutritionist — nutrient timing analysis")
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="Analyze a single date")
    p_analyze.add_argument("--date", default=None)
    p_analyze.add_argument("--emit-result", action="store_true")
    p_analyze.set_defaults(func=cmd_analyze)

    p_report = sub.add_parser("report", help="Multi-day trend")
    p_report.add_argument("--days", type=int, default=14)
    p_report.set_defaults(func=cmd_report)

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
