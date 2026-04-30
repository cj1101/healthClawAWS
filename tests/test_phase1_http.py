import json
from pathlib import Path

from fastapi.testclient import TestClient

from nemoclaw_health.app import create_app
from nemoclaw_health.db import reset_db_singleton
from nemoclaw_health.settings import Settings


ROOT = Path(__file__).resolve().parents[1]


def test_http_validate_denied_fixture(iso_test_settings):
    with open(
        ROOT / "specs" / "phase0" / "contracts" / "samples" / "03_denied_worker_present_to_user.json",
        encoding="utf-8",
    ) as f:
        evt = json.load(f)

    client = TestClient(create_app(iso_test_settings))
    resp = client.post("/v1/contracts/validate-event", json={"event": evt})
    assert resp.status_code == 403


def test_http_validate_ok_fixture(iso_test_settings):
    with open(
        ROOT / "specs" / "phase0" / "contracts" / "samples" / "01_delegate_popeye_to_stan.json",
        encoding="utf-8",
    ) as f:
        evt = json.load(f)

    client = TestClient(create_app(iso_test_settings))
    resp = client.post("/v1/contracts/validate-event", json={"event": evt})
    assert resp.status_code == 200


def test_http_chat(iso_test_settings):
    client = TestClient(create_app(iso_test_settings))
    resp = client.post("/v1/chat", json={"message": "Breakfast was high protein today."})
    assert resp.status_code == 200
    body = resp.json()
    assert "reply" in body


def test_http_ingest_invalid_source(iso_test_settings):
    client = TestClient(create_app(iso_test_settings))
    resp = client.post(
        "/v1/data/ingest",
        json={"domain": "x", "payload": {}, "source": "unknown_vendor"},
    )
    assert resp.status_code == 400


def test_http_delegation_prune_skipped_when_unconfigured(iso_test_settings):
    client = TestClient(create_app(iso_test_settings))
    resp = client.post("/v1/jobs/delegation-prune", json={"dry_run": True})
    assert resp.status_code == 200
    assert resp.json().get("skipped") is True


def test_http_jobs_bearer_token_when_password_set(iso_test_settings):
    reset_db_singleton()
    data = iso_test_settings.data_dir
    s = Settings(
        data_dir=data,
        sqlite_path=data / "t.sqlite",
        artifact_log=data / "orchestration.jsonl",
        raw_event_retention_days=90,
        dashboard_password="dashboard-secret",
        job_token="job-secret-token",
    )
    client = TestClient(create_app(s))
    no_auth = client.post("/v1/jobs/raw-event-prune", json={"dry_run": True})
    assert no_auth.status_code == 401
    bad = client.post(
        "/v1/jobs/raw-event-prune",
        json={"dry_run": True},
        headers={"Authorization": "Bearer wrong"},
    )
    assert bad.status_code == 401
    ok = client.post(
        "/v1/jobs/raw-event-prune",
        json={"dry_run": True},
        headers={"Authorization": "Bearer job-secret-token"},
    )
    assert ok.status_code == 200
