import pytest


@pytest.fixture
def iso_test_settings(tmp_path):
    """SQLite + artifact log under pytest tmp dirs; isolate singleton DB."""
    from nemoclaw_health.settings import Settings
    from nemoclaw_health.db import reset_db_singleton

    reset_db_singleton()
    data = tmp_path / "nemodata"
    data.mkdir(parents=True, exist_ok=True)
    # model_construct: do not merge os.environ / .env so WHOOP_* in a dev .env
    # cannot override explicit Nones (pydantic-settings env wins over __init__ kwargs).
    s = Settings.model_construct(
        data_dir=data,
        sqlite_path=data / "t.sqlite",
        artifact_log=data / "orchestration.jsonl",
        raw_event_retention_days=90,
        dashboard_password=None,
        session_secret=None,
        job_token=None,
        openrouter_api_key=None,
        whoop_client_id=None,
        whoop_client_secret=None,
        whoop_redirect_uri=None,
    )
    yield s
    reset_db_singleton()
