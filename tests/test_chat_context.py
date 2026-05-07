"""Tests for chat conversation context formatting and orchestrator wiring."""

from unittest.mock import patch

from nemoclaw_health.chat_context import format_recent_conversation_block
from nemoclaw_health.db import get_db, reset_db_singleton
from nemoclaw_health.orchestrator import HealthOrchestrator


def test_format_recent_conversation_block_empty() -> None:
    assert format_recent_conversation_block(None) == ""
    assert format_recent_conversation_block([]) == ""


def test_format_recent_conversation_block_renders_roles() -> None:
    s = format_recent_conversation_block(
        [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello sailor"},
        ]
    )
    assert "Recent conversation (oldest first, may be truncated):" in s
    assert "user: Hi\n" in s
    assert "assistant: Hello sailor\n" in s


def test_format_recent_conversation_block_skips_empty_content() -> None:
    block = format_recent_conversation_block(
        [
            {"role": "user", "content": "   "},
            {"role": "assistant", "content": "Only this"},
        ]
    )
    assert "Only this" in block
    assert "user:" not in block or "user:   " not in block


def test_run_chat_turn_prepends_conversation_context_isostub(iso_test_settings) -> None:
    reset_db_singleton()
    s = iso_test_settings
    get_db(s).init_schema()
    orch = HealthOrchestrator(s)
    captured: dict[str, str] = {}

    def capture_classify(text: str):
        captured["text"] = text
        return (["stan"], {})

    with patch("nemoclaw_health.orchestrator.classify_intents", side_effect=capture_classify):
        orch.run_chat_turn(
            "Second message.",
            conversation_context=[
                {"role": "user", "content": "First."},
                {"role": "assistant", "content": "Reply one."},
            ],
        )

    merged = captured["text"]
    assert "Recent conversation (oldest first" in merged
    assert "user: First." in merged
    assert "assistant: Reply one." in merged
    assert "Current turn:" in merged
    assert "Second message." in merged
