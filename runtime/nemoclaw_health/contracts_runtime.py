"""Load Phase 0 contract JSON for orchestrator prompts."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


def _contracts_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "specs" / "phase0" / "contracts"


@lru_cache
def load_tool_registry() -> dict:
    path = _contracts_dir() / "tool_registry.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@lru_cache
def load_permission_matrix() -> dict:
    path = _contracts_dir() / "permission_matrix.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def tools_visible_to(agent: str) -> list[dict]:
    data = load_tool_registry()
    out: list[dict] = []
    for t in data.get("tools", []):
        vis = t.get("visibility") or []
        if agent in vis:
            out.append(t)
    return out


def contracts_prompt_blob(agent: str) -> str:
    tools = tools_visible_to(agent)
    pm = load_permission_matrix()
    parts = [
        f"You are agent `{agent}` in the Nemoclaw health team.",
        "Tools visible to you (JSON): "
        + json.dumps(tools, ensure_ascii=False, indent=2),
        "Permission matrix (JSON): "
        + json.dumps(pm.get("permissions", []), ensure_ascii=False, indent=2),
    ]
    return "\n".join(parts)
