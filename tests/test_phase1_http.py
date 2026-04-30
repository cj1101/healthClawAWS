import json
from pathlib import Path

from fastapi.testclient import TestClient

from nemoclaw_health.app import create_app


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
