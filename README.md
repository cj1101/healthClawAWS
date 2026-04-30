# Nemoclaw Health — `healthClaw` on AWS

Phase 0 **contracts**, **Joy safety artifacts**, and a **vendored** copy of the OpenClaw health stack live in this repo. Runtime targets an Ubuntu EC2 host; see [`docs/ec2-debug.md`](docs/ec2-debug.md).

## Layout

| Path | Purpose |
|------|---------|
| `specs/phase0/contracts/` | Agent contracts, event schema, permissions, tool registry |
| `specs/phase0/safety/` | Safety policy, Joy templates, escalation rules, regression cases |
| `vendor/openclaw-health/` | Imported OpenClaw `health-coach` skill + `workspace/agent-network` configs |
| `scripts/` | Validators |

## Phase 0 validation

```bash
npm install
npm run validate:phase0
```

## Python smoke (vendor skill)

From `vendor/openclaw-health/skills/health-coach/` (requires `pip install -r requirements.txt` in that folder if you extend it):

```bash
cd vendor/openclaw-health/skills/health-coach
python -m pytest tests/test_health_team_contracts.py -q
```

## Git remote

Canonical GitHub repo: [`https://github.com/cj1101/healthClaw.git`](https://github.com/cj1101/healthClaw.git)

```bash
git remote add origin https://github.com/cj1101/healthClaw.git
```
