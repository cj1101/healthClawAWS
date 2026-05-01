from fastapi.testclient import TestClient

from nemoclaw_health.app import create_app
from nemoclaw_health.data_entry import DataEntryService
from nemoclaw_health.db import get_db


def test_lifespan_creates_health_db(iso_test_settings):
    hp = iso_test_settings.resolved_health_db()
    assert not hp.exists()
    with TestClient(create_app(iso_test_settings)) as client:
        assert client.get("/healthz").status_code == 200
        assert hp.is_file()


def test_data_entry_catalog_and_food_log_seed(iso_test_settings):
    with TestClient(create_app(iso_test_settings)) as client:
        client.get("/healthz")
        r = client.get("/v1/data-entry/catalog")
        assert r.status_code == 200
        cat = r.json()
        assert cat["managed_by_agent"] == "data-entry"
        assert "domains" in cat and "health_store" in cat
        slugs = {d["slug"] for d in cat["domains"]}
        assert "food_log" in slugs
        fl = next(x for x in cat["domains"] if x["slug"] == "food_log")
        assert "meal_ts" in fl["schema_hint"]
        assert fl["dynamic_table"] == "evt_dyn_food_log"
        assert cat["health_store"]["exists"] is True
        assert isinstance(cat["health_store"]["tables"], list)


def test_domain_rows_linked_raw_event(iso_test_settings):
    with TestClient(create_app(iso_test_settings)) as client:
        client.post(
            "/v1/data/ingest",
            json={
                "domain": "Sleep notes",
                "payload": {"hours": 7, "quality": "ok", "context": "nap"},
                "source": "manual",
                "confidence": 0.95,
            },
        )
        rows = client.get("/v1/data-entry/domain/sleep_notes/rows", params={"limit": 10})
        assert rows.status_code == 200
        body = rows.json()
        assert body["domain_slug"] == "sleep_notes"
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert row["confidence"] is not None
        assert row["raw_event"] is not None
        assert row["raw_event"]["event_type"] == "data_entry_committed"


def test_data_entry_events_filters(iso_test_settings):
    with TestClient(create_app(iso_test_settings)) as client:
        client.post(
            "/v1/data/ingest",
            json={
                "domain": "Steps",
                "payload": {"count": 9000, "device": "watch", "day": "today"},
                "source": "manual",
                "confidence": 0.95,
            },
        )
        ev = client.get("/v1/data-entry/events", params={"domain": "steps", "source": "manual", "limit": 5})
        assert ev.status_code == 200
        items = ev.json()["items"]
        assert len(items) >= 1
        assert all(i["domain_slug"] == "steps" for i in items)
        assert all(i["source"] == "manual" for i in items)


def test_data_entry_events_unknown_domain_404(iso_test_settings):
    with TestClient(create_app(iso_test_settings)) as client:
        assert client.get("/v1/data-entry/events", params={"domain": "missing_slug_xyz"}).status_code == 404


def test_insight_context_deterministic_fields(iso_test_settings):
    with TestClient(create_app(iso_test_settings)) as client:
        client.post(
            "/v1/data/ingest",
            json={
                "domain": "Hydration",
                "payload": {"liters": 2.1, "beverage": "water", "time": "morning"},
                "source": "manual",
                "confidence": 0.95,
            },
        )
        j = client.get("/v1/data-entry/insight-context", params={"days": 14, "recent_events_limit": 20}).json()
        assert j["managed_by_agent"] == "data-entry"
        assert j["window"]["days"] == 14
        assert "profile" in j and "goals" in j
        assert any(d["slug"] == "hydration" for d in j["domains_summary"])
        assert len(j["recent_events"]) >= 1
        assert "nemoclaw_raw_events" in j
        assert "whoop_mirror_row_counts_in_window" in j
        assert "meals" in j["health_db"] and "biometrics" in j["health_db"]


def test_meals_endpoint_wraps_health_db(iso_test_settings):
    with TestClient(create_app(iso_test_settings)) as client:
        m = client.get("/v1/data-entry/meals", params={"days": 7, "limit": 10}).json()
        assert m["managed_by_agent"] == "data-entry"
        assert "count" in m and "recent" in m


def test_health_store_bootstrap_idempotent_after_lifespan(iso_test_settings):
    """Manual bootstrap endpoint remains safe after lifespan bootstrap."""
    with TestClient(create_app(iso_test_settings)) as client:
        assert client.post("/v1/data-entry/health-store/bootstrap").status_code == 200


def test_service_rows_since_filter(iso_test_settings):
    get_db(iso_test_settings).init_schema()
    svc = DataEntryService(iso_test_settings)
    svc.ingest(domain="Temp", payload={"a": 1}, source="manual", occurred_at="2020-01-01T12:00:00Z")
    svc.ingest(domain="Temp", payload={"b": 2}, source="manual", occurred_at="2035-06-01T12:00:00Z")
    out = svc.list_domain_rows(slug="temp", limit=50, since="2030-01-01T00:00:00Z")
    assert len(out["rows"]) == 1
    assert out["rows"][0]["payload"] == {"b": 2}
