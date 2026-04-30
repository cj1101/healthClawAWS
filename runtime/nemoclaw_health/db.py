from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Iterable

from nemoclaw_health.settings import Settings

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS user_profile (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  body_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS goals (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  body_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS tracking_registry (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  schema_hint_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS raw_events (
  id TEXT PRIMARY KEY,
  occurred_at TEXT NOT NULL,
  source TEXT NOT NULL,
  event_type TEXT NOT NULL,
  domain_slug TEXT,
  payload_json TEXT NOT NULL,
  confidence REAL,
  provenance_json TEXT
);

CREATE TABLE IF NOT EXISTS derived_summaries (
  id TEXT PRIMARY KEY,
  summary_type TEXT NOT NULL,
  body_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  source_agent TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS delegation_events (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
  event_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_states (
  connector_id TEXT PRIMARY KEY,
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS manual_edits (
  id TEXT PRIMARY KEY,
  target_table TEXT NOT NULL,
  target_row_id TEXT NOT NULL,
  patch_json TEXT NOT NULL,
  edited_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS disclaimer_audit (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  tier TEXT NOT NULL,
  templates_applied_json TEXT NOT NULL,
  recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Database:
    """Thread-local SQLite wrapper for Phase 1."""

    def __init__(self, sqlite_path: Path):
        self.sqlite_path = sqlite_path
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self._local.conn = sqlite3.connect(
                str(self.sqlite_path),
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA foreign_keys = ON")
        return self._local.conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        cx = self._conn()
        cur = cx.cursor()
        try:
            yield cur
            cx.commit()
        except Exception:
            cx.rollback()
            raise

    def init_schema(self) -> None:
        cx = self._conn()
        cx.executescript(DDL)
        uv = int(cx.execute("PRAGMA user_version").fetchone()[0])
        if uv < SCHEMA_VERSION:
            cx.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            cx.commit()

    def ensure_dynamic_table(self, slug: str) -> str:
        """Creates evt_dyn_<slug> table if missing. Slug must already be sanitized."""
        table = f"evt_dyn_{slug}"
        cx = self._conn()
        cx.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
              id TEXT PRIMARY KEY,
              recorded_at TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              confidence REAL NOT NULL DEFAULT 1.0,
              source TEXT NOT NULL,
              provenance_json TEXT,
              clarification_pending INTEGER NOT NULL DEFAULT 0
            )
            """,
        )
        cx.commit()
        return table


_db_singleton: Database | None = None


def get_db(settings: Settings) -> Database:
    global _db_singleton
    path = settings.resolved_sqlite()
    if _db_singleton is None or Path(_db_singleton.sqlite_path).resolve() != Path(path).resolve():
        _db_singleton = Database(path)
        _db_singleton.init_schema()
    return _db_singleton


def reset_db_singleton() -> None:
    global _db_singleton
    _db_singleton = None


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def insert_raw_event(
    conn: sqlite3.Cursor,
    *,
    occurred_at: str,
    source: str,
    event_type: str,
    payload: dict[str, Any],
    domain_slug: str | None = None,
    confidence: float | None = None,
    provenance: dict[str, Any] | None = None,
) -> str:
    rid = new_id("re_")
    conn.execute(
        """
        INSERT INTO raw_events
          (id, occurred_at, source, event_type, domain_slug, payload_json, confidence, provenance_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rid,
            occurred_at,
            source,
            event_type,
            domain_slug,
            json.dumps(payload, ensure_ascii=False),
            confidence,
            json.dumps(provenance or {}, ensure_ascii=False),
        ),
    )
    return rid


def insert_delegation_event(conn: sqlite3.Cursor, task_id: str, event_obj: dict[str, Any]) -> str:
    eid = new_id("de_")
    conn.execute(
        """
        INSERT INTO delegation_events (id, task_id, event_json) VALUES (?, ?, ?)
        """,
        (eid, task_id, json.dumps(event_obj, ensure_ascii=False)),
    )
    return eid


def insert_agent_run(conn: sqlite3.Cursor, task_id: str, source_agent: str, payload: dict[str, Any]) -> str:
    rid = new_id("ar_")
    conn.execute(
        """
        INSERT INTO agent_runs (id, task_id, source_agent, payload_json) VALUES (?, ?, ?, ?)
        """,
        (rid, task_id, source_agent, json.dumps(payload, ensure_ascii=False)),
    )
    return rid


def prune_raw_events(conn: sqlite3.Cursor, cutoff_iso: str, *, dry_run: bool = False) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM raw_events WHERE occurred_at < ?",
        (cutoff_iso,),
    ).fetchone()
    count = int(row[0]) if row else 0
    if dry_run:
        return count
    if count:
        conn.execute("DELETE FROM raw_events WHERE occurred_at < ?", (cutoff_iso,))
    return count


def iter_recent_raw_events(conn: sqlite3.Cursor, limit: int = 50) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM raw_events ORDER BY datetime(occurred_at) DESC LIMIT ?
        """,
        (limit,),
    )


def fetch_profile(conn: sqlite3.Cursor) -> dict[str, Any]:
    row = conn.execute(
        "SELECT body_json FROM user_profile WHERE id = 1",
    ).fetchone()
    if row:
        return json.loads(row["body_json"])
    conn.execute(
        "INSERT INTO user_profile (id, body_json) VALUES (1, '{}')",
    )
    return {}
