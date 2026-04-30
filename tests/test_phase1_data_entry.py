from nemoclaw_health.db import get_db, reset_db_singleton
from nemoclaw_health.data_entry import DataEntryService


def test_dynamic_domain_registers_and_writes(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    db = get_db(s)
    db.init_schema()

    svc = DataEntryService(s)
    svc.register_domain("Energy level", schema_hint=["value", "time_of_day"])

    out = svc.ingest(domain="Energy level", payload={"value": 7, "time_of_day": "morning"}, source="manual")
    assert out["status"] == "committed"
    assert out["domain_slug"] == "energy_level"

    tbl = "evt_dyn_energy_level"
    with db.transaction() as cur:
        c = cur.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]

    assert c == 1


def test_low_confidence_requires_clarification(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    db = get_db(s)
    db.init_schema()

    svc = DataEntryService(s)
    svc.register_domain("Meal snack", schema_hint=["description", "kcal_estimate"])

    out = svc.ingest(domain="Meal snack", payload={}, source="manual")
    assert out["status"] == "clarification_required"

    tbl = "evt_dyn_meal_snack"
    with db.transaction() as cur:
        c = cur.execute(
            f"SELECT COUNT(*) FROM {tbl} WHERE clarification_pending = 1",
        ).fetchone()[0]

    assert c == 1


def test_clarification_commit_then_committed(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    db = get_db(s)
    db.init_schema()

    svc = DataEntryService(s)
    svc.register_domain("Meal snack", schema_hint=["description", "kcal_estimate"])
    out = svc.ingest(domain="Meal snack", payload={}, source="manual")
    assert out["status"] == "clarification_required"
    pid = str(out["pending_row_id"])

    done = svc.commit_clarification(
        pending_row_id=pid,
        domain_slug="meal_snack",
        payload_patch={"description": "apple", "kcal_estimate": 80},
    )
    assert done["status"] == "committed"

    tbl = "evt_dyn_meal_snack"
    with db.transaction() as cur:
        pend = cur.execute(
            f"SELECT COUNT(*) FROM {tbl} WHERE id = ? AND clarification_pending = 1",
            (pid,),
        ).fetchone()[0]
        committed_raw = cur.execute(
            "SELECT COUNT(*) FROM raw_events WHERE event_type = 'data_entry_committed'",
        ).fetchone()[0]

    assert pend == 0
    assert committed_raw >= 1


def test_update_schema_hints(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    get_db(s).init_schema()
    svc = DataEntryService(s)
    svc.register_domain("Hydration", schema_hint=["glasses"])
    r = svc.update_schema_hints(domain="Hydration", schema_hint=["glasses", "time_window"])
    assert "time_window" in r["schema_hint"]
