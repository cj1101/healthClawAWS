#!/usr/bin/env python3
"""
Nurse Monitor — Predictive injury/overtraining risk engine.

Role contract:
  - SILENT WATCHER. This module never sends messages to the user.
  - Returns a structured risk payload that the Manager may act on.
  - Inputs: biometric_samples, allostatic_scores, qualitative_modifiers (SQLite).
  - Output: { risk_score, category, reasons[], recommended_action }

CLI:
    python nurse_engine.py assess               # assess today
    python nurse_engine.py assess --date 2026-03-20
    python nurse_engine.py report --days 7      # trend report
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


def calculate_injury_risk(date_str: Optional[str] = None) -> Dict[str, Any]:
    """
    Compute injury/overtraining risk for the given date.

    Returns a dict with:
        risk_score     float 0-10
        category       "low" | "moderate" | "high" | "critical"
        reasons        list[str]
        recommended_action  str
        data_confidence "high" | "medium" | "low"
    """
    from health_db import get_active_modifiers, get_allostatic_range, get_biometrics, get_biometrics_range

    thr = _load_thresholds().get("injury_risk", {})
    date = date_str or _today()
    reasons: List[str] = []
    score = 0.0

    # --- 1. WHOOP biometrics today ---
    bio = get_biometrics(date)
    data_points = 0

    if bio:
        data_points += 1
        rec = bio.get("recovery_score")
        hrv = bio.get("hrv_rmssd_milli")
        strain = bio.get("avg_strain")

        if rec is not None:
            rec = float(rec)
            if rec < float(thr.get("recovery_score_critical", 25)):
                score += 3.0
                reasons.append(f"WHOOP recovery critically low ({rec:.0f}%)")
            elif rec < float(thr.get("recovery_score_low", 35)):
                score += 1.5
                reasons.append(f"WHOOP recovery low ({rec:.0f}%)")

        if strain is not None:
            strain = float(strain)
            if strain >= float(thr.get("strain_critical", 19)):
                score += 2.5
                reasons.append(f"Strain critically high ({strain:.1f}/21)")
            elif strain >= float(thr.get("strain_high", 16)):
                score += 1.5
                reasons.append(f"Strain high ({strain:.1f}/21)")

    # --- 2. HRV trend: check for large drop vs 7-day average ---
    start_7d = _date_n_ago(7, date)
    bio_range = get_biometrics_range(start_7d, date)
    if len(bio_range) >= 3:
        data_points += 1
        hrv_vals = [r["hrv_rmssd_milli"] for r in bio_range if r.get("hrv_rmssd_milli")]
        if hrv_vals and bio and bio.get("hrv_rmssd_milli"):
            avg_hrv = sum(hrv_vals[:-1]) / max(len(hrv_vals) - 1, 1)
            today_hrv = float(bio["hrv_rmssd_milli"])
            drop_pct = ((avg_hrv - today_hrv) / max(avg_hrv, 1)) * 100
            drop_threshold = float(thr.get("hrv_drop_pct", 20))
            if drop_pct >= drop_threshold:
                score += 1.5
                reasons.append(f"HRV dropped {drop_pct:.0f}% vs 7-day average (threshold {drop_threshold}%)")

    # --- 3. Consecutive poor sleep days ---
    poor_sleep_thresh = float(thr.get("poor_sleep_threshold_hours", 6.0))
    consecutive_limit = int(thr.get("consecutive_poor_sleep_days", 3))
    if bio_range:
        data_points += 1
        poor_streak = 0
        for r in sorted(bio_range, key=lambda x: x["sample_date"], reverse=True):
            sh = r.get("sleep_hours")
            if sh and float(sh) < poor_sleep_thresh:
                poor_streak += 1
            else:
                break
        if poor_streak >= consecutive_limit:
            score += 2.0
            reasons.append(f"{poor_streak} consecutive nights with <{poor_sleep_thresh}h sleep")

    # --- 3b. Hard threshold trigger: sleep <5h OR >11h ---
    hard_sleep_low = float(thr.get("hard_sleep_low_hours", 5.0))
    hard_sleep_high = float(thr.get("hard_sleep_high_hours", 11.0))
    if bio and bio.get("sleep_hours") is not None:
        today_sleep = float(bio["sleep_hours"])
        if today_sleep < hard_sleep_low or today_sleep > hard_sleep_high:
            score += 2.5
            reasons.append(
                f"Hard threshold: sleep duration out of bounds ({today_sleep:.1f}h not in [{hard_sleep_low:.1f}, {hard_sleep_high:.1f}]h)."
            )

    # --- 3c. Hard threshold trigger: RHR spikes >10 bpm for >=2 days ---
    rhr_spike_threshold = float(thr.get("hard_rhr_spike_bpm", 10.0))
    recent_bio = bio_range[-2:] if len(bio_range) >= 2 else []
    if len(recent_bio) == 2:
        baseline_rows = bio_range[:-2]
        baseline_rhrs = [float(r["resting_hr"]) for r in baseline_rows if r.get("resting_hr") is not None]
        if baseline_rhrs and all(r.get("resting_hr") is not None for r in recent_bio):
            baseline_rhr = sum(baseline_rhrs) / len(baseline_rhrs)
            recent_spikes = [
                float(r["resting_hr"]) - baseline_rhr > rhr_spike_threshold for r in recent_bio
            ]
            if all(recent_spikes):
                score += 3.0
                reasons.append(
                    f"Hard threshold: resting HR >{rhr_spike_threshold:.0f} bpm above baseline for 2 consecutive days."
                )

    # --- 4. Allostatic load ---
    from health_db import get_allostatic_score
    allo = get_allostatic_score(date)
    if allo:
        data_points += 1
        allo_score = float(allo["score_1_10"])
        if allo_score >= float(thr.get("allostatic_load_critical", 8.5)):
            score += 2.5
            reasons.append(f"Allostatic load critical ({allo_score:.1f}/10)")
        elif allo_score >= float(thr.get("allostatic_load_high", 7.0)):
            score += 1.0
            reasons.append(f"Allostatic load high ({allo_score:.1f}/10)")

    # --- 5. Calorie deficit trend ---
    from health_db import get_meals_range
    meals_range = get_meals_range(start_7d, date)
    if meals_range:
        data_points += 1
        from collections import defaultdict
        daily_cals: Dict[str, float] = defaultdict(float)
        for m in meals_range:
            daily_cals[m["meal_date"]] += float(m.get("calories", 0))

        deficit_days = 0
        for d, kcal in daily_cals.items():
            if kcal > 0 and kcal < float(thr.get("calorie_floor_kcal", 1800)):
                deficit_days += 1

        calorie_deficit_days_limit = int(thr.get("calorie_deficit_days", 3))
        if deficit_days >= calorie_deficit_days_limit:
            score += 1.5
            reasons.append(
                f"{deficit_days} days in the last week with calories below "
                f"{thr.get('calorie_floor_kcal', 1800)} kcal"
            )

    # --- 6. Qualitative modifiers (injury/fatigue keywords) ---
    modifiers = get_active_modifiers(date, days_back=3)
    injury_mods = [m for m in modifiers if m["modifier_type"] == "injury_risk"]
    fatigue_mods = [m for m in modifiers if m["modifier_type"] == "fatigue"]
    if injury_mods:
        data_points += 1
        score += 2.5
        reasons.append(
            f"Active injury-risk notes: {'; '.join(m['note_text'][:60] for m in injury_mods[:2])}"
        )
    if fatigue_mods:
        score += 1.0
        reasons.append(
            f"Active fatigue notes: {'; '.join(m['note_text'][:60] for m in fatigue_mods[:2])}"
        )

    # --- Clamp and classify ---
    risk_score = min(round(score, 2), 10.0)
    thresholds = thr.get("risk_score_thresholds", {})
    if risk_score >= float(thresholds.get("high", 7.5)):
        category = "high"
        action = (
            "HIGH INJURY RISK — Manager should immediately propose a mandatory recovery goal. "
            "No high-intensity work until risk drops below moderate."
        )
    elif risk_score >= float(thresholds.get("moderate", 5.5)):
        category = "moderate"
        action = (
            "MODERATE RISK — Manager should flag this to the user at next check-in and "
            "propose a deload/recovery goal for consideration."
        )
    elif risk_score >= float(thresholds.get("low", 3.0)):
        category = "low"
        action = "LOW RISK — Monitor; no immediate action required."
    else:
        category = "minimal"
        action = "MINIMAL RISK — All systems green."

    confidence = "high" if data_points >= 4 else ("medium" if data_points >= 2 else "low")

    if not reasons:
        reasons = ["No risk factors detected."]

    return {
        "date": date,
        "agent_id": "joy",
        "role": "nurse",
        "risk_score": risk_score,
        "category": category,
        "reasons": reasons,
        "recommended_action": action,
        "data_confidence": confidence,
        "data_points_used": data_points,
    }


def trend_report(days: int = 7, end_date: Optional[str] = None) -> Dict[str, Any]:
    """Return a multi-day risk trend for summary view."""
    end = end_date or _today()
    assessments = []
    for i in range(days - 1, -1, -1):
        d = _date_n_ago(i, end)
        try:
            assessments.append(calculate_injury_risk(d))
        except Exception as exc:
            assessments.append({"date": d, "error": str(exc)})

    scores = [a["risk_score"] for a in assessments if "risk_score" in a]
    avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0
    peak = max(scores) if scores else 0.0

    return {
        "period_days": days,
        "end_date": end,
        "avg_risk_score": avg_score,
        "peak_risk_score": peak,
        "assessments": assessments,
    }


def cmd_assess(args: argparse.Namespace) -> int:
    result = calculate_injury_risk(getattr(args, "date", None))
    if args.emit_result:
        path = write_agent_result("joy", result)
        result["result_path"] = str(path)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    result = trend_report(days=args.days)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Nurse Monitor — injury risk engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p_assess = sub.add_parser("assess", help="Assess risk for a date")
    p_assess.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    p_assess.add_argument("--emit-result", action="store_true")
    p_assess.set_defaults(func=cmd_assess)

    p_report = sub.add_parser("report", help="Multi-day risk trend")
    p_report.add_argument("--days", type=int, default=7)
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
