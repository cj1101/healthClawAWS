"""Shared formatting for chat ``conversation_context`` (Telegram / API turns)."""


from __future__ import annotations

from typing import Any


def format_recent_conversation_block(turns: list[dict[str, Any]] | None) -> str:
    """Format prior turns as a single preamble block.

    Mirrors the vision path so downstream routing/synthesis sees the same phrasing as the VLM.
    Skips turns with empty content after strip.
    """
    if not turns:
        return ""
    parts: list[str] = ["Recent conversation (oldest first, may be truncated):\n"]
    for m in turns:
        role = (m.get("role") or "").strip()
        content = (m.get("content") or "").strip()
        if content:
            parts.append(f"{role}: {content}\n")
    return "".join(parts)
