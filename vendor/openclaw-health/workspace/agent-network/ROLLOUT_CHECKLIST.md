# Koda Subagent Network Rollout Checklist

## Phase 0 - Preflight

- [ ] Confirm `workspace/agent-network/teams.v1.json` exists and all required teams/agents are enabled as intended.
- [ ] Confirm `workspace/agent-network/policy.v1.json` has expected autonomy rules.
- [ ] Run contract checks:
  - `node dashboard/scripts/agent-network-contract-check.mjs`

## Phase 1 - Observe Only

- [ ] Open Mission Control `Workflows` page and verify data loads.
- [ ] Confirm workflow timeline updates every ~3 seconds.
- [ ] Confirm communication graph edges appear for recent subagent activity.
- [ ] Confirm policy decisions are visible on delegation events.

## Phase 2 - Toggle Safety and Runtime State

- [ ] Toggle `coding` team OFF in Mission Control and verify new coding delegations fallback to Koda.
- [ ] Toggle `health` team OFF in Mission Control and verify health delegations fallback to Koda.
- [ ] Re-enable both teams and verify delegation resumes to managers.
- [ ] Verify startup state is `koda`, `cody`, and `popeye` ON while all worker subagents default OFF.
- [ ] Verify worker agents wake only when a manager delegation or approved runtime signal targets them.
- [ ] Verify worker agents return to OFF after task completion / idle timeout instead of remaining warm indefinitely.
- [ ] Verify health worker result files are written to `workspace/agent-network/results/` as `{agent}_{timestamp}.json`.

## Phase 3 - Policy Gating

- [ ] Validate low-risk coding action yields policy decision `auto`.
- [ ] Validate low-risk health analysis action yields policy decision `auto`.
- [ ] Validate external messaging action yields policy decision `needs_approval`.
- [ ] Validate blocked action class yields policy decision `blocked`.

## Phase 4 - Weekly Mission Hook

- [ ] Confirm cron job `koda-weekly-4am-000000000001` appears in `cron/jobs.json`.
- [ ] Confirm schedule is `0 4 * * 0` in timezone `America/New_York`.
- [ ] After first run, verify workflow timeline includes the weekly mission-improvement turn.

## Backout Plan

- [ ] Disable new weekly job by setting `enabled: false`.
- [ ] Disable teams from Mission Control toggles to route all work back to Koda.
- [ ] Keep policy file intact but set strict behavior by using approval-only rules if needed.
