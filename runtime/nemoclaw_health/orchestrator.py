from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from nemoclaw_health.artifacts import append_jsonl
from nemoclaw_health.db import (
    Database,
    get_db,
    insert_agent_run,
    insert_delegation_event,
    new_id,
)
from nemoclaw_health.events import UserVisibilityInvariantError, validate_orchestration_event
from nemoclaw_health.settings import Settings


def load_joy_templates() -> list[dict[str, Any]]:
    root = Path(__file__).resolve().parents[2]
    path = root / "specs" / "phase0" / "safety" / "joy_templates.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)["templates"]


_JOY_TEMPLATES = None


def joy_templates() -> list[dict[str, Any]]:
    global _JOY_TEMPLATES
    if _JOY_TEMPLATES is None:
        _JOY_TEMPLATES = load_joy_templates()
    return _JOY_TEMPLATES


def joy_body_for_tier(tier: str) -> str:
    m = {"info": "JOY_INFO_V1", "watch": "JOY_WATCH_V1", "urgent": "JOY_URGENT_V1"}
    tid = m.get(tier, "JOY_INFO_V1")
    for t in joy_templates():
        if t["id"] == tid:
            return str(t["body"])
    return str(joy_templates()[0]["body"])


def classify_intents(text: str) -> tuple[list[str], dict[str, Any]]:
    """
    Lightweight router for Phase 1 (keyword stub). Replace with LLM/router in Phase 3+.
    Returns (worker_chain, classifier_meta).
    """
    t = text.lower()
    flags: dict[str, Any] = {}

    risky = (
        r"\b(chest pain|heart attack|can'?t breathe|can't breathe|fainting|blood pressure systolic\s*1[6-9]\d)\b",
        r"\b(180|190|200)\s*bpm\b",
        r"\b(emergency room|911|988)\b",
    )
    for pat in risky:
        if re.search(pat, t, re.I):
            flags["risk_signals"] = True
            break

    workers: list[str] = []
    if flags.get("risk_signals"):
        workers.append("joy")
    elif re.search(
        r"\b(hrv|resting\s+heart|heart\s+rate).*\b(down|dropped|worse)|\brecovery\s+downtrend\b",
        t,
        re.I,
    ):
        workers.append("joy")
        flags["risk_signals_watch"] = True
    if re.search(r"\b(ate|meal|snack|protein|macros|nutrition|hunger)\b", t):
        workers.append("stan")
    if re.search(r"\b(workout|gym|reps?|sets?|squat|deadlift|jump higher|prs?|lifting)\b", t):
        workers.append("dick")
    if not workers:
        workers.append("stan")

    ordered: list[str] = []
    for w in workers:
        if w not in ordered:
            ordered.append(w)
    if "joy" in ordered:
        tail = sorted(
            [w for w in ordered if w != "joy"],
            key=lambda w: {"stan": 0, "dick": 1}.get(w, 9),
        )
        ordered = ["joy", *tail]

    return ordered, flags


def _delegate_event(task_id: str, target: str, intent_slug: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "source_agent": "popeye",
        "target_agent": target,
        "intent": intent_slug,
        "confidence": 0.82,
        "risk": "low",
        "payload": payload,
        "citations": [],
        "actions": [{"type": "delegate", "detail": {"ttl_s": 120}}],
        "workflow_id": f"wf_{task_id}",
        "team_id": "health",
        "policy_decision": "auto",
    }


def _return_event(
    task_id: str,
    source: str,
    intent_slug: str,
    payload: dict[str, Any],
    *,
    operational_risk: str = "low",
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "source_agent": source,
        "target_agent": "popeye",
        "intent": intent_slug,
        "confidence": 0.8,
        "risk": operational_risk,
        "payload": payload,
        "citations": [],
        "actions": [{"type": "return_to_manager", "detail": {}}],
    }


def _present_event(task_id: str, synthesized: str, joy_templates_applied: list[str]) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "source_agent": "popeye",
        "target_agent": "system",
        "intent": "synthesize_reply",
        "confidence": 0.88,
        "risk": "low",
        "payload": {
            "reply_markdown": synthesized,
            "joy": {"templates_applied": joy_templates_applied},
        },
        "citations": [],
        "actions": [{"type": "present_to_user", "detail": {"channel": "api"}}],
    }


def run_worker_stub(worker: str, user_text: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Deterministic stubs until OpenRouter-backed agents land in Phase 3."""
    if worker == "joy":
        if meta.get("risk_signals"):
            tier = "urgent"
            summary = "Notable physiological-language cues detected."
        elif meta.get("risk_signals_watch"):
            tier = "watch"
            summary = "Recovery or resting-vital wording suggests closer monitoring."
        else:
            tier = "info"
            summary = "No urgent keyword patterns matched."
        return {
            "tier": tier,
            "signals": meta,
            "summary": summary,
        }
    if worker == "stan":
        return {
            "macros_delta_hint": "Track protein distribution across waking hours versus your goal curve.",
            "summary": "Nutrition angle: stabilize meal spacing and quantify yesterday's intake variance.",
        }
    if worker == "dick":
        return {
            "session_struct_hint": ["warmup_skills_specificity", "main_volume_progression", "recovery_readiness_note"],
            "summary": "Training angle: emphasize progressive overload on the limiting factor for your stated goal.",
        }
    raise ValueError(f"unknown worker {worker}")


def enforce_no_worker_present(events: list[dict[str, Any]]) -> None:
    for ev in events:
        if ev.get("source_agent") == "popeye":
            continue
        actions = ev.get("actions") or []
        if any(a.get("type") == "present_to_user" for a in actions):
            raise UserVisibilityInvariantError("worker attempted present_to_user before synthesis")


class HealthOrchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db: Database = get_db(settings)
        self.artifact_log = settings.resolved_artifact_log()

    def _persist_event(self, parent_task: str, ev: dict[str, Any]) -> None:
        validate_orchestration_event(ev, enforce_invariant=True)
        with self.db.transaction() as cur:
            insert_delegation_event(cur, parent_task, ev)
        append_jsonl(self.artifact_log, ev)

    def run_chat_turn(self, user_message: str) -> dict[str, Any]:
        root_task = new_id("task_")
        chain: list[str] = []
        events_for_guard: list[dict[str, Any]] = []

        workers, meta = classify_intents(user_message)
        joy_tier_final = "info"
        joy_templates_applied: list[str] = []

        with self.db.transaction() as cur:
            insert_agent_run(cur, root_task, "popeye", {"phase": "classify", "workers": workers, "meta": meta})

        for w in workers:
            delegation = _delegate_event(root_task, w, f"delegate_to_{w}", {"user_message": user_message, "meta": meta})
            delegation["workflow_id"] = f"wf_{root_task}_{w}"

            validate_orchestration_event(delegation, enforce_invariant=True)
            with self.db.transaction() as cur:
                insert_delegation_event(cur, root_task, delegation)
            append_jsonl(self.artifact_log, delegation)
            chain.append(f"popeye -> {w} (delegate)")

            stub = run_worker_stub(w, user_message, meta)
            if w == "joy":
                joy_tier_final = stub.get("tier", "info")

            op_risk = (
                "medium"
                if w == "joy" and stub.get("tier") in ("watch", "urgent")
                else "low"
            )
            rtn = _return_event(
                root_task,
                w,
                f"{w}_response",
                stub,
                operational_risk=op_risk,
            )

            validate_orchestration_event(rtn, enforce_invariant=True)
            events_for_guard.append(rtn)

            with self.db.transaction() as cur:
                insert_delegation_event(cur, root_task, rtn)
                insert_agent_run(cur, root_task, w, {"stub_response": stub})
            append_jsonl(self.artifact_log, rtn)
            chain.append(f"{w} -> popeye (structured return)")

        enforce_no_worker_present(events_for_guard)

        synth = synthesize_stub_reply(user_message, workers, joy_tier_final)
        tid = joy_template_id_for_tier(joy_tier_final)
        if tid:
            joy_templates_applied.append(tid)

        present = _present_event(root_task, synth, joy_templates_applied)
        validate_orchestration_event(present, enforce_invariant=True)
        with self.db.transaction() as cur:
            insert_delegation_event(cur, root_task, present)
            if joy_tier_final in ("watch", "urgent"):
                cur.execute(
                    """
                    INSERT INTO disclaimer_audit (id, task_id, tier, templates_applied_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        new_id("da_"),
                        root_task,
                        joy_tier_final,
                        json.dumps(joy_templates_applied),
                    ),
                )
        append_jsonl(self.artifact_log, present)
        chain.append("popeye -> present_to_user")

        reply = present["payload"]["reply_markdown"]
        return {"task_id": root_task, "reply": reply, "trace_chain": chain, "joy_tier": joy_tier_final}


def joy_template_id_for_tier(tier: str) -> str | None:
    return {"info": "JOY_INFO_V1", "watch": "JOY_WATCH_V1", "urgent": "JOY_URGENT_V1"}.get(tier)


def synthesize_stub_reply(user_message: str, workers: list[str], joy_tier: str) -> str:
    """Merge specialist stubs deterministically + mandatory Joy disclaimer text for elevated tiers."""
    blocks: list[str] = []

    if joy_tier == "watch":
        blocks.append(joy_body_for_tier("watch"))
    elif joy_tier == "urgent":
        blocks.append(joy_body_for_tier("urgent"))
    elif "joy" in workers:
        blocks.append(joy_body_for_tier("info"))

    if "stan" in workers:
        blocks.append(
            "**Stan (nutrition focus):** stabilize meal rhythm and quantify protein vs your target band "
            "for the window you mentioned.",
        )
    if "dick" in workers:
        blocks.append(
            "**Dick (training focus):** keep intensity progression tied to symptom-free ranges; bias the "
            "limiting factor drills you named toward your performance goal.",
        )

    blocks.append("_Coaching synthesis only — not medical diagnosis or individualized treatment directives._")

    recap = (
        f"I routed your note through: `{', '.join(workers)}` based on cues in your message "
        "(Phase 1 uses a lightweight classifier; richer routing arrives with the full Popeye router)."
    )
    return "\n\n".join([recap] + blocks)
