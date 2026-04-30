# Vendored health stack — runtime entrypoints

Source: copied from OpenClaw into `vendor/openclaw-health/`.

## Primary application

| Component | Path | Notes |
|-----------|------|-------|
| Health orchestration CLI | `vendor/openclaw-health/skills/health-coach/health_coach.py` | `python health_coach.py --help`; subcommands include sync/coaching/agent flows and WHOOP `auth_*` |
| Engines | `nurse_engine.py` (Joy), `nutritionist_engine.py` (Stan), `trainer_engine.py` (Dick) | Used by Popeye-facing pipeline in `health_coach.py` |
| Contract-style tests | `vendor/openclaw-health/skills/health-coach/tests/test_health_team_contracts.py` | Domain behavior asserts |

## Agent network config (reference)

| File | Purpose |
|------|---------|
| `vendor/openclaw-health/workspace/agent-network/teams.v1.json` | Health team `popeye` + workers `stan`, `dick`, `joy` |
| `vendor/openclaw-health/workspace/agent-network/policy.v1.json` | Delegation risk tiers by action type |

Phase 0 adds **canonical** Nemoclaw contracts under `specs/phase0/`; production code should converge to those schemas over time (compat mapping in [`specs/phase0/contracts/agent_contracts.md`](../specs/phase0/contracts/agent_contracts.md)).
