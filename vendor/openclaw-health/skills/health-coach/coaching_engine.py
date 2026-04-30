from __future__ import annotations

from typing import List, Tuple

from models import CoachingOutput, DailySnapshot


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _readiness(snapshot: DailySnapshot) -> Tuple[float, List[str]]:
    reasons: List[str] = []
    score = 50.0

    if snapshot.whoop.recovery_score is not None:
        rec = float(snapshot.whoop.recovery_score)
        score += (rec - 50.0) * 0.5
        reasons.append(f"WHOOP recovery {rec:.0f}%")

    if snapshot.whoop.sleep_hours is not None:
        sleep_hours = float(snapshot.whoop.sleep_hours)
        if sleep_hours >= 7.5:
            score += 10
        elif sleep_hours < 6.0:
            score -= 12
        reasons.append(f"sleep {sleep_hours:.1f}h")

    # Heavy schedule lowers practical readiness for hard training.
    load = float(snapshot.schedule.schedule_load_score)
    score -= (load / 100.0) * 15.0
    reasons.append(f"schedule load {load:.0f}/100")

    # Macro adequacy heuristics
    protein = snapshot.macros.protein_g
    carbs = snapshot.macros.carbs_g
    if protein < 120:
        score -= 5
        reasons.append("protein low for performance/body comp")
    if carbs < 150 and snapshot.whoop.workout_kcal > 400:
        score -= 7
        reasons.append("carbs low relative to output")

    return _clamp(round(score, 1), 0, 100), reasons


def _band(score: float) -> str:
    if score >= 67:
        return "green"
    if score >= 34:
        return "yellow"
    return "red"


def _training_intent(score: float) -> str:
    if score >= 67:
        return "push"
    if score >= 34:
        return "maintain"
    return "recover"


def _modality(intent: str, snapshot: DailySnapshot) -> str:
    if intent == "push":
        if snapshot.schedule.schedule_load_score > 65:
            return "Bike intervals (short) + brief climbing technique session"
        return "Primary performance day: hard climbing or bike intervals"
    if intent == "maintain":
        return "Moderate climbing technique or zone-2 bike + yoga mobility"
    return "Recovery-focused yoga, mobility, walk, and easy zone-1 spin"


def _nutrition_adjustment(intent: str, snapshot: DailySnapshot) -> str:
    if intent == "push":
        return "Keep protein high and add pre/post workout carbs (+40-90g total)."
    if intent == "maintain":
        return "Hold calories near baseline; prioritize protein and distribute carbs around sessions."
    if snapshot.macros.calories < 1800:
        return "Refuel today: increase calories and carbs, keep protein high."
    return "Emphasize recovery nutrition, hydration, and consistent protein."


def _schedule_advice(snapshot: DailySnapshot) -> str:
    if snapshot.schedule.schedule_load_score >= 70:
        return "Use a 20-30 minute minimum-effective workout block due to high calendar/task load."
    if snapshot.schedule.schedule_load_score >= 45:
        return "Use a 40 minute focused session and avoid overlong workouts."
    return "You have room for a 60+ minute structured session."


def _confidence(snapshot: DailySnapshot) -> str:
    missing = 0
    if snapshot.whoop.recovery_score is None:
        missing += 1
    if snapshot.whoop.sleep_hours is None:
        missing += 1
    if snapshot.macros.entry_count == 0:
        missing += 1
    if missing == 0:
        return "high"
    if missing == 1:
        return "medium"
    return "low"


def build_coaching_output(snapshot: DailySnapshot) -> CoachingOutput:
    readiness, rationale = _readiness(snapshot)
    band = _band(readiness)
    intent = _training_intent(readiness)
    modality = _modality(intent, snapshot)
    nutrition = _nutrition_adjustment(intent, snapshot)
    schedule = _schedule_advice(snapshot)
    confidence = _confidence(snapshot)

    fallback = "If energy drops, swap to 20 minutes yoga + breathing + easy walk."
    return CoachingOutput(
        date=snapshot.date,
        readiness_score=readiness,
        readiness_band=band,
        training_intent=intent,
        recommended_modality=modality,
        nutrition_adjustment=nutrition,
        schedule_advice=schedule,
        confidence=confidence,
        rationale=rationale,
        fallback_option=fallback,
    )
