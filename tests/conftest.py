import pytest


@pytest.fixture
def iso_test_settings(tmp_path):
    """SQLite + artifact log under pytest tmp dirs; isolate singleton DB."""
    from nemoclaw_health.settings import Settings
    from nemoclaw_health.db import reset_db_singleton

    reset_db_singleton()
    data = tmp_path / "nemodata"
    data.mkdir(parents=True, exist_ok=True)
    s = Settings(
        data_dir=data,
        sqlite_path=data / "t.sqlite",
        artifact_log=data / "orchestration.jsonl",
        raw_event_retention_days=90,
    )
    yield s
    reset_db_singleton()
