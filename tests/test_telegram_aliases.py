"""Unit tests for Telegram plain-text command aliases (no Bot API)."""

from nemoclaw_health.telegram_bot import (
    _LABEL_HELP,
    _LABEL_NEW,
    _LABEL_SUMMARY,
    _normalize_alias_line,
    _route_text_command,
)


def test_normalize_alias_line_collapses_whitespace() -> None:
    assert _normalize_alias_line("  nemoclaw   new  ") == "nemoclaw new"


def test_route_hc_aliases() -> None:
    assert _route_text_command("hc:new") == "new"
    assert _route_text_command(_normalize_alias_line("HC:SUMMARY")) == "summary"
    assert _route_text_command("hc:help") == "help"


def test_route_nemoclaw_aliases() -> None:
    assert _route_text_command("nemoclaw new") == "new"
    assert _route_text_command("nemoclaw summary") == "summary"


def test_route_keyboard_labels_normalized() -> None:
    assert _route_text_command(_normalize_alias_line(_LABEL_NEW)) == "new"
    assert _route_text_command(_normalize_alias_line(_LABEL_SUMMARY)) == "summary"
    assert _route_text_command(_normalize_alias_line(_LABEL_HELP)) == "help"


def test_route_short_exact_matches() -> None:
    assert _route_text_command("reset") == "new"
    assert _route_text_command("snapshot") == "summary"
    assert _route_text_command("help") == "help"


def test_route_coaching_text_not_matched() -> None:
    assert _route_text_command("help me plan meals") is None
    assert _route_text_command("new workout idea") is None
