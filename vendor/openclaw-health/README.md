# Vendored OpenClaw subset

Copied from local OpenClaw into this repository for Nemoclaw Health continuity.

## Contents

- `skills/health-coach/` — Python health orchestration (`health_coach.py`) and specialists (`nutritionist_engine`, `trainer_engine`, `nurse_engine`).
- `workspace/agent-network/` — `teams.v1.json`, `policy.v1.json`, related runtime/policy JSON.

Secrets and bulky raw device dumps are **ignored** via the repo-root `.gitignore` (`tokens/`, `data/raw/`).
