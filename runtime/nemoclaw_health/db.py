from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Iterable

from nemoclaw_health.settings import Settings

SCHEMA_VERSION = 4

_DYN_SLUG_OK = re.compile(r"^[a-z0-9_]{1,48}$")

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

    def __init__(self, sqlite_path: Path, *, busy_timeout_ms: int = 5000):
        self.sqlite_path = sqlite_path
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self._local.conn = sqlite3.connect(
                str(self.sqlite_path),
                check_same_thread=False,
                timeout=float(self.busy_timeout_ms) / 1000.0,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        return self._local.conn

    def get_connection(self) -> sqlite3.Connection:
        """Thread-local SQLite connection (e.g. long streams with periodic commits)."""
        return self._conn()

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
        self._migrate_from(cx, uv)
        cx.commit()

    def _migrate_from(self, cx: sqlite3.Connection, from_version: int) -> None:
        if from_version >= SCHEMA_VERSION:
            return
        if from_version < 2:
            cx.execute(
                """
                CREATE TABLE IF NOT EXISTS connector_idempotency (
                  connector_id TEXT NOT NULL,
                  dedupe_key TEXT NOT NULL,
                  raw_event_id TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT (datetime('now')),
                  PRIMARY KEY (connector_id, dedupe_key)
                )
                """
            )
        if from_version < 4:
            cx.executescript(
                """
                CREATE TABLE IF NOT EXISTS whoop_workout (
                  id TEXT PRIMARY KEY NOT NULL,
                  user_id INTEGER,
                  start TEXT,
                  end TEXT,
                  payload_json TEXT NOT NULL,
                  fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS whoop_sleep (
                  id TEXT PRIMARY KEY NOT NULL,
                  cycle_id INTEGER,
                  payload_json TEXT NOT NULL,
                  fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS whoop_recovery (
                  sleep_id TEXT PRIMARY KEY NOT NULL,
                  cycle_id INTEGER,
                  payload_json TEXT NOT NULL,
                  fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS whoop_cycle (
                  id INTEGER PRIMARY KEY NOT NULL,
                  payload_json TEXT NOT NULL,
                  fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS whoop_body_measurement_snapshot (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  snapshot_hash TEXT NOT NULL UNIQUE,
                  payload_json TEXT NOT NULL,
                  fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS whoop_profile (
                  user_id INTEGER PRIMARY KEY NOT NULL,
                  payload_json TEXT NOT NULL,
                  fetched_at TEXT NOT NULL
                );
                """
            )
        cx.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
    resolved = Path(path).resolve()
    if (
        _db_singleton is None
        or Path(_db_singleton.sqlite_path).resolve() != resolved
        or _db_singleton.busy_timeout_ms != settings.sqlite_busy_timeout_ms
    ):
        _db_singleton = Database(path, busy_timeout_ms=settings.sqlite_busy_timeout_ms)
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


def dyn_row_id_from_provenance_json(blob: str | None) -> str | None:
    if not blob:
        return None
    try:
        d = json.loads(blob)
    except json.JSONDecodeError:
        return None
    rid = d.get("dyn_row")
    if isinstance(rid, str) and rid.strip():
        return rid.strip()
    return None


def count_connector_idempotency_for_prune(conn: sqlite3.Cursor, cutoff_iso: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM connector_idempotency
        WHERE raw_event_id IN (SELECT id FROM raw_events WHERE occurred_at < ?)
        """,
        (cutoff_iso,),
    ).fetchone()
    return int(row[0]) if row else 0


def prune_raw_events_and_linked_dyn_rows(conn: sqlite3.Cursor, cutoff_iso: str, *, dry_run: bool = False) -> dict[str, int]:
    """
    Delete stale raw_events and matching evt_dyn_<slug> rows when provenance carries dyn_row.
    Drops connector_idempotency rows pointing at deleted raw_event ids so connectors can re-ingest.
    Never touches derived_summaries, goals, or user_profile.
    """
    rows = conn.execute(
        """
        SELECT id, domain_slug, provenance_json FROM raw_events WHERE occurred_at < ?
        """,
        (cutoff_iso,),
    ).fetchall()
    raw_count = len(rows)
    dyn_pairs: list[tuple[str, str]] = []
    for r in rows:
        slug = r["domain_slug"]
        dyn_id = dyn_row_id_from_provenance_json(r["provenance_json"])
        if dyn_id and slug and _DYN_SLUG_OK.match(str(slug)):
            dyn_pairs.append((str(slug), dyn_id))
    idemp_count = count_connector_idempotency_for_prune(conn, cutoff_iso)

    if dry_run:
        return {
            "raw_events_affected": raw_count,
            "dyn_rows_deleted": len(dyn_pairs),
            "connector_idempotency_deleted": idemp_count,
        }

    changed_dyn = 0
    seen: set[tuple[str, str]] = set()
    for slug, dyn_id in dyn_pairs:
        key = (slug, dyn_id)
        if key in seen:
            continue
        seen.add(key)
        table = f"evt_dyn_{slug}"
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
            (table,),
        ).fetchone()
        if not exists:
            continue
        try:
            cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (dyn_id,))
            rc = cur.rowcount
            if isinstance(rc, int) and rc > 0:
                changed_dyn += rc
        except sqlite3.OperationalError:
            continue

    conn.execute(
        """
        DELETE FROM connector_idempotency
        WHERE raw_event_id IN (SELECT id FROM raw_events WHERE occurred_at < ?)
        """,
        (cutoff_iso,),
    )

    conn.execute("DELETE FROM raw_events WHERE occurred_at < ?", (cutoff_iso,))

    return {
        "raw_events_affected": raw_count,
        "dyn_rows_deleted": changed_dyn,
        "connector_idempotency_deleted": idemp_count,
    }


def prune_delegation_metadata(conn: sqlite3.Cursor, cutoff_iso: str, *, dry_run: bool = False) -> dict[str, int]:
    """Remove old orchestration audit rows (delegation_events, agent_runs)."""
    dr = conn.execute(
        "SELECT COUNT(*) FROM delegation_events WHERE unixepoch(recorded_at) < unixepoch(?)",
        (cutoff_iso,),
    ).fetchone()
    ar = conn.execute(
        "SELECT COUNT(*) FROM agent_runs WHERE unixepoch(started_at) < unixepoch(?)",
        (cutoff_iso,),
    ).fetchone()
    dec = int(dr[0]) if dr else 0
    arc = int(ar[0]) if ar else 0
    if dry_run:
        return {"delegation_events_affected": dec, "agent_runs_affected": arc}
    conn.execute(
        "DELETE FROM delegation_events WHERE unixepoch(recorded_at) < unixepoch(?)",
        (cutoff_iso,),
    )
    conn.execute(
        "DELETE FROM agent_runs WHERE unixepoch(started_at) < unixepoch(?)",
        (cutoff_iso,),
    )
    return {"delegation_events_deleted": dec, "agent_runs_deleted": arc}


def prune_raw_events(conn: sqlite3.Cursor, cutoff_iso: str, *, dry_run: bool = False) -> int:
    """Backward-compatible count/delete for raw_events only (no dyn cleanup). Prefer prune_raw_events_and_linked_dyn_rows."""
    stats = prune_raw_events_and_linked_dyn_rows(conn, cutoff_iso, dry_run=dry_run)
    return int(stats["raw_events_affected"])


def iter_recent_raw_events(conn: sqlite3.Cursor, limit: int = 50) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM raw_events ORDER BY datetime(occurred_at) DESC LIMIT ?
        """,
        (limit,),
    )


def fetch_connector_state(conn: sqlite3.Cursor, connector_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT state_json FROM connector_states WHERE connector_id = ?",
        (connector_id,),
    ).fetchone()
    if not row:
        return {}
    return json.loads(row["state_json"] or "{}")


def put_connector_state(conn: sqlite3.Cursor, connector_id: str, state: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO connector_states (connector_id, state_json, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(connector_id) DO UPDATE SET
          state_json = excluded.state_json,
          updated_at = datetime('now')
        """,
        (connector_id, json.dumps(state, ensure_ascii=False)),
    )


def idempotency_seen(conn: sqlite3.Cursor, connector_id: str, dedupe_key: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM connector_idempotency WHERE connector_id = ? AND dedupe_key = ?
        """,
        (connector_id, dedupe_key),
    ).fetchone()
    return row is not None


def record_idempotency(conn: sqlite3.Cursor, connector_id: str, dedupe_key: str, raw_event_id: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO connector_idempotency
          (connector_id, dedupe_key, raw_event_id)
        VALUES (?, ?, ?)
        """,
        (connector_id, dedupe_key, raw_event_id),
    )


def upsert_whoop_workout(conn: sqlite3.Cursor, payload: dict[str, Any], *, fetched_at: str) -> None:
    rid = payload.get("id")
    if rid is None or isinstance(rid, dict):
        return
    rid_s = str(rid).strip()
    if not rid_s:
        return
    user_raw = payload.get("user_id")
    user_id_i: int | None
    try:
        user_id_i = int(user_raw) if user_raw is not None and not isinstance(user_raw, dict) else None
    except (TypeError, ValueError):
        user_id_i = None
    start_s = str(payload["start"]) if isinstance(payload.get("start"), str) else None
    end_s = str(payload["end"]) if isinstance(payload.get("end"), str) else None
    blob = json.dumps(payload, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO whoop_workout (id, user_id, start, end, payload_json, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          user_id = excluded.user_id,
          start = excluded.start,
          end = excluded.end,
          payload_json = excluded.payload_json,
          fetched_at = excluded.fetched_at
        """,
        (rid_s, user_id_i, start_s, end_s, blob, fetched_at),
    )


def upsert_whoop_sleep(conn: sqlite3.Cursor, payload: dict[str, Any], *, fetched_at: str) -> None:
    rid = payload.get("id")
    if rid is None or isinstance(rid, dict):
        return
    rid_s = str(rid).strip()
    if not rid_s:
        return
    cyc = payload.get("cycle_id")
    try:
        cycle_i = int(cyc) if cyc is not None and not isinstance(cyc, dict) else None
    except (TypeError, ValueError):
        cycle_i = None
    blob = json.dumps(payload, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO whoop_sleep (id, cycle_id, payload_json, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          cycle_id = excluded.cycle_id,
          payload_json = excluded.payload_json,
          fetched_at = excluded.fetched_at
        """,
        (rid_s, cycle_i, blob, fetched_at),
    )


def upsert_whoop_recovery(conn: sqlite3.Cursor, payload: dict[str, Any], *, fetched_at: str) -> None:
    sid = payload.get("sleep_id")
    if sid is None or isinstance(sid, dict):
        return
    sleep_s = str(sid).strip()
    if not sleep_s:
        return
    cyc = payload.get("cycle_id")
    try:
        cycle_i = int(cyc) if cyc is not None and not isinstance(cyc, dict) else None
    except (TypeError, ValueError):
        cycle_i = None
    blob = json.dumps(payload, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO whoop_recovery (sleep_id, cycle_id, payload_json, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(sleep_id) DO UPDATE SET
          cycle_id = excluded.cycle_id,
          payload_json = excluded.payload_json,
          fetched_at = excluded.fetched_at
        """,
        (sleep_s, cycle_i, blob, fetched_at),
    )


def upsert_whoop_cycle(conn: sqlite3.Cursor, payload: dict[str, Any], *, fetched_at: str) -> None:
    rid = payload.get("id")
    if rid is None or isinstance(rid, dict):
        return
    try:
        cid = int(rid)
    except (TypeError, ValueError):
        return
    blob = json.dumps(payload, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO whoop_cycle (id, payload_json, fetched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          payload_json = excluded.payload_json,
          fetched_at = excluded.fetched_at
        """,
        (cid, blob, fetched_at),
    )


def upsert_whoop_body_measurement_snapshot(
    conn: sqlite3.Cursor,
    *,
    snapshot_hash: str,
    payload: dict[str, Any],
    fetched_at: str,
) -> None:
    h = snapshot_hash.strip()
    if not h:
        return
    blob = json.dumps(payload, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO whoop_body_measurement_snapshot (snapshot_hash, payload_json, fetched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(snapshot_hash) DO UPDATE SET
          payload_json = excluded.payload_json,
          fetched_at = excluded.fetched_at
        """,
        (h, blob, fetched_at),
    )


def upsert_whoop_profile(conn: sqlite3.Cursor, payload: dict[str, Any], *, fetched_at: str) -> None:
    uid = payload.get("user_id")
    try:
        user_id_i = int(uid)
    except (TypeError, ValueError):
        return
    blob = json.dumps(payload, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO whoop_profile (user_id, payload_json, fetched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
          payload_json = excluded.payload_json,
          fetched_at = excluded.fetched_at
        """,
        (user_id_i, blob, fetched_at),
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
