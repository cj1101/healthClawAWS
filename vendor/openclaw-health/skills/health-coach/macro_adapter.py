from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from models import MacroDay

SKILL_DIR = Path(__file__).resolve().parent
MACRO_SKILL_DIR = SKILL_DIR.parent / "food-macro-tracker"
MACRO_STATE_FILE = MACRO_SKILL_DIR / "state.json"
MACRO_HISTORY_FILE = MACRO_SKILL_DIR / "nutrition_history.csv"


def _safe_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def get_macro_day(date_str: str) -> MacroDay:
    if MACRO_STATE_FILE.exists():
        try:
            payload = json.loads(MACRO_STATE_FILE.read_text(encoding="utf-8"))
            if payload.get("current_date") == date_str:
                totals = payload.get("daily_totals", {}) or {}
                entries = payload.get("entries", []) or []
                return MacroDay(
                    date=date_str,
                    calories=_safe_float(totals.get("calories")),
                    protein_g=_safe_float(totals.get("protein_g")),
                    carbs_g=_safe_float(totals.get("carbs_g")),
                    fats_g=_safe_float(totals.get("fats_g")),
                    fiber_g=_safe_float(totals.get("fiber_g")),
                    entry_count=len(entries),
                )
        except Exception:
            pass

    if MACRO_HISTORY_FILE.exists():
        try:
            with MACRO_HISTORY_FILE.open("r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    if row.get("date") == date_str:
                        return MacroDay(
                            date=date_str,
                            calories=_safe_float(row.get("calories")),
                            protein_g=_safe_float(row.get("protein_g")),
                            carbs_g=_safe_float(row.get("carbs_g")),
                            fats_g=_safe_float(row.get("fats_g")),
                            fiber_g=_safe_float(row.get("fiber_g")),
                            entry_count=int(_safe_float(row.get("entry_count"))),
                        )
        except Exception:
            pass

    return MacroDay(date=date_str)


def get_macro_history(limit_days: int = 14) -> List[MacroDay]:
    rows: List[MacroDay] = []
    if not MACRO_HISTORY_FILE.exists():
        return rows
    try:
        with MACRO_HISTORY_FILE.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows.append(
                    MacroDay(
                        date=row.get("date", ""),
                        calories=_safe_float(row.get("calories")),
                        protein_g=_safe_float(row.get("protein_g")),
                        carbs_g=_safe_float(row.get("carbs_g")),
                        fats_g=_safe_float(row.get("fats_g")),
                        fiber_g=_safe_float(row.get("fiber_g")),
                        entry_count=int(_safe_float(row.get("entry_count"))),
                    )
                )
    except Exception:
        return []

    rows.sort(key=lambda r: r.date, reverse=True)
    return rows[:limit_days]


def average_calories(days: int = 7) -> float:
    rows = get_macro_history(limit_days=days)
    if not rows:
        return 0.0
    return sum(r.calories for r in rows) / len(rows)
