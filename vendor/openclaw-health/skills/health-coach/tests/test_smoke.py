from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from health_store import load_store, set_push_marker, get_push_state
from models import DailySnapshot, MacroDay, ScheduleDay, WhoopDay


class SmokeTests(unittest.TestCase):
    def test_push_marker_roundtrip(self) -> None:
        set_push_marker("test_marker", "ok")
        state = get_push_state()
        self.assertEqual(state.get("test_marker"), "ok")

    def test_snapshot_dataclass_roundtrip(self) -> None:
        snap = DailySnapshot(
            date="2026-03-23",
            macros=MacroDay(date="2026-03-23", calories=2000),
            whoop=WhoopDay(date="2026-03-23", workout_kcal=350),
            schedule=ScheduleDay(date="2026-03-23", schedule_load_score=42),
        )
        as_dict = snap.to_dict()
        rebuilt = DailySnapshot.from_dict(as_dict)
        self.assertEqual(rebuilt.date, snap.date)
        self.assertEqual(rebuilt.whoop.workout_kcal, 350)
        self.assertEqual(rebuilt.schedule.schedule_load_score, 42)

    def test_store_exists(self) -> None:
        store = load_store()
        self.assertIn("snapshots", store)
        self.assertIn("push_state", store)


if __name__ == "__main__":
    unittest.main()
