"""Minimal OpenRouter chat-completions client."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from nemoclaw_health.settings import Settings


def chat_completion(
    settings: Settings,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.25,
    timeout_s: float = 120.0,
) -> str:
    key = settings.openrouter_api_key
    if not key:
        raise RuntimeError("OpenRouter API key is not configured")
    url = f"{settings.openrouter_api_base.rstrip('/')}/chat/completions"
    payload = {
        "model": settings.openrouter_model,
        "messages": messages,
        "temperature": temperature,
    }
    with httpx.Client(timeout=timeout_s) as client:
        r = client.post(
            url,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
    return str(data["choices"][0]["message"]["content"])


_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def parse_llm_json_object(raw: str) -> dict[str, Any]:
    """Extract the first JSON object from model output."""
    s = raw.strip()
    m = _JSON_FENCE.search(s)
    if m:
        s = m.group(1).strip()
    try:
        out = json.loads(s)
        if isinstance(out, dict):
            return out
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        return json.loads(s[start : end + 1])
    raise ValueError("no JSON object found in model response")
