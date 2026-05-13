from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from nemoclaw_health.artifacts import append_jsonl
from nemoclaw_health.chat_context import format_recent_conversation_block
from nemoclaw_health.contracts_runtime import contracts_prompt_blob
from nemoclaw_health.data_entry import DataEntryService
from nemoclaw_health.db import (
    Database,
    get_db,
    insert_agent_run,
    insert_delegation_event,
    new_id,
)
from nemoclaw_health.events import UserVisibilityInvariantError, validate_orchestration_event
from nemoclaw_health.health_coach_store import bootstrap_stan_snapshots, save_stan_snapshot
from nemoclaw_health.image_subagent import describe_images_for_coaching
from nemoclaw_health.openrouter_client import chat_completion, parse_llm_json_object
from nemoclaw_health.settings import OPENROUTER_MODEL_ID, Settings


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
    """Keyword router baseline (merged with optional LLM routing when API key present)."""
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


_VALID_WORKERS = frozenset({"stan", "dick", "joy"})


def fetch_data_entry_insight_context(settings: Settings, *, days: int = 14) -> dict[str, Any] | None:
    """Load bounded insight context from DataEntryService when the backend exposes it.

    Tries ``build_insight_context(days=...)`` then ``get_insight_context`` with the same
    call pattern. Returns ``None`` if methods are absent, unsupported, errors, or the
    return value is not a dict.
    """
    svc = DataEntryService(settings)
    for meth in ("build_insight_context", "get_insight_context"):
        fn = getattr(svc, meth, None)
        if not callable(fn):
            continue
        try:
            raw = fn(days=days)
        except TypeError:
            try:
                raw = fn()
            except Exception:
                raw = None
        except Exception:
            continue
        if isinstance(raw, dict):
            return raw
    return None


def _bounded_insight_context(insight: dict[str, Any], *, _depth: int = 0) -> dict[str, Any]:
    """Privacy/size guard: shallow structure, truncated strings/lists, capped nesting — no raw event dumps."""
    if _depth >= 3:
        return {}
    max_keys = 48 if _depth == 0 else 16
    out: dict[str, Any] = {}
    omitted = 0
    for i, (k, v) in enumerate(insight.items()):
        if i >= max_keys:
            omitted = len(insight) - max_keys
            break
        sk = str(k)[:80]
        if isinstance(v, (bool, int, float)) or v is None:
            out[sk] = v
        elif isinstance(v, str):
            out[sk] = v[:500] + ("..." if len(v) > 500 else "")
        elif isinstance(v, list):
            clipped: list[Any] = []
            for j, item in enumerate(v[:25]):
                if isinstance(item, dict):
                    clipped.append(_bounded_insight_context(item, _depth=_depth + 1))
                elif isinstance(item, str):
                    clipped.append(item[:300] + ("..." if len(item) > 300 else ""))
                elif isinstance(item, (bool, int, float)) or item is None:
                    clipped.append(item)
                else:
                    clipped.append(str(item)[:200])
            out[sk] = clipped
        elif isinstance(v, dict):
            out[sk] = _bounded_insight_context(v, _depth=_depth + 1)
        else:
            out[sk] = str(v)[:300]
    if omitted:
        out["_omitted_top_level_keys"] = omitted
    return out


def merge_workers_keyword_llm(keyword_workers: list[str], llm_workers: list[Any]) -> list[str]:
    merged: list[str] = []
    for w in keyword_workers:
        if w in _VALID_WORKERS and w not in merged:
            merged.append(w)
    for w in llm_workers:
        if isinstance(w, str) and w in _VALID_WORKERS and w not in merged:
            merged.append(w)
    if not merged:
        merged = ["stan"]
    if "joy" in merged:
        tail = sorted(
            [x for x in merged if x != "joy"],
            key=lambda w: {"stan": 0, "dick": 1}.get(w, 9),
        )
        return ["joy", *tail]
    return merged


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
    """Deterministic stubs when LLM is unavailable or JSON parsing fails."""
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
            "data_quality_flags": [],
            "macro_trends": {
                "summary": "Insufficient data for macro trend analysis. Log more meals to enable tracking.",
                "vs_goals": "Unable to assess — no goal baseline available.",
                "notable_patterns": [],
            },
            "recovery_correlations": {
                "summary": "Cross-reference analysis pending — ensure WHOOP or Apple Health data is synced.",
                "findings": [],
            },
            "sleep_correlations": {
                "summary": "Sleep correlation analysis pending — sync wearable data to enable.",
                "findings": [],
            },
            "workout_correlations": {
                "summary": "Workout correlation analysis pending — sync strain data to enable.",
                "findings": [],
            },
            "timing_patterns": {
                "summary": "Log meals consistently to surface timing patterns and fasting windows.",
                "fasting_windows": [],
                "gaps": [],
            },
            "anomalies": [],
            "goal_progress": {
                "summary": "Goal progress tracking requires consistent food logging and a defined goal.",
                "on_track": None,
                "metrics": [],
            },
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
            "**Stan (nutrition + biometric cross-reference):** log meals and sync wearable data "
            "to enable full recovery, sleep, and workout correlations against your nutrition patterns.",
        )
    if "dick" in workers:
        blocks.append(
            "**Dick (training focus):** keep intensity progression tied to symptom-free ranges; bias the "
            "limiting factor drills you named toward your performance goal.",
        )

    blocks.append("_Coaching synthesis only — not medical diagnosis or individualized treatment directives._")

    recap = (
        f"I routed your note through: `{', '.join(workers)}` based on cues in your message "
        "(keyword router plus optional LLM routing when configured)."
    )
    return "\n\n".join([recap] + blocks)


def _normalize_joy_worker_payload(obj: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    tier = str(obj.get("tier") or "info").lower()
    if tier not in ("info", "watch", "urgent"):
        tier = "info"
    summary = str(obj.get("summary") or "").strip() or "Joy assessment complete."
    signals = obj.get("signals") if isinstance(obj.get("signals"), dict) else meta
    return {"tier": tier, "signals": signals, "summary": summary}


def _normalize_stan_worker_payload(obj: dict[str, Any]) -> dict[str, Any]:
    def _str_field(key: str, default: str) -> str:
        return str(obj.get(key) or default).strip() or default

    def _list_field(key: str) -> list[Any]:
        v = obj.get(key)
        return v if isinstance(v, list) else []

    def _dict_field(key: str) -> dict[str, Any]:
        v = obj.get(key)
        return v if isinstance(v, dict) else {}

    def _section(key: str, summary_default: str) -> dict[str, Any]:
        raw = obj.get(key)
        if isinstance(raw, dict):
            if "summary" not in raw:
                raw["summary"] = summary_default
            return raw
        return {"summary": summary_default, "findings": []}

    return {
        "data_quality_flags": _list_field("data_quality_flags"),
        "macro_trends": _section("macro_trends", "Macro trend analysis unavailable."),
        "recovery_correlations": _section("recovery_correlations", "Recovery correlation analysis unavailable."),
        "sleep_correlations": _section("sleep_correlations", "Sleep correlation analysis unavailable."),
        "workout_correlations": _section("workout_correlations", "Workout correlation analysis unavailable."),
        "timing_patterns": _section("timing_patterns", "Timing pattern analysis unavailable."),
        "anomalies": _list_field("anomalies"),
        "goal_progress": _section("goal_progress", "Goal progress assessment unavailable."),
    }


def _normalize_dick_worker_payload(obj: dict[str, Any]) -> dict[str, Any]:
    hints = obj.get("session_struct_hint")
    if not isinstance(hints, list):
        hints = ["warmup_skills_specificity", "main_volume_progression"]
    hints_s = [str(h) for h in hints][:12]
    return {
        "session_struct_hint": hints_s,
        "summary": str(obj.get("summary") or "Training structure notes captured."),
    }


def _llm_route(settings: Settings, user_message: str, kw_workers: list[str], kw_meta: dict[str, Any]) -> dict[str, Any]:
    blob = contracts_prompt_blob("popeye")
    sys_msg = (
        f"{blob}\n"
        "You route user messages for delegation to stan (nutrition), dick (training), joy (risk monitoring).\n"
        'Respond ONLY with JSON: {"workers":["stan"|"dick"|"joy",...], '
        '"logging":{"should_log":false,"domain":"","payload":{},"source":"manual"}}.\n'
        "logging.should_log true only when the user is recording metrics (food, workout, vitals, weight).\n"
        "logging.source must be one of: manual, whoop, healthkit_export, wearable_auto, image_derived_local, barcode_local.\n"
        "Never include present_to_user or diagnosis language."
    )
    user_msg = json.dumps(
        {"user_message": user_message, "keyword_workers": kw_workers, "keyword_meta": kw_meta},
        ensure_ascii=False,
    )
    raw = chat_completion(
        settings,
        [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
        temperature=0.1,
    )
    return parse_llm_json_object(raw)


_STAN_SCHEMA = """{
  "data_quality_flags": [{"field":"string","issue":"string","severity":"low|medium|high"}],
  "macro_trends": {"summary":"string","vs_goals":"string","notable_patterns":[]},
  "recovery_correlations": {"summary":"string","findings":[]},
  "sleep_correlations": {"summary":"string","findings":[]},
  "workout_correlations": {"summary":"string","findings":[]},
  "timing_patterns": {"summary":"string","fasting_windows":[],"gaps":[]},
  "anomalies": [{"date":"string","type":"string","detail":"string"}],
  "goal_progress": {"summary":"string","on_track":true,"metrics":[]}
}"""

_STAN_SYSTEM_INSTRUCTIONS = (
    "You are Stan, the cross-referencing nutrition and biometric analyst.\n"
    "Your job:\n"
    "1. QA-validate any freshly ingested food entries in the context: check for missing macros, "
    "implausible calorie values, duplicate timestamps, or incomplete descriptions. "
    "Populate data_quality_flags for each issue found (severity: low/medium/high).\n"
    "2. Analyze food logs against all available biometric data (WHOOP recovery score, HRV, "
    "sleep hours, sleep performance, strain, workout output, Apple Health biometrics) "
    "to surface concrete correlations and patterns.\n"
    "3. Track macro and calorie trends against the user's stated goals.\n"
    "4. Identify meal timing patterns, fasting windows, and undereating gaps.\n"
    "5. Flag anomalies: missing log days, intake spikes, inconsistent data.\n"
    "6. Assess progress toward the user's health and body composition goals.\n"
    "Use all data in the context window. If a section lacks data, say so concisely in summary. "
    "Never speak to the user directly. Return ONLY valid JSON."
)


def _llm_worker(settings: Settings, worker: str, user_message: str, meta: dict[str, Any]) -> dict[str, Any]:
    blob = contracts_prompt_blob(worker)
    if worker == "joy":
        schema = '{"tier":"info|watch|urgent","summary":"string","signals":{}}'
        extra_instructions = "Keep summaries concise and non-diagnostic."
    elif worker == "stan":
        schema = _STAN_SCHEMA
        extra_instructions = _STAN_SYSTEM_INSTRUCTIONS
    else:
        schema = '{"session_struct_hint":["string"],"summary":"string"}'
        extra_instructions = "Keep summaries concise and non-diagnostic."
    sys_msg = (
        f"{blob}\n"
        "Respond ONLY with JSON matching this schema (no prose outside JSON):\n"
        f"{schema}\n"
        f"{extra_instructions}"
    )
    user_ctx = json.dumps({"user_message": user_message, "meta": meta}, ensure_ascii=False)

    def _call() -> dict[str, Any]:
        raw = chat_completion(
            settings,
            [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_ctx}],
            temperature=0.25,
        )
        return parse_llm_json_object(raw)

    try:
        obj = _call()
    except (json.JSONDecodeError, ValueError, KeyError):
        repair = chat_completion(
            settings,
            [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_ctx},
                {"role": "user", "content": "Your previous reply was not valid JSON. Output ONLY valid JSON now."},
            ],
            temperature=0.0,
        )
        obj = parse_llm_json_object(repair)

    if worker == "joy":
        return _normalize_joy_worker_payload(obj, meta)
    if worker == "stan":
        return _normalize_stan_worker_payload(obj)
    if worker == "dick":
        return _normalize_dick_worker_payload(obj)
    raise ValueError(worker)


def _llm_synthesize(
    settings: Settings,
    user_message: str,
    workers: list[str],
    joy_tier: str,
    payloads: dict[str, Any],
    data_entry_result: dict[str, Any] | None,
    insight_context: dict[str, Any] | None,
) -> str:
    blob = contracts_prompt_blob("popeye")

    # Extract Stan's latest snapshot from insight_context for non-nutrition turns
    stan_snapshot = None
    if isinstance(insight_context, dict):
        stan_snapshot = insight_context.get("stan_latest_snapshot")

    sys_msg = (
        f"{blob}\n"
        "You are Popeye, the sole voice to the user. Write cohesive markdown coaching guidance.\n"
        "Merge specialist structured outputs; cite Stan/Dick/Joy perspectives where relevant.\n"
        "Stay non-diagnostic; never claim definitive medical diagnoses.\n"
        "\n"
        "## Stan output handling\n"
        "Stan returns a multi-section JSON object. When Stan ran this turn (present in worker_payloads), "
        "use all relevant sections to ground your response:\n"
        "- ``data_quality_flags``: if severity is medium or high, gently flag the data issue to the user "
        "so they can correct their log entry.\n"
        "- ``macro_trends``: weave in trend insights and goal comparison where relevant.\n"
        "- ``recovery_correlations`` / ``sleep_correlations`` / ``workout_correlations``: surface the most "
        "actionable findings from whichever sections have non-empty findings lists.\n"
        "- ``timing_patterns``: mention notable fasting windows or meal gaps if they are significant.\n"
        "- ``anomalies``: flag anomalies the user should know about.\n"
        "- ``goal_progress``: reference on_track status and key metrics when discussing goals.\n"
        "When Stan did NOT run this turn but ``stan_latest_snapshot`` is present in context, "
        "draw on it to provide continuity of nutrition and biometric insights. "
        "Mention the snapshot is from a recent analysis, not the current message.\n"
        "\n"
        "If Joy tier is watch or urgent, weave in the corresponding Joy disclaimer markers "
        "([[JOY_WATCH_V1]] / [[JOY_URGENT_V1]]) explicitly.\n"
        "If Joy ran at info tier, ensure [[JOY_INFO_V1]] appears when discussing wearable-derived signals.\n"
        "When ``insight_context`` is present, use only its summaries and counts to ground coaching; "
        "do not invent specifics beyond that bounded context.\n"
        "Close with the coaching-not-diagnosis framing."
    )
    payload_blob = {
        "workers": workers,
        "joy_tier": joy_tier,
        "worker_payloads": payloads,
        "data_entry_result": data_entry_result,
        "insight_context": insight_context,
        "stan_latest_snapshot": stan_snapshot,
        "user_message": user_message,
    }
    raw = chat_completion(
        settings,
        [{"role": "system", "content": sys_msg}, {"role": "user", "content": json.dumps(payload_blob)}],
        temperature=0.35,
    )
    return raw.strip()


def _finalize_llm_reply(reply: str, joy_tier: str, workers: list[str]) -> str:
    prefix: list[str] = []
    markers = {
        "watch": "[[JOY_WATCH_V1]]",
        "urgent": "[[JOY_URGENT_V1]]",
        "info": "[[JOY_INFO_V1]]",
    }
    if joy_tier in ("watch", "urgent"):
        mk = markers[joy_tier]
        if mk not in reply:
            prefix.append(joy_body_for_tier(joy_tier))
    elif "joy" in workers:
        mk = markers["info"]
        if mk not in reply:
            prefix.append(joy_body_for_tier("info"))

    footer = "_Coaching synthesis only — not medical diagnosis or individualized treatment directives._"
    parts = [*prefix, reply]
    if footer not in reply:
        parts.append(footer)
    return "\n\n".join(parts)


class HealthOrchestrator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db: Database = get_db(settings)
        self.artifact_log = settings.resolved_artifact_log()

    def _merge_vision_into_message(
        self,
        user_message: str,
        *,
        images: list[tuple[str, bytes]] | None,
        conversation_context: list[dict[str, str]] | None,
    ) -> str:
        if not images:
            return user_message
        model_label = OPENROUTER_MODEL_ID
        if self.settings.openrouter_api_key:
            try:
                vision = describe_images_for_coaching(
                    self.settings,
                    user_text=user_message,
                    images=images,
                    conversation_context=conversation_context,
                )
            except Exception:
                vision = "[Image understanding failed; proceeding with user text only.]"
        else:
            vision = (
                "[Images attached; vision skipped — OpenRouter API key not configured. "
                "Describe images in text for full coaching.]"
            )
        caption = user_message.strip() or "(no text beyond images)"
        return f"Image understanding ({model_label}):\n{vision}\n\nUser message:\n{caption}"

    def run_chat_turn(
        self,
        user_message: str,
        *,
        images: list[tuple[str, bytes]] | None = None,
        conversation_context: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        effective = self._merge_vision_into_message(
            user_message,
            images=images,
            conversation_context=conversation_context,
        )
        conv_block = format_recent_conversation_block(conversation_context)
        if conv_block:
            effective = f"{conv_block}\nCurrent turn:\n{effective}"
        if self.settings.openrouter_api_key:
            try:
                return self._run_llm_turn(effective)
            except Exception:
                return self._run_stub_turn(effective)
        return self._run_stub_turn(effective)

    def _run_stub_turn(self, user_message: str) -> dict[str, Any]:
        root_task = new_id("task_")
        chain: list[str] = []
        events_for_guard: list[dict[str, Any]] = []

        workers, meta = classify_intents(user_message)
        insight_raw = fetch_data_entry_insight_context(self.settings)
        bounded_insight = _bounded_insight_context(insight_raw) if insight_raw is not None else None
        routing_meta: dict[str, Any] = {**meta}
        if bounded_insight is not None:
            routing_meta["data_context_available"] = True
            routing_meta["data_context"] = bounded_insight
        else:
            routing_meta["data_context_available"] = False
        joy_tier_final = "info"
        joy_templates_applied: list[str] = []

        with self.db.transaction() as cur:
            insert_agent_run(
                cur,
                root_task,
                "popeye",
                {
                    "phase": "classify_keyword",
                    "workers": workers,
                    "keyword_meta": meta,
                    "routing_meta": routing_meta,
                    "insight_context_meta": {
                        "available": bounded_insight is not None,
                        "bounded": bounded_insight,
                    },
                },
            )

        for w in workers:
            delegation = _delegate_event(
                root_task,
                w,
                f"delegate_to_{w}",
                {"user_message": user_message, "meta": routing_meta},
            )
            delegation["workflow_id"] = f"wf_{root_task}_{w}"

            validate_orchestration_event(delegation, enforce_invariant=True)
            with self.db.transaction() as cur:
                insert_delegation_event(cur, root_task, delegation)
            append_jsonl(self.artifact_log, delegation)
            chain.append(f"popeye -> {w} (delegate)")

            stub = run_worker_stub(w, user_message, routing_meta)
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
                insert_agent_run(
                    cur,
                    root_task,
                    w,
                    {
                        "phase": "stub_worker",
                        "stub_response": stub,
                        "data_context_available": routing_meta.get("data_context_available"),
                    },
                )
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

    def run_stan_snapshot(self, trigger_source: str) -> dict[str, Any]:
        """Run Stan's full cross-referencing analysis and persist the result as a snapshot.

        Called after data ingest events (food log, WHOOP sync, Apple Health import).
        Writes to ``stan_snapshots`` table so Popeye always has Stan's latest analysis
        in ``insight_context`` even on non-nutrition chat turns.
        """
        db_path = self.settings.resolved_sqlite()
        try:
            bootstrap_stan_snapshots(db_path)
        except Exception:
            pass

        insight = fetch_data_entry_insight_context(self.settings, days=30)
        if insight is None:
            return {"ok": False, "reason": "no_insight_context"}

        if not self.settings.openrouter_api_key:
            return {"ok": False, "reason": "no_llm_key"}

        try:
            payload = _llm_worker(
                self.settings,
                "stan",
                f"[stan_snapshot trigger={trigger_source}] Perform full cross-referencing analysis.",
                {"full_insight_context": insight, "trigger_source": trigger_source},
            )
        except Exception as exc:
            payload = run_worker_stub("stan", "", {})
            payload["_snapshot_error"] = str(exc)

        try:
            save_stan_snapshot(db_path, trigger_source, payload)
        except Exception:
            pass

        return {"ok": True, "trigger_source": trigger_source, "sections": list(payload.keys())}

    def _run_llm_turn(self, user_message: str) -> dict[str, Any]:
        root_task = new_id("task_")
        chain: list[str] = []
        events_for_guard: list[dict[str, Any]] = []

        kw_workers, kw_meta = classify_intents(user_message)
        insight_raw_llm = fetch_data_entry_insight_context(self.settings)
        bounded_insight_llm = (
            _bounded_insight_context(insight_raw_llm) if insight_raw_llm is not None else None
        )
        routing_meta_llm: dict[str, Any] = {**kw_meta}
        if bounded_insight_llm is not None:
            routing_meta_llm["data_context_available"] = True
            routing_meta_llm["data_context"] = bounded_insight_llm
        else:
            routing_meta_llm["data_context_available"] = False
        llm_workers_extra: list[str] = []
        logging_spec: dict[str, Any] = {"should_log": False}

        try:
            route = _llm_route(self.settings, user_message, kw_workers, kw_meta)
            llm_workers_extra = route.get("workers") if isinstance(route.get("workers"), list) else []
            log_obj = route.get("logging")
            if isinstance(log_obj, dict):
                logging_spec = log_obj
        except Exception:
            llm_workers_extra = []

        workers = merge_workers_keyword_llm(kw_workers, llm_workers_extra)

        with self.db.transaction() as cur:
            insert_agent_run(
                cur,
                root_task,
                "popeye",
                {
                    "phase": "llm_classify_hybrid",
                    "keyword_workers": kw_workers,
                    "merged_workers": workers,
                    "keyword_meta": kw_meta,
                    "routing_meta": routing_meta_llm,
                    "insight_context_meta": {
                        "available": bounded_insight_llm is not None,
                        "bounded": bounded_insight_llm,
                    },
                    "logging_spec": logging_spec,
                },
            )

        data_entry_result: dict[str, Any] | None = None
        if logging_spec.get("should_log") and logging_spec.get("domain"):
            svc = DataEntryService(self.settings)
            dom = str(logging_spec.get("domain") or "").strip()
            pay = logging_spec.get("payload") if isinstance(logging_spec.get("payload"), dict) else {}
            src = str(logging_spec.get("source") or "manual").strip().lower()
            _food_domains = {"food", "meal", "nutrition", "diet", "food_log"}
            client_conf: float | None = None
            if dom.lower() in _food_domains:
                dom = "food_log"
                client_conf = 1.0
            try:
                data_entry_result = svc.ingest(
                    domain=dom,
                    payload=pay,
                    source=src,
                    client_confidence=client_conf,
                )
            except ValueError as e:
                data_entry_result = {"status": "error", "detail": str(e)}

            de_payload = {
                "user_message": user_message,
                "logging_spec": logging_spec,
                "data_context_available": routing_meta_llm.get("data_context_available", False),
            }
            if bounded_insight_llm is not None:
                de_payload["data_context"] = bounded_insight_llm
            delegation = _delegate_event(
                root_task,
                "data-entry",
                "delegate_to_data_entry",
                de_payload,
            )
            delegation["workflow_id"] = f"wf_{root_task}_data_entry"
            validate_orchestration_event(delegation, enforce_invariant=True)
            with self.db.transaction() as cur:
                insert_delegation_event(cur, root_task, delegation)
                insert_agent_run(
                    cur,
                    root_task,
                    "data-entry",
                    {"phase": "data_entry_ingest", "logging_spec": logging_spec, "result": data_entry_result},
                )
            append_jsonl(self.artifact_log, delegation)
            chain.append("popeye -> data-entry (delegate)")

            rtn_de = _return_event(root_task, "data-entry", "data_entry_response", {"result": data_entry_result})
            validate_orchestration_event(rtn_de, enforce_invariant=True)
            events_for_guard.append(rtn_de)
            with self.db.transaction() as cur:
                insert_delegation_event(cur, root_task, rtn_de)
            append_jsonl(self.artifact_log, rtn_de)
            chain.append("data-entry -> popeye (structured return)")

            # Stan QA pass: validate the freshly ingested entry regardless of worker routing
            if data_entry_result and data_entry_result.get("status") != "error":
                _food_domains = {"food", "meal", "nutrition", "diet", "food_log"}
                if str(logging_spec.get("domain") or "").strip().lower() in _food_domains:
                    qa_ctx = {
                        "qa_mode": True,
                        "ingested_entry": {
                            "domain": logging_spec.get("domain"),
                            "payload": logging_spec.get("payload"),
                            "source": logging_spec.get("source"),
                        },
                        "data_entry_result": data_entry_result,
                    }
                    if insight_raw_llm is not None:
                        qa_ctx["full_insight_context"] = insight_raw_llm
                    try:
                        stan_qa_raw = _llm_worker(self.settings, "stan", user_message, qa_ctx)
                        stan_qa_flags = stan_qa_raw.get("data_quality_flags", [])
                    except Exception:
                        stan_qa_flags = []
                    data_entry_result["stan_qa_flags"] = stan_qa_flags
                    chain.append("popeye -> stan (qa_validate_food_entry)")
                    chain.append("stan -> popeye (data_quality_flags return)")

        worker_payloads: dict[str, Any] = {}
        joy_tier_final = "info"
        joy_templates_applied: list[str] = []

        for w in workers:
            wl_payload: dict[str, Any] = {
                "user_message": user_message,
                "meta": routing_meta_llm,
            }
            # Stan receives the full (unbounded) insight context for cross-referencing
            if w == "stan" and insight_raw_llm is not None:
                wl_payload["full_insight_context"] = insight_raw_llm
            delegation = _delegate_event(root_task, w, f"delegate_to_{w}", wl_payload)
            delegation["workflow_id"] = f"wf_{root_task}_{w}"

            validate_orchestration_event(delegation, enforce_invariant=True)
            with self.db.transaction() as cur:
                insert_delegation_event(cur, root_task, delegation)
            append_jsonl(self.artifact_log, delegation)
            chain.append(f"popeye -> {w} (delegate)")

            try:
                worker_meta = routing_meta_llm
                if w == "stan" and insight_raw_llm is not None:
                    worker_meta = {**routing_meta_llm, "full_insight_context": insight_raw_llm}
                stub = _llm_worker(self.settings, w, user_message, worker_meta)
            except Exception:
                stub = run_worker_stub(w, user_message, routing_meta_llm)

            worker_payloads[w] = stub
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
                insert_agent_run(
                    cur,
                    root_task,
                    w,
                    {
                        "phase": "llm_worker",
                        "structured_response": stub,
                        "data_context_available": routing_meta_llm.get("data_context_available"),
                    },
                )
            append_jsonl(self.artifact_log, rtn)
            chain.append(f"{w} -> popeye (structured return)")

        enforce_no_worker_present(events_for_guard)

        try:
            synth = _llm_synthesize(
                self.settings,
                user_message,
                workers,
                joy_tier_final,
                worker_payloads,
                data_entry_result,
                bounded_insight_llm,
            )
            synth = _finalize_llm_reply(synth, joy_tier_final, workers)
        except Exception:
            synth = synthesize_stub_reply(user_message, workers, joy_tier_final)

        tid = joy_template_id_for_tier(joy_tier_final)
        if tid:
            joy_templates_applied.append(tid)

        present = _present_event(root_task, synth, joy_templates_applied)
        validate_orchestration_event(present, enforce_invariant=True)
        with self.db.transaction() as cur:
            insert_delegation_event(cur, root_task, present)
            insert_agent_run(cur, root_task, "popeye", {"phase": "llm_synthesize"})
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
