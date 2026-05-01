"""Vision pass for chat images: single OpenRouter call with the configured VLM (default Qwen 3.6)."""

from __future__ import annotations

import base64
from typing import Any

from nemoclaw_health.openrouter_client import chat_completion
from nemoclaw_health.settings import Settings

_VISION_SYSTEM = (
    "You are a vision subagent for a health coaching pipeline. Your output is consumed by routing "
    "and specialist agents (nutrition, training, recovery). Describe only what is visible or clearly "
    "readable: OCR text on labels or screens, foods and approximate portions if inferable, exercise "
    "equipment or logged workouts, wearable charts or metrics (quote numbers when readable), and "
    "medical-looking documents only as neutral transcription—do not diagnose. Note uncertainty "
    "when the image is blurry or ambiguous. Use concise prose in plain text (no markdown fences)."
)


def describe_images_for_coaching(
    settings: Settings,
    *,
    user_text: str,
    images: list[tuple[str, bytes]],
    conversation_context: list[dict[str, str]] | None,
) -> str:
    """Multimodal completion using ``settings.openrouter_vision_model``."""
    if not images:
        return ""

    blocks: list[str] = []
    if conversation_context:
        blocks.append("Recent conversation (oldest first, may be truncated):\n")
        for m in conversation_context:
            role = m.get("role", "")
            content = (m.get("content") or "").strip()
            if content:
                blocks.append(f"{role}: {content}\n")

    ut = user_text.strip()
    if ut:
        blocks.append(f"Current user message: {ut}\n")

    blocks.append(
        "Describe the attached image(s) for the coaching assistant: transcribe visible text, "
        "identify foods/meals, exercise or biometric screenshots, charts, and limitations.\n"
        "Keep the answer focused and factual."
    )
    preamble = "".join(blocks)

    content_parts: list[dict[str, Any]] = [{"type": "text", "text": preamble}]
    for mime, raw in images:
        b64 = base64.standard_b64encode(raw).decode("ascii")
        data_uri = f"data:{mime};base64,{b64}"
        content_parts.append({"type": "image_url", "image_url": {"url": data_uri}})

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _VISION_SYSTEM},
        {"role": "user", "content": content_parts},
    ]

    return chat_completion(
        settings,
        messages,
        temperature=0.2,
        timeout_s=180.0,
        model=settings.openrouter_vision_model,
    ).strip()
