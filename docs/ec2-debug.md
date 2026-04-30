## EC2 runtime & debugging (`healthClaw` on Ubuntu)

Canonical flow: **`GitHub ← → laptop ← → EC2`**. Prefer **committed** JSON contracts and reproducible **`npm run validate:phase0`** on both laptop and EC2.

### Prerequisites

- Git
- Node.js ≥ 18 (use [nvm](https://github.com/nvm-sh/nvm) on Ubuntu) for Phase 0 validators
- SSH key with correct permissions locally (do **not** commit `.pem`; path on Windows is yours only):

```powershell
ssh -i "C:\Users\charl\openclawKey.pem" ubuntu@ec2-44-200-84-118.compute-1.amazonaws.com
```

On Linux/macOS enforce key perms (`chmod 400 key.pem`) before SSH.

### One-time EC2 checkout

```bash
sudo apt update && sudo apt install -y git
mkdir -p ~/healthClaw && cd ~/healthClaw
git clone https://github.com/cj1101/healthClaw.git .
# or: clone into subdir then symlink
npm install
npm run validate:phase0
```

### Daily iteration loop

```bash
cd ~/healthClaw
git pull
npm ci
npm run validate:phase0
```

Python skill work (WHOOP ingestion, orchestration demos) expects **Python 3.11+**:

```bash
cd ~/healthClaw/vendor/openclaw-health/skills/health-coach
python3 -m venv .venv && source .venv/bin/activate
pip install python-dotenv httpx aiohttp cryptography  # add more as SKILL requires
python health_coach.py --help
```

### Debugging tips

1. **`validate:phase0` first** — fails fast when contracts regress (schemas, permission matrix completeness, Joy regression JSON).
2. **Logs** — if you integrate with OpenClaw’s `workflow-events.jsonl`-style drains in later phases, `tail -f` the configured path; Phase 0 is file-based only.
3. **Secrets on EC2** — keep `.env` and OAuth tokens outside git (see `.gitignore`); copy via `scp`/SSM, not pasted into repos.

See also [`docs/VENDOR_ENTRYPOINTS.md`](VENDOR_ENTRYPOINTS.md).
