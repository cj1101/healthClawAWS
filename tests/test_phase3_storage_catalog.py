from fastapi.testclient import TestClient

from nemoclaw_health.app import create_app
from nemoclaw_health.data_entry import DataEntryService
from nemoclaw_health.db import get_db


def test_storage_catalog_managed_by_data_entry(iso_test_settings):
    client = TestClient(create_app(iso_test_settings))
    r = client.get("/v1/storage/catalog?tables=1")
    assert r.status_code == 200
    j = r.json()
    assert j["managed_by_agent"] == "data-entry"
    ids = {x["id"] for x in j["stores"]}
    assert "nemoclaw_sqlite" in ids
    assert "health_coach_sqlite" in ids
    assert "orchestration_jsonl" in ids
    h = next(x for x in j["stores"] if x["id"] == "health_coach_sqlite")
    assert h["path"].endswith("health.db")
    assert "tables" in h


def test_health_store_bootstrap_then_biometric(iso_test_settings):
    client = TestClient(create_app(iso_test_settings))
    assert client.post("/v1/data-entry/health-store/bootstrap").status_code == 200
    r = client.post(
        "/v1/data-entry/health-store/biometric-sample",
        json={
            "sample_date": "2024-06-02",
            "source": "manual",
            "recovery_score": 66.0,
            "sleep_hours": 8.0,
        },
    )
    assert r.status_code == 200
    catalog = client.get("/v1/storage/catalog?tables=1").json()
    h = next(x for x in catalog["stores"] if x["id"] == "health_coach_sqlite")
    bio = next(t for t in h["tables"] if t["name"] == "biometric_samples")
    assert bio["row_count"] >= 1


def test_ingest_mirrors_biometric_fields(iso_test_settings):
    get_db(iso_test_settings).init_schema()
    svc = DataEntryService(iso_test_settings)
    svc.register_domain("Daily vitals", schema_hint=["recovery_score"])
    out = svc.ingest(
        domain="Daily vitals",
        payload={
            "recovery_score": 72.5,
            "sleep_hours": 7.25,
            "sample_date": "2024-08-01",
        },
        source="manual",
    )
    assert out["status"] == "committed"
    assert out["health_db_mirror"]["ok"] is True
    assert out["health_db_mirror"].get("mirrored") is True
