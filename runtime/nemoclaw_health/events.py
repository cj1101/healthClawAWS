from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

_ROOT = Path(__file__).resolve().parents[2]
EVENT_SCHEMA_PATH = _ROOT / "specs" / "phase0" / "contracts" / "event_schema.json"

_validator_cache: Draft202012Validator | None = None


def load_event_validator() -> Draft202012Validator:
    global _validator_cache
    if _validator_cache is not None:
        return _validator_cache
    with open(EVENT_SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    _validator_cache = Draft202012Validator(schema)
    return _validator_cache


class EventValidationError(ValueError):
    pass


class UserVisibilityInvariantError(ValueError):
    """Contract deny-path: worker must not issue user-visible actions."""


def assert_user_visibility_allowed(event_obj: dict[str, Any]) -> None:
    """Phase 0 invariant — only popeye may request present_to_user."""
    actions = event_obj.get("actions") or []
    present = any(a.get("type") == "present_to_user" for a in actions)
    if not present:
        return
    source = event_obj.get("source_agent")
    if source != "popeye":
        raise UserVisibilityInvariantError(
            "`present_to_user` is forbidden unless source_agent is popeye.",
        )


def validate_orchestration_event(event_obj: dict[str, Any], *, enforce_invariant: bool = True) -> None:
    v = load_event_validator()
    errors = sorted(v.iter_errors(event_obj), key=lambda e: e.json_path)
    if errors:
        raise EventValidationError(
            "; ".join(f"{list(e.absolute_path)}: {e.message}" for e in errors),
        )
    if enforce_invariant:
        assert_user_visibility_allowed(event_obj)
