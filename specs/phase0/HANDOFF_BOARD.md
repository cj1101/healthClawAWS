# Phase 0 — Subagent Handoff Board

Copy either brief into parallel agent slots. Acceptance checks tie to **`npm run validate:phase0`**.

Both briefs consume or produce files under **`specs/phase0/`** only unless explicitly extending vendored runtime.

---

## Brief 1 — Contracts Lead (`architecture-contracts`)

**Mission.** Define Nemoclaw health-agent boundaries, orchestration JSON, tool visibility registry, permission model (Popeye sole user-visible synthesizer).

**Scope.**

- Canonical roles `popeye`, `stan`, `dick`, `joy`, `data-entry`, `debug`.
- JSON Schema for envelopes + permission matrix schema + starter tool registry populated with tool ids.

**Inputs.**

- Existing OpenClaw health behavior (vendored) under [`vendor/openclaw-health/`](../vendor/openclaw-health/README.md).
- User-visible boundary invariants for Popeye synthesis.

**Outputs (paths).**

- [`contracts/agent_contracts.md`](./contracts/agent_contracts.md)
- [`contracts/event_schema.json`](./contracts/event_schema.json)
- [`contracts/permission_matrix.json`](./contracts/permission_matrix.json) + [`contracts/permission_matrix.schema.json`](./contracts/permission_matrix.schema.json)
- [`contracts/tool_registry.schema.json`](./contracts/tool_registry.schema.json) + [`contracts/tool_registry.json`](./contracts/tool_registry.json)
- Golden + deny-path samples [`contracts/samples/*`](./contracts/samples/)

**Strict acceptance.**

1. `npm run validate:phase0` passes in CI/local.
2. Schema validates all samples flagged `expectSchemaValid: true`.
3. `03_denied_worker_present_to_user.json` manifests **semantic deny** (`present_to_user` issued by Joy).
4. Permission matrix completeness: **every permission row declares every Nemoclaw agent id**.

---

## Brief 2 — Safety Lead (`joy-safety`)

**Mission.** Encode deterministic Joy tiers, disclaimers, blocked diagnostic language, escalation rules.

**Scope.**

- Tiers `info` / `watch` / `urgent`.
- Disclaimer templates mirrored for blueprint compatibility [`joy_disclaimer_templates.json`](./safety/joy_disclaimer_templates.json).

**Inputs.**

- Watch/urgent must never ship diagnostic certainty wording.
- [`safety_policy.md`](./safety/safety_policy.md) playbook text.

**Outputs (paths).**

- [`safety/safety_policy.md`](phase0/safety/safety_policy.md)
- [`safety/joy_templates.json`](phase0/safety/joy_templates.json)
- [`safety/joy_disclaimer_templates.json`](phase0/safety/joy_disclaimer_templates.json)
- [`safety/joy_escalation_rules.json`](phase0/safety/joy_escalation_rules.json)
- [`safety/safety_regression_cases.json`](phase0/safety/safety_regression_cases.json)

**Strict acceptance.**

1. Regression suite distinguishes **positive** vs **failure** Joy strings via `expect_failure`.
2. `watch`/`urgent` positive cases retain required `[[JOY_*]]` markers.
3. Global regex blocklist fires on diagnostic certainty wording.
4. Escalation rules reference **existing** Joy template IDs.

---

## Operational note — EC2

See [`docs/ec2-debug.md`](../docs/ec2-debug.md).
