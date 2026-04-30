"""
SQLite foundation for the OpenClaw health platform.

Single authoritative store at skills/health-coach/data/health.db.
All tables are created by bootstrap_db() which is safe to call repeatedly
(migrations are additive-only; no data is dropped).
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

SKILL_DIR = Path(__file__).resolve().parent
DATA_DIR = SKILL_DIR / "data"
DB_PATH = DATA_DIR / "health.db"

# Schema version — increment when adding new tables/columns
SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS biometric_samples (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_date         TEXT    NOT NULL,
    ingest_ts           TEXT    NOT NULL,
    hrv_rmssd_milli     REAL,
    resting_hr          REAL,
    sleep_hours         REAL,
    sleep_performance_pct REAL,
    recovery_score      REAL,
    avg_strain          REAL,
    workout_kcal        REAL,
    workout_count       INTEGER,
    body_weight_kg      REAL,
    source              TEXT    DEFAULT 'whoop',
    UNIQUE(sample_date, source)
);

CREATE TABLE IF NOT EXISTS sleep_cycles (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    whoop_sleep_id   TEXT    UNIQUE,
    sleep_start_ts   TEXT    NOT NULL,
    sleep_end_ts     TEXT    NOT NULL,
    sleep_date       TEXT    NOT NULL,
    total_hours      REAL,
    rem_hours        REAL,
    deep_hours       REAL,
    light_hours      REAL,
    performance_pct  REAL,
    raw_json         TEXT
);

CREATE TABLE IF NOT EXISTS meals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_ts     TEXT    NOT NULL,
    meal_date   TEXT    NOT NULL,
    description TEXT,
    protein_g   REAL    NOT NULL DEFAULT 0,
    carbs_g     REAL    NOT NULL DEFAULT 0,
    fats_g      REAL    NOT NULL DEFAULT 0,
    fiber_g     REAL    NOT NULL DEFAULT 0,
    calories    REAL    NOT NULL DEFAULT 0,
    input_type  TEXT,
    source_ref  TEXT,
    created_at  TEXT    DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_meals_date ON meals(meal_date);
CREATE INDEX IF NOT EXISTS idx_meals_ts   ON meals(meal_ts);

CREATE TABLE IF NOT EXISTS meal_sleep_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id         INTEGER NOT NULL REFERENCES meals(id),
    sleep_cycle_id  INTEGER NOT NULL REFERENCES sleep_cycles(id),
    delta_minutes   INTEGER,
    window_bucket   TEXT,
    UNIQUE(meal_id, sleep_cycle_id)
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT,
    account     TEXT,
    title       TEXT,
    start_ts    TEXT,
    end_ts      TEXT,
    all_day     INTEGER DEFAULT 0,
    ingest_date TEXT,
    UNIQUE(event_id, account)
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT    UNIQUE,
    account     TEXT,
    title       TEXT,
    notes       TEXT,
    due_ts      TEXT,
    status      TEXT,
    ingest_date TEXT
);

CREATE TABLE IF NOT EXISTS allostatic_scores (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    score_date          TEXT    NOT NULL UNIQUE,
    score_1_10          REAL    NOT NULL,
    event_count         INTEGER DEFAULT 0,
    busy_hours          REAL    DEFAULT 0,
    due_today_count     INTEGER DEFAULT 0,
    overdue_count       INTEGER DEFAULT 0,
    schedule_raw_score  REAL    DEFAULT 0,
    components_json     TEXT,
    computed_at         TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS weather_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_ts        TEXT    NOT NULL,
    obs_date      TEXT    NOT NULL,
    location      TEXT    DEFAULT 'Fort Greene, Brooklyn',
    lat           REAL,
    lon           REAL,
    temp_c        REAL,
    feels_like_c  REAL,
    humidity_pct  REAL,
    condition     TEXT,
    wind_kph      REAL,
    raw_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_weather_date ON weather_samples(obs_date);

CREATE TABLE IF NOT EXISTS aqi_samples (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    obs_ts    TEXT    NOT NULL,
    obs_date  TEXT    NOT NULL,
    location  TEXT    DEFAULT 'Fort Greene, Brooklyn',
    aqi       INTEGER,
    pm25      REAL,
    pm10      REAL,
    category  TEXT,
    source    TEXT,
    raw_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_aqi_date ON aqi_samples(obs_date);

CREATE TABLE IF NOT EXISTS qualitative_modifiers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    note_ts       TEXT    NOT NULL,
    note_date     TEXT    NOT NULL,
    note_text     TEXT    NOT NULL,
    modifier_type TEXT,
    severity      TEXT    DEFAULT 'moderate',
    active        INTEGER DEFAULT 1,
    created_at    TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS goal_proposals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_ts       TEXT    NOT NULL,
    title             TEXT    NOT NULL,
    description       TEXT,
    rationale         TEXT,
    status            TEXT    NOT NULL DEFAULT 'pending',
    user_response     TEXT,
    modification_text TEXT,
    activated_at      TEXT,
    resolved_at       TEXT,
    created_at        TEXT    DEFAULT (datetime('now')),
    updated_at        TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS body_measurements (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    measurement_ts    TEXT    NOT NULL,
    measurement_date  TEXT    NOT NULL,
    height_meter      REAL,
    weight_kilogram   REAL,
    max_heart_rate    INTEGER,
    source            TEXT    DEFAULT 'whoop',
    raw_json          TEXT,
    ingest_ts         TEXT    NOT NULL,
    UNIQUE(measurement_date, source)
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def bootstrap_db() -> None:
    """Create DB file, run DDL, record schema version. Safe to call repeatedly."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(_DDL)
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()


@contextmanager
def db_conn() -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a connection with row_factory set."""
    bootstrap_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Biometrics
# ---------------------------------------------------------------------------

def upsert_biometric(
    sample_date: str,
    *,
    hrv_rmssd_milli: Optional[float] = None,
    resting_hr: Optional[float] = None,
    sleep_hours: Optional[float] = None,
    sleep_performance_pct: Optional[float] = None,
    recovery_score: Optional[float] = None,
    avg_strain: Optional[float] = None,
    workout_kcal: Optional[float] = None,
    workout_count: Optional[int] = None,
    body_weight_kg: Optional[float] = None,
    source: str = "whoop",
) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO biometric_samples
                (sample_date, ingest_ts, hrv_rmssd_milli, resting_hr, sleep_hours,
                 sleep_performance_pct, recovery_score, avg_strain, workout_kcal,
                 workout_count, body_weight_kg, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(sample_date, source) DO UPDATE SET
                ingest_ts             = excluded.ingest_ts,
                hrv_rmssd_milli       = COALESCE(excluded.hrv_rmssd_milli, hrv_rmssd_milli),
                resting_hr            = COALESCE(excluded.resting_hr, resting_hr),
                sleep_hours           = COALESCE(excluded.sleep_hours, sleep_hours),
                sleep_performance_pct = COALESCE(excluded.sleep_performance_pct, sleep_performance_pct),
                recovery_score        = COALESCE(excluded.recovery_score, recovery_score),
                avg_strain            = COALESCE(excluded.avg_strain, avg_strain),
                workout_kcal          = COALESCE(excluded.workout_kcal, workout_kcal),
                workout_count         = COALESCE(excluded.workout_count, workout_count),
                body_weight_kg        = COALESCE(excluded.body_weight_kg, body_weight_kg)
            """,
            (
                sample_date, _now_iso(), hrv_rmssd_milli, resting_hr, sleep_hours,
                sleep_performance_pct, recovery_score, avg_strain, workout_kcal,
                workout_count, body_weight_kg, source,
            ),
        )


def get_biometrics(date_str: str, source: str = "whoop") -> Optional[Dict[str, Any]]:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM biometric_samples WHERE sample_date=? AND source=?",
            (date_str, source),
        ).fetchone()
        return dict(row) if row else None


def get_biometrics_range(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM biometric_samples WHERE sample_date>=? AND sample_date<=? ORDER BY sample_date ASC",
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Sleep cycles
# ---------------------------------------------------------------------------

def upsert_sleep_cycle(
    whoop_sleep_id: Optional[str],
    sleep_start_ts: str,
    sleep_end_ts: str,
    sleep_date: str,
    *,
    total_hours: Optional[float] = None,
    rem_hours: Optional[float] = None,
    deep_hours: Optional[float] = None,
    light_hours: Optional[float] = None,
    performance_pct: Optional[float] = None,
    raw_json: Optional[str] = None,
) -> int:
    with db_conn() as conn:
        if whoop_sleep_id:
            conn.execute(
                """
                INSERT INTO sleep_cycles
                    (whoop_sleep_id, sleep_start_ts, sleep_end_ts, sleep_date,
                     total_hours, rem_hours, deep_hours, light_hours, performance_pct, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(whoop_sleep_id) DO UPDATE SET
                    sleep_start_ts  = excluded.sleep_start_ts,
                    sleep_end_ts    = excluded.sleep_end_ts,
                    sleep_date      = excluded.sleep_date,
                    total_hours     = COALESCE(excluded.total_hours, total_hours),
                    rem_hours       = COALESCE(excluded.rem_hours, rem_hours),
                    deep_hours      = COALESCE(excluded.deep_hours, deep_hours),
                    light_hours     = COALESCE(excluded.light_hours, light_hours),
                    performance_pct = COALESCE(excluded.performance_pct, performance_pct),
                    raw_json        = COALESCE(excluded.raw_json, raw_json)
                """,
                (whoop_sleep_id, sleep_start_ts, sleep_end_ts, sleep_date,
                 total_hours, rem_hours, deep_hours, light_hours, performance_pct, raw_json),
            )
            row = conn.execute(
                "SELECT id FROM sleep_cycles WHERE whoop_sleep_id=?", (whoop_sleep_id,)
            ).fetchone()
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO sleep_cycles
                    (sleep_start_ts, sleep_end_ts, sleep_date, total_hours,
                     rem_hours, deep_hours, light_hours, performance_pct, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (sleep_start_ts, sleep_end_ts, sleep_date, total_hours,
                 rem_hours, deep_hours, light_hours, performance_pct, raw_json),
            )
            row = conn.execute(
                "SELECT id FROM sleep_cycles WHERE sleep_start_ts=?", (sleep_start_ts,)
            ).fetchone()
        return int(row["id"]) if row else -1


def get_sleep_cycles(date_str: str) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sleep_cycles WHERE sleep_date=? ORDER BY sleep_start_ts ASC",
            (date_str,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Meals
# ---------------------------------------------------------------------------

def insert_meal(
    meal_ts: str,
    meal_date: str,
    *,
    description: Optional[str] = None,
    protein_g: float = 0,
    carbs_g: float = 0,
    fats_g: float = 0,
    fiber_g: float = 0,
    calories: float = 0,
    input_type: Optional[str] = None,
    source_ref: Optional[str] = None,
) -> int:
    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO meals
                (meal_ts, meal_date, description, protein_g, carbs_g, fats_g,
                 fiber_g, calories, input_type, source_ref)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (meal_ts, meal_date, description, protein_g, carbs_g, fats_g,
             fiber_g, calories, input_type, source_ref),
        )
        if cur.lastrowid and cur.lastrowid > 0:
            return cur.lastrowid
        row = conn.execute(
            "SELECT id FROM meals WHERE meal_ts=? AND description=?",
            (meal_ts, description),
        ).fetchone()
        return int(row["id"]) if row else -1


def get_meals(date_str: str) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM meals WHERE meal_date=? ORDER BY meal_ts ASC",
            (date_str,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_meals_range(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM meals WHERE meal_date>=? AND meal_date<=? ORDER BY meal_ts ASC",
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Meal-sleep links
# ---------------------------------------------------------------------------

def link_meal_to_sleep(meal_id: int, sleep_cycle_id: int, delta_minutes: int, window_bucket: str) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO meal_sleep_links(meal_id, sleep_cycle_id, delta_minutes, window_bucket)
            VALUES (?,?,?,?)
            """,
            (meal_id, sleep_cycle_id, delta_minutes, window_bucket),
        )


def get_meal_sleep_correlations(days_back: int = 14) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT m.meal_ts, m.meal_date, m.description, m.fats_g, m.calories,
                   s.sleep_date, s.total_hours, s.performance_pct,
                   l.delta_minutes, l.window_bucket
            FROM meal_sleep_links l
            JOIN meals m ON m.id = l.meal_id
            JOIN sleep_cycles s ON s.id = l.sleep_cycle_id
            WHERE date(m.meal_date) >= date('now', ?)
            ORDER BY m.meal_ts DESC
            """,
            (f"-{days_back} days",),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Allostatic scores
# ---------------------------------------------------------------------------

def upsert_allostatic_score(
    score_date: str,
    score_1_10: float,
    *,
    event_count: int = 0,
    busy_hours: float = 0,
    due_today_count: int = 0,
    overdue_count: int = 0,
    schedule_raw_score: float = 0,
    components: Optional[Dict[str, Any]] = None,
) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO allostatic_scores
                (score_date, score_1_10, event_count, busy_hours, due_today_count,
                 overdue_count, schedule_raw_score, components_json, computed_at)
            VALUES (?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(score_date) DO UPDATE SET
                score_1_10          = excluded.score_1_10,
                event_count         = excluded.event_count,
                busy_hours          = excluded.busy_hours,
                due_today_count     = excluded.due_today_count,
                overdue_count       = excluded.overdue_count,
                schedule_raw_score  = excluded.schedule_raw_score,
                components_json     = excluded.components_json,
                computed_at         = excluded.computed_at
            """,
            (
                score_date, score_1_10, event_count, busy_hours, due_today_count,
                overdue_count, schedule_raw_score,
                json.dumps(components) if components else None,
            ),
        )


def get_allostatic_score(date_str: str) -> Optional[Dict[str, Any]]:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM allostatic_scores WHERE score_date=?", (date_str,)
        ).fetchone()
        return dict(row) if row else None


def get_allostatic_range(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM allostatic_scores WHERE score_date>=? AND score_date<=? ORDER BY score_date ASC",
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Weather & AQI
# ---------------------------------------------------------------------------

def insert_weather(
    obs_ts: str,
    obs_date: str,
    *,
    location: str = "Fort Greene, Brooklyn",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    temp_c: Optional[float] = None,
    feels_like_c: Optional[float] = None,
    humidity_pct: Optional[float] = None,
    condition: Optional[str] = None,
    wind_kph: Optional[float] = None,
    raw_json: Optional[str] = None,
) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO weather_samples
                (obs_ts, obs_date, location, lat, lon, temp_c, feels_like_c,
                 humidity_pct, condition, wind_kph, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (obs_ts, obs_date, location, lat, lon, temp_c, feels_like_c,
             humidity_pct, condition, wind_kph, raw_json),
        )


def get_latest_weather(date_str: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with db_conn() as conn:
        if date_str:
            row = conn.execute(
                "SELECT * FROM weather_samples WHERE obs_date=? ORDER BY obs_ts DESC LIMIT 1",
                (date_str,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM weather_samples ORDER BY obs_ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None


def insert_aqi(
    obs_ts: str,
    obs_date: str,
    *,
    location: str = "Fort Greene, Brooklyn",
    aqi: Optional[int] = None,
    pm25: Optional[float] = None,
    pm10: Optional[float] = None,
    category: Optional[str] = None,
    source: Optional[str] = None,
    raw_json: Optional[str] = None,
) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO aqi_samples(obs_ts, obs_date, location, aqi, pm25, pm10, category, source, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (obs_ts, obs_date, location, aqi, pm25, pm10, category, source, raw_json),
        )


def get_latest_aqi(date_str: Optional[str] = None) -> Optional[Dict[str, Any]]:
    with db_conn() as conn:
        if date_str:
            row = conn.execute(
                "SELECT * FROM aqi_samples WHERE obs_date=? ORDER BY obs_ts DESC LIMIT 1",
                (date_str,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM aqi_samples ORDER BY obs_ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Qualitative modifiers
# ---------------------------------------------------------------------------

def insert_qualitative_modifier(
    note_text: str,
    modifier_type: str,
    severity: str = "moderate",
    note_ts: Optional[str] = None,
) -> int:
    ts = note_ts or _now_iso()
    note_date = ts[:10]
    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO qualitative_modifiers(note_ts, note_date, note_text, modifier_type, severity)
            VALUES (?,?,?,?,?)
            """,
            (ts, note_date, note_text, modifier_type, severity),
        )
        return cur.lastrowid or -1


def get_active_modifiers(date_str: Optional[str] = None, days_back: int = 3) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        if date_str:
            rows = conn.execute(
                """
                SELECT * FROM qualitative_modifiers
                WHERE active=1 AND date(note_date) >= date(?, ?)
                ORDER BY note_ts DESC
                """,
                (date_str, f"-{days_back} days"),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM qualitative_modifiers
                WHERE active=1 AND date(note_date) >= date('now', ?)
                ORDER BY note_ts DESC
                """,
                (f"-{days_back} days",),
            ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Goal proposals
# ---------------------------------------------------------------------------

def insert_goal_proposal(
    title: str,
    description: str,
    rationale: str,
) -> int:
    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO goal_proposals(proposal_ts, title, description, rationale, status)
            VALUES (?,?,?,?,'pending')
            """,
            (_now_iso(), title, description, rationale),
        )
        return cur.lastrowid or -1


def update_goal_proposal_status(
    proposal_id: int,
    status: str,
    *,
    user_response: Optional[str] = None,
    modification_text: Optional[str] = None,
) -> None:
    now = _now_iso()
    activated_at = now if status == "approved" else None
    resolved_at = now if status in ("rejected", "approved") else None
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE goal_proposals
            SET status=?, user_response=?, modification_text=?,
                activated_at=COALESCE(?, activated_at),
                resolved_at=COALESCE(?, resolved_at),
                updated_at=?
            WHERE id=?
            """,
            (status, user_response, modification_text, activated_at, resolved_at, now, proposal_id),
        )


def get_pending_proposals() -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM goal_proposals WHERE status='pending' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_proposals(limit: int = 20) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM goal_proposals ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_proposal(proposal_id: int) -> Optional[Dict[str, Any]]:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM goal_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Body measurements (Whoop / scale / manual)
# ---------------------------------------------------------------------------

def upsert_body_measurement(
    measurement_date: str,
    measurement_ts: str,
    *,
    height_meter: Optional[float] = None,
    weight_kilogram: Optional[float] = None,
    max_heart_rate: Optional[int] = None,
    source: str = "whoop",
    raw_json: Optional[str] = None,
) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO body_measurements
                (measurement_ts, measurement_date, height_meter, weight_kilogram,
                 max_heart_rate, source, raw_json, ingest_ts)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(measurement_date, source) DO UPDATE SET
                measurement_ts    = excluded.measurement_ts,
                height_meter      = COALESCE(excluded.height_meter, height_meter),
                weight_kilogram   = COALESCE(excluded.weight_kilogram, weight_kilogram),
                max_heart_rate    = COALESCE(excluded.max_heart_rate, max_heart_rate),
                raw_json          = COALESCE(excluded.raw_json, raw_json),
                ingest_ts         = excluded.ingest_ts
            """,
            (
                measurement_ts,
                measurement_date,
                height_meter,
                weight_kilogram,
                max_heart_rate,
                source,
                raw_json,
                _now_iso(),
            ),
        )


def get_body_measurements_range(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM body_measurements
            WHERE measurement_date >= ? AND measurement_date <= ?
            ORDER BY measurement_date ASC
            """,
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]


def get_latest_body_measurement(source: str = "whoop") -> Optional[Dict[str, Any]]:
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM body_measurements
            WHERE source = ?
            ORDER BY measurement_date DESC
            LIMIT 1
            """,
            (source,),
        ).fetchone()
        return dict(row) if row else None
