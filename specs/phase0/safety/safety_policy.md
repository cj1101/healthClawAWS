# Joy — deterministic safety policy (Phase 0)

Joy provides **recovery / vitality signal** summaries and tiered escalation. Joy is **not** a clinician: outputs are **informational** and **non-diagnostic**.

## Risk tiers

| Tier | Meaning | Required behavior |
|------|---------|-------------------|
| **info** | Routine monitoring; mild variance | Neutral framing; educational tone. |
| **watch** | Sustained or notable divergence | Mandatory disclaimer + contextual guidance toward professional care **when appropriate** (no coercion). |
| **urgent** | Severe or persistent red-line patterns | Strong disclaimer; urge immediate clinician / emergency pathways per escalation rules JSON; Popeye merges wording only after templates applied. |

Tiers map to deterministic rules in [`joy_escalation_rules.json`](joy_escalation_rules.json).

## Prohibited claims (representative patterns)

Forbidden **diagnostic certainty** language, including assertions that imply a definitive medical condition assignment from wearables/logs alone—e.g., “you are diagnosed”, “you have [condition]”, “this confirms”. Use probabilistic monitoring language.

Full machine patterns live in [`safety_regression_cases.json`](safety_regression_cases.json) `global_blocked_regex`.

## Escalation playbook (text)

- **info** — Log signals; surface context; no alarmist tone.
- **watch** — Include `JOY_WATCH_V1` template markers; suggest monitoring timeline; avoid treatment directives.
- **urgent** — Include `JOY_URGENT_V1` template markers; direct user to appropriate **in-person** or **emergency** care per rule text; never delay care with app-only advice.

## Audit

- Every `watch` / `urgent` user-visible path must record which template ids were attached (orchestration `payload.joy.templates_applied` recommended in later phases).
