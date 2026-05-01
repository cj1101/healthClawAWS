import pytest


@pytest.fixture
def iso_test_settings(tmp_path):
    """SQLite + artifact log under pytest tmp dirs; isolate singleton DB."""
    from nemoclaw_health.health_coach_store import configure_health_coach_db
    from nemoclaw_health.settings import Settings
    from nemoclaw_health.db import reset_db_singleton

    reset_db_singleton()
    data = tmp_path / "nemodata"
    data.mkdir(parents=True, exist_ok=True)
    s = Settings(
        _env_file=None,
        data_dir=data,
        sqlite_path=data / "t.sqlite",
        artifact_log=data / "orchestration.jsonl",
        health_db_path=data / "health.db",
        raw_event_retention_days=90,
        # Explicitly clear production credentials so tests always run in
        # deterministic stub mode with no auth wall, and WHOOP tests can
        # assert on the "not configured" code path.
        dashboard_password=None,
        session_secret=None,
        job_token=None,
        openrouter_api_key=None,
        whoop_client_id=None,
        whoop_client_secret=None,
        whoop_redirect_uri=None,
    )
    configure_health_coach_db(s.resolved_health_db())
    yield s
    reset_db_singleton()
