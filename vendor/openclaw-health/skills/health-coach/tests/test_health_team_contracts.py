from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import health_coach
import nurse_engine
import nutritionist_engine
import result_store
import trainer_engine


class HealthTeamContractTests(unittest.TestCase):
    def test_stan_flags_baseline_deviation_over_30pct(self) -> None:
        fake_meals = [
            {
                "protein_g": 60,
                "carbs_g": 80,
                "fats_g": 30,
                "calories": 2200,
                "meal_ts": "2026-03-31T12:00:00Z",
                "description": "test",
            }
        ]
        with (
            patch("health_db.get_meals", return_value=fake_meals),
            patch("health_db.get_biometrics", return_value={"workout_kcal": 500}),
            patch("health_db.get_sleep_cycles", return_value=[]),
            patch("health_db.get_meal_sleep_correlations", return_value=[]),
            patch("health_db.get_meals_range", return_value=fake_meals * 7),
            patch("health_db.get_all_proposals", return_value=[]),
            patch.object(nutritionist_engine, "_rolling_baseline", return_value={"calories": 1400.0, "protein_g": 100.0, "carbs_g": 100.0, "fats_g": 40.0}),
        ):
            out = nutritionist_engine.analyze_nutrient_timing("2026-03-31")

        alert_text = " ".join(out["alerts"]).lower()
        self.assertIn("baseline deviation", alert_text)
        self.assertGreater(out["flags_count"], 0)

    def test_joy_hard_threshold_rhr_and_sleep(self) -> None:
        bio_range = [
            {"sample_date": "2026-03-25", "resting_hr": 50, "sleep_hours": 7.0, "hrv_rmssd_milli": 70},
            {"sample_date": "2026-03-26", "resting_hr": 51, "sleep_hours": 7.1, "hrv_rmssd_milli": 71},
            {"sample_date": "2026-03-30", "resting_hr": 63, "sleep_hours": 4.7, "hrv_rmssd_milli": 60},
            {"sample_date": "2026-03-31", "resting_hr": 64, "sleep_hours": 4.6, "hrv_rmssd_milli": 59},
        ]
        with (
            patch("health_db.get_biometrics", return_value=bio_range[-1]),
            patch("health_db.get_biometrics_range", return_value=bio_range),
            patch("health_db.get_allostatic_score", return_value=None),
            patch("health_db.get_meals_range", return_value=[]),
            patch("health_db.get_active_modifiers", return_value=[]),
        ):
            out = nurse_engine.calculate_injury_risk("2026-03-31")

        reasons = " ".join(out["reasons"]).lower()
        self.assertIn("hard threshold", reasons)
        self.assertIn("sleep duration out of bounds", reasons)

    def test_popeye_classifies_multi_domain_message(self) -> None:
        targets = health_coach._classify_targets("I feel unwell and need a workout and meal plan")
        self.assertIn("joy", targets)
        self.assertIn("dick", targets)
        self.assertIn("stan", targets)

    def test_trainer_generates_plan(self) -> None:
        plan = trainer_engine.build_training_plan(
            "Need a bike session",
            activity={"readiness_score": 72, "sleep_hours": 7.4, "workout_kcal": 100},
            date_str="2026-03-31",
        )
        self.assertEqual(plan["modality"], "bike")
        self.assertEqual(plan["intensity"], "high")
        self.assertGreater(plan["duration_minutes"], 30)

    def test_result_store_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.object(result_store, "RESULTS_DIR", tmp_path):
                written = result_store.write_agent_result("joy", {"alert": "test"}, ts=1743432000)
                self.assertTrue(written.exists())
                records = result_store.list_agent_results(agent_id="joy")
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["agent_id"], "joy")


if __name__ == "__main__":
    unittest.main()
