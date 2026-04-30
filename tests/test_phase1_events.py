from pathlib import Path

import pytest

from nemoclaw_health.events import (
    EventValidationError,
    UserVisibilityInvariantError,
    assert_user_visibility_allowed,
    validate_orchestration_event,
)


ROOT = Path(__file__).resolve().parents[1]


def test_denied_worker_present_to_user_fixture():
    p = ROOT / "specs" / "phase0" / "contracts" / "samples" / "03_denied_worker_present_to_user.json"
    import json

    evt = json.loads(p.read_text(encoding="utf-8"))
    validate_orchestration_event(evt, enforce_invariant=False)

    with pytest.raises(UserVisibilityInvariantError):
        validate_orchestration_event(evt, enforce_invariant=True)

    with pytest.raises(UserVisibilityInvariantError):
        assert_user_visibility_allowed(evt)


def test_schema_rejects_partial_event():
    with pytest.raises(EventValidationError):
        validate_orchestration_event({"task_id": "x"}, enforce_invariant=False)
