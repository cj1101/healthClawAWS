import json
from unittest.mock import patch

from nemoclaw_health.db import get_db, reset_db_singleton
from nemoclaw_health.data_entry import DataEntryService
from nemoclaw_health.orchestrator import HealthOrchestrator


def test_chat_only_popeye_presents_user(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    get_db(s).init_schema()
    orch = HealthOrchestrator(s)
    out = orch.run_chat_turn("I ate chicken and logged a moderate workout.")

    assert "reply" in out
    assert "Stan" in out["reply"]

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


def test_stub_turn_without_insight_marks_context_unavailable(iso_test_settings):
    reset_db_singleton()
    s = iso_test_settings
    get_db(s).init_schema()
    with patch(
        "nemoclaw_health.orchestrator.fetch_data_entry_insight_context",
        return_value=None,
    ):
        orch = HealthOrchestrator(s)
        out = orch.run_chat_turn("I ate chicken and greens.")
    task_id = out["task_id"]
    db = get_db(s)
    with db.transaction() as cur:
        rows = cur.execute(
            "SELECT source_agent, payload_json FROM agent_runs WHERE task_id = ?",
            (task_id,),
        ).fetchall()
    payloads = [json.loads(r[1]) for r in rows]
    classify = next(p for p in payloads if p.get("phase") == "classify_keyword")
    assert classify["insight_context_meta"]["available"] is False
    assert classify["insight_context_meta"]["bounded"] is None
    assert classify["routing_meta"]["data_context_available"] is False
    stan_run = next(p for p in payloads if p.get("phase") == "stub_worker" and "stub_response" in p)
    assert stan_run["data_context_available"] is False


def test_stub_turn_records_bounded_insight_when_service_exposes_builder(iso_test_settings):
    def fake_build(self, days=14):
        return {"window_days": days, "entry_count_recent": 5, "catalog_tags": ["food", "workout"]}

    reset_db_singleton()
    s = iso_test_settings
    get_db(s).init_schema()
    with patch.object(DataEntryService, "build_insight_context", fake_build):
        orch = HealthOrchestrator(s)
        out = orch.run_chat_turn("Light snack before bed.")
        task_id = out["task_id"]

    db = get_db(s)
    with db.transaction() as cur:
        rows = cur.execute(
            "SELECT payload_json FROM agent_runs WHERE task_id = ? ORDER BY started_at",
            (task_id,),
        ).fetchall()
        ev_rows = cur.execute(
            "SELECT event_json FROM delegation_events WHERE task_id = ?",
            (task_id,),
        ).fetchall()
    payloads = [json.loads(r[0]) for r in rows]
    classify = next(p for p in payloads if p.get("phase") == "classify_keyword")
    assert classify["insight_context_meta"]["available"] is True
    assert classify["insight_context_meta"]["bounded"]["entry_count_recent"] == 5
    assert classify["routing_meta"]["data_context_available"] is True
    assert classify["routing_meta"]["data_context"]["window_days"] == 14

    stan_delegate = None
    for r in ev_rows:
        ev = json.loads(r[0])
        if ev.get("intent") == "delegate_to_stan":
            stan_delegate = ev
            break
    assert stan_delegate is not None
    pl = stan_delegate["payload"]["meta"]
    assert pl["data_context_available"] is True
    assert pl["data_context"]["entry_count_recent"] == 5

    long_text = "x" * 2000

    def huge_build(self, days=14):
        return {"summary": "ok", "long_text": long_text}

    reset_db_singleton()
    s2 = iso_test_settings
    get_db(s2).init_schema()
    with patch.object(DataEntryService, "build_insight_context", huge_build):
        orch2 = HealthOrchestrator(s2)
        out2 = orch2.run_chat_turn("Another note.")
        task2 = out2["task_id"]
    assert len(long_text) > 500
    with get_db(s2).transaction() as cur:
        row_c = cur.execute(
            "SELECT payload_json FROM agent_runs WHERE task_id = ? AND payload_json LIKE '%classify_keyword%'",
            (task2,),
        ).fetchone()
    classify2 = json.loads(row_c[0])
    bounded_text = classify2["insight_context_meta"]["bounded"]["long_text"]
    assert len(bounded_text) <= 503
    assert bounded_text.endswith("...")

