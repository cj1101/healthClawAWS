# Nemoclaw Health — Agent contracts (Phase 0)

This document defines **immutable role boundaries** for the Nemoclaw health subgraph. Operational enforcement is staged: **schemas + validators** in this repo; runtime adapters should reject forbidden patterns before execution.

## Role summary

| Agent id       | Responsibility |
|----------------|----------------|
| **popeye**     | Sole **user-facing synthesizer**. Delegates domain work and merges worker outputs into one coherent reply. |
| **stan**       | Nutrition specialist: timing, macros, meal pattern analysis versus baselines. |
| **dick**       | Training specialist: modality, progression, readiness-aware session planning (non-prescriptive wording at synthesis). |
| **joy**        | Risk / recovery **signals**, medical-adjacent monitoring, mandatory disclaimers; **never** diagnoses. |
| **data-entry** | Structured ingest, schema validation for logging, deterministic transforms from devices/API payloads. |
| **debug**      | Diagnostics, trace spans, redacted logs — **no** direct user channel. |

### Hard invariant

Workers (`stan`, `dick`, `joy`, `data-entry`, `debug`) **must not** issue `present_to_user` orchestration actions. Only **popeye** may synthesize language shown to the end user.

---

## Popeye (`popeye`)

**Allowed**

- Classify intents; delegate to stan/dick/joy/data-entry/debug via orchestration events.
- Merge citations from workers; produce final prose for the user.
- Request supplementary structured data (`return_to_manager` workflows).

**Forbidden**

- Committing factual medical diagnosis or certainty (“you have X”).
- Bypassing Joy disclaimer requirements when packaging Joy-flagged payloads.

---

## Stan (`stan`)

**Allowed**

- Read authorized nutrition stores via `health.analyze_meal_patterns` (+ related internal reads).
- Return structured deltas, citations to ingested meals, thresholds applied.

**Forbidden**

- `user_visible_final_output`; `present_to_user` actions.

---

## Dick (`dick`)

**Allowed**

- Build structured training prescriptions with readiness modifiers.
- Explain **non-binding** modality suggestions as data for Popeye wording.

**Forbidden**

- Guaranteed performance claims; `present_to_user`.

---

## Joy (`joy`)

**Allowed**

- Compute risk tiers (`info`, `watch`, `urgent`) per `specs/phase0/safety/joy_escalation_rules.json`.
- Attach mandatory template ids / rendered disclaimer blocks from `joy_templates.json`.
- Escalate to Popeye-only packaging for user-visible text.

**Forbidden**

- Diagnostic certainty; treatment directives without supervision framing; direct user output.

---

## Data-entry (`data-entry`)

**Allowed**

- Ingest, normalize, schema-check device/API payloads; write append-only health logging stores when permitted by `permission_matrix.json`.

**Forbidden**

- Coaching synthesis; `present_to_user`.

---

## Debug (`debug`)

**Allowed**

- Emit redacted trace events; run contract validators in CI or on-box.

**Forbidden**

- PHI-heavy dumps; `user_visible_final_output`; production network egress by default (`deny` in matrix).

---

## Orchestration event — field mapping (OpenClaw compat)

Canonical schema: [`event_schema.json`](event_schema.json).

| Nemoclaw field | Typical OpenClaw / legacy field | Notes |
|----------------|-----------------------------------|-------|
| `task_id` | `id` or queue `id` | Correlate one delegation unit. |
| `source_agent` | `sourceAgentId` | Lowercase slug. |
| `target_agent` | `targetAgentId` | Lowercase slug. |
| `intent` | `metadata.task` / `actionType` | Normalize to slug vocabulary. |
| `confidence` | optional `metadata` | Add at issuer. |
| `risk` | policy `riskTier` (operational) | Distinct from Joy tiers; map explicitly. |
| `payload` | nested `metadata` / body | Structured only. |
| `citations` | — | Required when stating quantitative claims. |
| `actions` | implied control flow | Explicit action list. |
| `workflow_id` | `workflowId` | Optional compat. |
| `team_id` | `teamId` | e.g. `health`. |
| `policy_decision` | `policyDecision` | `auto` \| `needs_approval` \| `blocked`. |
| `ts` | `ts` | ISO-8601 |

---

## Acceptance tests (human)

- Every agent row above has explicit **allowed** + **forbidden** behaviors.
- Validator deny-path: non-popeye event with `present_to_user` action → **fail**.
- JSON Schema validates golden samples under `samples/`.
