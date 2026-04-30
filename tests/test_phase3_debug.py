"""Phase 3: debug APIs + dashboard auth."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nemoclaw_health.app import create_app


def test_debug_sessions_and_trace_after_chat(iso_test_settings):
    client = TestClient(create_app(iso_test_settings))
    chat = client.post("/v1/chat", json={"message": "High protein breakfast today."})
    assert chat.status_code == 200
    task_id = chat.json().get("task_id")
    assert task_id

    sess = client.get("/v1/debug/sessions")
    assert sess.status_code == 200
    ids = [s["task_id"] for s in sess.json()["sessions"]]
    assert task_id in ids

    tr = client.get(f"/v1/debug/session/{task_id}")
    assert tr.status_code == 200
    body = tr.json()
    assert body["task_id"] == task_id
    assert len(body["delegation_events"]) >= 1

    an = client.post("/v1/debug/analyze", json={"task_id": task_id})
    assert an.status_code == 200
    assert "findings" in an.json()
    assert "trace" in an.json()


def test_debug_analyze_env_openrouter_warning(iso_test_settings):
    client = TestClient(create_app(iso_test_settings))
    r = client.post("/v1/debug/analyze", json={})
    assert r.status_code == 200
    codes = [f["code"] for f in r.json().get("findings", [])]
    assert "OPENROUTER_KEY_MISSING" in codes


def test_dashboard_password_blocks_v1_until_login(iso_test_settings):
    cfg = iso_test_settings.model_copy(update={"dashboard_password": "secret"})
    app = create_app(cfg)
    with TestClient(app) as client:
        r1 = client.post("/v1/chat", json={"message": "hi"})
        assert r1.status_code == 401

        r2 = client.post("/v1/auth/login", json={"password": "wrong"})
        assert r2.status_code == 401

        r3 = client.post("/v1/auth/login", json={"password": "secret"})
        assert r3.status_code == 200

        r4 = client.post("/v1/chat", json={"message": "hi"})
        assert r4.status_code == 200


def test_whoop_callback_allowed_without_session(iso_test_settings):
    cfg = iso_test_settings.model_copy(update={"dashboard_password": "x"})
    client = TestClient(create_app(cfg))
    r = client.get("/v1/connectors/whoop/callback", params={"code": "nope", "state": "bad"})
    assert r.status_code in (400, 502, 503)


def test_timeline_and_profile_goals(iso_test_settings):
    client = TestClient(create_app(iso_test_settings))
    g = client.get("/v1/profile")
    assert g.status_code == 200
    client.put("/v1/profile", json={"hello": "world"})
    g2 = client.get("/v1/profile")
    assert g2.json()["profile"]["hello"] == "world"

    gc = client.post("/v1/goals", json={"title": "Test goal", "body_json": {"k": 1}})
    assert gc.status_code == 200
    gl = client.get("/v1/goals")
    assert gl.status_code == 200
    assert len(gl.json()["goals"]) >= 1
