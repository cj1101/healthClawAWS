from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from coaching_engine import build_coaching_output
from models import DailySnapshot, MacroDay, ScheduleDay, WhoopDay


class CoachingEngineTests(unittest.TestCase):
    def test_green_day_push_intent(self) -> None:
        snap = DailySnapshot(
            date="2026-03-23",
            macros=MacroDay(date="2026-03-23", calories=2800, protein_g=180, carbs_g=300, fats_g=80, entry_count=4),
            whoop=WhoopDay(
                date="2026-03-23",
                recovery_score=82,
                sleep_hours=8.1,
                workout_kcal=700,
                workout_count=1,
            ),
            schedule=ScheduleDay(date="2026-03-23", schedule_load_score=20, event_count=2, busy_hours=2.5),
        )
        out = build_coaching_output(snap)
        self.assertGreaterEqual(out.readiness_score, 67)
        self.assertEqual(out.training_intent, "push")
        self.assertEqual(out.readiness_band, "green")

    def test_red_day_recovery_intent(self) -> None:
        snap = DailySnapshot(
            date="2026-03-23",
            macros=MacroDay(date="2026-03-23", calories=1400, protein_g=80, carbs_g=60, fats_g=35, entry_count=1),
            whoop=WhoopDay(
                date="2026-03-23",
                recovery_score=22,
                sleep_hours=5.3,
                workout_kcal=600,
                workout_count=1,
            ),
            schedule=ScheduleDay(date="2026-03-23", schedule_load_score=80, event_count=8, busy_hours=9.0),
        )
        out = build_coaching_output(snap)
        self.assertLess(out.readiness_score, 34)
        self.assertEqual(out.training_intent, "recover")
        self.assertEqual(out.readiness_band, "red")


if __name__ == "__main__":
    unittest.main()
