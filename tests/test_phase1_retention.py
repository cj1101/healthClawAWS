from datetime import datetime, timezone

from nemoclaw_health.db import get_db, reset_db_singleton
from nemoclaw_health.export_backup import export_raw_events_jsonl
from nemoclaw_health.data_entry import DataEntryService
from nemoclaw_health.retention import run_delegation_metadata_prune, run_raw_event_prune
from nemoclaw_health.settings import Settings


def test_prune_dry_run_counts_without_delete(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    db = get_db(s)
    db.init_schema()

    old = (
        datetime(2025, 1, 2, tzinfo=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO raw_events (id, occurred_at, source, event_type, payload_json)
            VALUES ('r1', ?, 'manual', 'test_old', '{"k": true}')
            """,
            (old,),
        )

    r = run_raw_event_prune(db, retention_days=90, dry_run=True)
    assert r["raw_events_affected"] >= 1

    with db.transaction() as cur:
        c = cur.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]

    assert c == 1


def test_prune_deletes_stale_rows(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    db = get_db(s)
    db.init_schema()

    old = (
        datetime(2025, 1, 2, tzinfo=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO raw_events (id, occurred_at, source, event_type, payload_json)
            VALUES ('r2', ?, 'manual', 'test_old', '{}')
            """,
            (old,),
        )

    r = run_raw_event_prune(db, retention_days=90, dry_run=False)
    assert r["raw_events_affected"] >= 1

    with db.transaction() as cur:
        c = cur.execute("SELECT COUNT(*) FROM raw_events WHERE id='r2'").fetchone()[0]

    assert c == 0


def test_prune_removes_linked_dyn_rows(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    db = get_db(s)
    db.init_schema()
    svc = DataEntryService(s)
    svc.register_domain("meal_log", schema_hint=["kcal"])

    old = (
        datetime(2025, 1, 2, tzinfo=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    out = svc.ingest(
        domain="meal_log",
        payload={"kcal": 400},
        source="manual",
        occurred_at=old,
        client_confidence=1.0,
    )
    assert out["status"] == "committed"
    slug = out["domain_slug"]
    dyn_id = out["dynamic_row_id"]

    tbl = f"evt_dyn_{slug}"
    run_raw_event_prune(db, retention_days=90, dry_run=False)

    with db.transaction() as cur:
        rc = cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE id = ?", (dyn_id,)).fetchone()[0]
        raw_c = cur.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
    assert rc == 0
    assert raw_c == 0


def test_export_raw_events_jsonl_writes_file(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    db = get_db(s)
    db.init_schema()
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO raw_events (id, occurred_at, source, event_type, payload_json)
            VALUES ('rex1', '2026-04-01T12:00:00Z', 'manual', 'unit_test', '{"ok":true}')
            """,
        )
    dest = s.data_dir / "snap.jsonl"
    r = export_raw_events_jsonl(db, dest)
    assert dest.is_file()
    assert r["raw_events_written"] == 1


def test_delegation_metadata_prune_optional(iso_test_settings):
    reset_db_singleton()
    tmp = iso_test_settings.data_dir.parent / "deleg_meta"
    tmp.mkdir(parents=True, exist_ok=True)
    s = Settings(
        data_dir=tmp,
        sqlite_path=tmp / "deleg.sqlite",
        artifact_log=tmp / "a.jsonl",
        delegation_metadata_retention_days=400,
    )
    db = get_db(s)
    db.init_schema()

    old = "2024-06-01T00:00:00Z"
    new = "2026-04-01T00:00:00Z"
    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO delegation_events (id, task_id, recorded_at, event_json)
            VALUES ('de_old', 't_old', ?, '{"task_id":"t_old"}')
            """,
            (old,),
        )
        cur.execute(
            """
            INSERT INTO delegation_events (id, task_id, recorded_at, event_json)
            VALUES ('de_new', 't_new', ?, '{"task_id":"t_new"}')
            """,
            (new,),
        )
        cur.execute(
            """
            INSERT INTO agent_runs (id, task_id, source_agent, started_at, payload_json)
            VALUES ('ar_old', 't_old', 'popeye', ?, '{}')
            """,
            (old,),
        )

    run_delegation_metadata_prune(db, s.delegation_metadata_retention_days, dry_run=False)

    with db.transaction() as cur:
        dc = cur.execute("SELECT COUNT(*) FROM delegation_events").fetchone()[0]
        ac = cur.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]

    assert dc == 1
    assert ac == 0

    reset_db_singleton()


def test_goal_table_untouched(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    db = get_db(s)
    db.init_schema()

    with db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO goals (id, title, body_json, created_at)
            VALUES ('g1', 't', '{}', '2025-01-01T00:00:00Z')
            """,
        )

    run_raw_event_prune(db, retention_days=1, dry_run=False)

    with db.transaction() as cur:
        gc = cur.execute("SELECT COUNT(*) FROM goals WHERE id='g1'").fetchone()[0]

    assert gc == 1
