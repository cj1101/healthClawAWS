from nemoclaw_health.db import get_db, reset_db_singleton
from nemoclaw_health.orchestrator import HealthOrchestrator


def test_chat_only_popeye_presents_user(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    get_db(s).init_schema()
    orch = HealthOrchestrator(s)
    out = orch.run_chat_turn("I ate chicken and logged a moderate workout.")

    assert "reply" in out
    assert "Stan (nutrition focus)" in out["reply"]

    db = get_db(s)
    with db.transaction() as cur:
        n = cur.execute(
            """
            SELECT COUNT(*) FROM delegation_events
            WHERE event_json LIKE '%"type": "present_to_user"%'
            """,
        ).fetchone()[0]

    assert n == 1


def test_joy_watch_includes_watch_marker(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    get_db(s).init_schema()

    orch = HealthOrchestrator(s)
    out = orch.run_chat_turn("My HRV has been in a downturn this week.")

    assert out["joy_tier"] == "watch"
    assert "[[JOY_WATCH_V1]]" in out["reply"]


def test_joy_urgent_includes_urgent_marker(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    get_db(s).init_schema()

    orch = HealthOrchestrator(s)
    out = orch.run_chat_turn("I have chest pain after training.")

    assert out["joy_tier"] == "urgent"
    assert "[[JOY_URGENT_V1]]" in out["reply"]


def test_llm_path_with_mocked_openrouter(iso_test_settings):
    from unittest.mock import patch

    reset_db_singleton()
    s = iso_test_settings.model_copy(update={"openrouter_api_key": "sk-test"})
    get_db(s).init_schema()

    calls = {"n": 0}

    def fake_chat(settings, messages, **kw):
        del settings
        i = calls["n"]
        calls["n"] += 1
        if i == 0:
            return (
                '{"workers":["stan"],"logging":'
                '{"should_log":false,"domain":"","payload":{},"source":"manual"}}'
            )
        if i == 1:
            return '{"macros_delta_hint":"prioritize protein","summary":"Stan stub via mock"}'
        return "Mock popeye synthesis with coaching cues."

    with patch("nemoclaw_health.orchestrator.chat_completion", side_effect=fake_chat):
        orch = HealthOrchestrator(s)
        out = orch.run_chat_turn("I ate lentils for lunch.")

    assert calls["n"] == 3
    assert "reply" in out
    assert len(out["trace_chain"]) >= 3

