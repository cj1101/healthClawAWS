from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MacroDay:
    date: str
    calories: float = 0.0
    protein_g: float = 0.0
    carbs_g: float = 0.0
    fats_g: float = 0.0
    fiber_g: float = 0.0
    entry_count: int = 0


@dataclass
class WhoopDay:
    date: str
    workout_kj: float = 0.0
    workout_kcal: float = 0.0
    workout_count: int = 0
    avg_strain: float = 0.0
    recovery_score: Optional[float] = None
    resting_hr: Optional[float] = None
    hrv_rmssd_milli: Optional[float] = None
    sleep_hours: Optional[float] = None
    sleep_performance_pct: Optional[float] = None
    body_weight_kg: Optional[float] = None
    body_height_m: Optional[float] = None
    max_heart_rate: Optional[int] = None


@dataclass
class ScheduleDay:
    date: str
    event_count: int = 0
    busy_hours: float = 0.0
    due_today_count: int = 0
    overdue_count: int = 0
    schedule_load_score: float = 0.0


@dataclass
class DailySnapshot:
    date: str
    macros: MacroDay
    whoop: WhoopDay
    schedule: ScheduleDay
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        return out

    @staticmethod
    def from_dict(payload: Dict[str, Any]) -> "DailySnapshot":
        return DailySnapshot(
            date=payload.get("date", ""),
            macros=MacroDay(**payload.get("macros", {})),
            whoop=WhoopDay(**payload.get("whoop", {})),
            schedule=ScheduleDay(**payload.get("schedule", {})),
            metadata=payload.get("metadata", {}) or {},
        )


@dataclass
class CoachingOutput:
    date: str
    readiness_score: float
    readiness_band: str
    training_intent: str
    recommended_modality: str
    nutrition_adjustment: str
    schedule_advice: str
    confidence: str
    rationale: List[str]
    fallback_option: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
