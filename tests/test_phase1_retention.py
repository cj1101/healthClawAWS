from datetime import datetime, timezone

from nemoclaw_health.db import get_db, reset_db_singleton
from nemoclaw_health.retention import run_raw_event_prune


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
