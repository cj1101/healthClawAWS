"""Phase 2: WHOOP OAuth + sync integration tests (mocked HTTP / API client)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from nemoclaw_health.app import create_app
from nemoclaw_health.connectors import whoop_oauth as wo
from nemoclaw_health.connectors import whoop_sync as ws
from nemoclaw_health.db import fetch_connector_state, get_db, put_connector_state


def test_whoop_status_disconnected_default(iso_test_settings):
    client = TestClient(create_app(iso_test_settings))
    r = client.get("/v1/connectors/whoop/status")
    assert r.status_code == 200
    assert r.json()["connected"] is False


def test_whoop_authorize_requires_config(iso_test_settings):
    client = TestClient(create_app(iso_test_settings))
    r = client.get("/v1/connectors/whoop/authorize-url")
    assert r.status_code == 503


def test_whoop_oauth_authorize_then_callback(monkeypatch, iso_test_settings):
    monkeypatch.delenv("NEMOWLAW_WHOOP_CLIENT_ID", raising=False)
    monkeypatch.delenv("NEMOWLAW_WHOOP_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("NEMOWLAW_WHOOP_REDIRECT_URI", raising=False)

    cfg = iso_test_settings.model_copy(
        update={
            "whoop_client_id": "test-cli",
            "whoop_client_secret": "secret",
            "whoop_redirect_uri": "http://localhost:9999/v1/connectors/whoop/callback",
        },
    )
    app = create_app(cfg)

    monkeypatch.setattr(
        wo,
        "token_exchange_authorization_code",
        lambda _settings, **_k: {  # noqa: ARG005
            "access_token": "a1",
            "refresh_token": "r1",
            "expires_in": 3600,
            "scope": "offline",
        },
    )

    c1 = TestClient(app)
    a = c1.get("/v1/connectors/whoop/authorize-url")
    assert a.status_code == 200

    db = get_db(cfg)
    with db.transaction() as cur:
        st = fetch_connector_state(cur, "whoop")

    oauth_state = (st.get("oauth_pending") or {}).get("state")

    cb = c1.get(
        "/v1/connectors/whoop/callback",
        params={"code": "auth-code-xyz", "state": oauth_state},
    )
    assert cb.status_code == 200
    body = cb.json()
    assert body.get("connected") is True

    tok = wo.ensure_whoop_access_token(db, cfg)
    assert tok == "a1"


def test_whoop_callback_state_mismatch(iso_test_settings):
    cfg = iso_test_settings.model_copy(
        update={
            "whoop_client_id": "c",
            "whoop_client_secret": "s",
            "whoop_redirect_uri": "http://localhost/cb",
        },
    )
    app = create_app(cfg)
    cl = TestClient(app)
    cl.get("/v1/connectors/whoop/authorize-url")
    cb = cl.get("/v1/connectors/whoop/callback", params={"code": "x", "state": "wrong"})
    assert cb.status_code == 400


class _FakeWhoopClient:
    def __init__(self, *_a, **_k) -> None:  # noqa: ANN401
        pass

    def get_workouts(self, **_k):  # noqa: ANN003
        return [
            {"id": "w-1", "start": "2025-06-01T12:00:00.000Z", "sport_id": 1, "strain": 12.5},
        ]

    def get_sleep(self, **_k):  # noqa: ANN003
        return []

    def get_recovery(self, **_k):  # noqa: ANN003
        return []

    def get_cycles(self, **_k):  # noqa: ANN003
        return []

    def get_body_measurement(self):  # noqa: ANN202
        return {"height_meter": 1.82, "weight_kilogram": 80}


def test_whoop_sync_mocked(monkeypatch, iso_test_settings):
    cfg = iso_test_settings.model_copy(
        update={
            "whoop_client_id": "c",
            "whoop_client_secret": "s",
            "whoop_redirect_uri": "http://localhost/cb",
        },
    )
    db = get_db(cfg)

    with db.transaction() as cur:
        put_connector_state(
            cur,
            "whoop",
            {
                "oauth": {"access_token": "t", "refresh_token": "r", "expires_at": 9999999999},
            },
        )

    monkeypatch.setattr(ws, "WhoopAPIClient", _FakeWhoopClient)

    r1 = ws.sync_whoop(db, cfg, days=7)
    assert r1["ok"] is True
    ingested_workouts = int(r1["totals"]["workout"]["ingested"])
    assert ingested_workouts >= 1

    r2 = ws.sync_whoop(db, cfg, days=7)
    assert int(r2["totals"]["workout"]["skipped_duplicate"]) >= ingested_workouts


def test_http_whoop_sync_route(monkeypatch, iso_test_settings):
    cfg = iso_test_settings.model_copy(
        update={
            "whoop_client_id": "c",
            "whoop_client_secret": "s",
            "whoop_redirect_uri": "http://localhost/cb",
        },
    )
    monkeypatch.setattr(ws, "WhoopAPIClient", _FakeWhoopClient)

    db = get_db(cfg)
    with db.transaction() as cur:
        put_connector_state(
            cur,
            "whoop",
            {
                "oauth": {"access_token": "tok", "refresh_token": "r", "expires_at": 9999999999},
            },
        )

    client = TestClient(create_app(cfg))
    resp = client.post("/v1/connectors/whoop/sync")
    assert resp.status_code == 200
    assert resp.json()["totals"]["workout"]["fetched"] >= 1
