import json
from pathlib import Path

from nemoclaw_health.openrouter_client import chat_completion
from nemoclaw_health.settings import OPENROUTER_MODEL_ID, Settings


def test_chat_completion_pins_openrouter_model(monkeypatch):
    seen = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeClient:
        def __init__(self, timeout):
            seen["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, *, headers, json):
            seen["url"] = url
            seen["headers"] = headers
            seen["payload"] = json
            return FakeResponse()

    monkeypatch.setattr("nemoclaw_health.openrouter_client.httpx.Client", FakeClient)

    settings = Settings(
        _env_file=None,
        openrouter_api_key="sk-test",
        openrouter_model="other/text-model",
        openrouter_vision_model="other/vision-model",
    )

    out = chat_completion(
        settings,
        [{"role": "user", "content": "hello"}],
        model="other/explicit-model",
    )

    assert out == "ok"
    assert seen["payload"]["model"] == OPENROUTER_MODEL_ID


def test_vendor_agent_network_uses_only_pinned_model():
    path = (
        Path(__file__).resolve().parents[1]
        / "vendor/openclaw-health/workspace/agent-network/teams.v1.json"
    )
    data = json.loads(path.read_text())

    for agent in data["agents"]:
        assert agent.get("modelPreference") == OPENROUTER_MODEL_ID
        assert set(agent.get("modelFallbacks", [OPENROUTER_MODEL_ID])) == {OPENROUTER_MODEL_ID}
