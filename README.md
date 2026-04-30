# Nemoclaw Health — `healthClaw` on AWS

Phase 0 **contracts**, **Joy safety artifacts**, and a **vendored** copy of the OpenClaw health stack live in this repo. Runtime targets an Ubuntu EC2 host; see [`docs/ec2-debug.md`](docs/ec2-debug.md). **Wave D (production-lite EC2):** [`deploy/ec2/bootstrap.sh`](deploy/ec2/bootstrap.sh), systemd timers, and Nginx — see the Wave D section in [`docs/ec2-debug.md`](docs/ec2-debug.md).

## Layout

| Path | Purpose |
|------|---------|
| `runtime/nemoclaw_health/` | FastAPI service: orchestrator, data plane, WHOOP / Apple connectors |
| `specs/phase0/contracts/` | Agent contracts, event schema, permissions, tool registry |
| `specs/phase0/safety/` | Safety policy, Joy templates, escalation rules, regression cases |
| `vendor/openclaw-health/` | Imported OpenClaw `health-coach` skill + `workspace/agent-network` configs |
| `scripts/` | Validators |

## Phase 0 validation

```bash
npm install
npm run validate:phase0
```

## Phase 1 + 2 runtime (FastAPI)

From repo root:

```bash
pip install -r requirements.txt
npm run validate:phase2    # Phase 0 JSON/AJV checks + pytest
```

Serve locally:

```bash
cd runtime
PYTHONPATH=. uvicorn nemoclaw_health.app:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/` for the dashboard (static UI). Set `NEMOWLAW_DASHBOARD_PASSWORD` to require sign-in for all `/v1/*` routes except `GET /healthz`, `POST /v1/auth/login`, `GET /v1/connectors/whoop/callback`, and **`POST /v1/jobs/*` when `Authorization: Bearer <NEMOWLAW_JOB_TOKEN>` matches** (for systemd / cron). Optional `NEMOWLAW_SESSION_SECRET` overrides the derived session signing key. Example EC2 env: [`deploy/ec2/ec2.env.example`](deploy/ec2/ec2.env.example).

| Area | Highlights |
|------|------------|
| Phase 1 | `/v1/chat`, data-entry, SQLite retention, contract validation |
| Phase 2 | WHOOP OAuth + sync (`/v1/connectors/whoop/*`), Apple Health export ZIP import (`/v1/connectors/apple-health/*`), cron alias `POST /v1/jobs/whoop-sync` |
| Phase 3 | Session auth (`NEMOWLAW_DASHBOARD_PASSWORD`), localhost dashboard at `/`, `/v1/profile`, `/v1/goals`, `/v1/timeline`, `/v1/debug/*` |

### Storage & retention

| Entity | Policy |
|--------|--------|
| `raw_events` | Default **90-day** prune (`NEMOWLAW_RAW_EVENT_RETENTION_DAYS`). Deletes matching rows in `evt_dyn_<slug>` when `provenance_json.dyn_row` links them so timelines stay aligned. |
| `connector_idempotency` | Rows referencing pruned `raw_events.id` are removed so connectors can re-ingest later windows if needed. |
| `delegation_events`, `agent_runs` | Optional prune via `NEMOWLAW_DELEGATION_METADATA_RETENTION_DAYS` (omit or `0` = disabled); never deletes goals/profile/`derived_summaries`. |
| SQLite durability | WAL journal mode + configurable busy timeout (`NEMOWLAW_SQLITE_BUSY_TIMEOUT_MS`). |
| Export | `POST /v1/storage/export-raw-jsonl` writes JSONL under `data_dir`; copy `nemoclaw.sqlite` separately for full backup. |

### WHOOP developer app

- Create or edit your OAuth app in the [WHOOP Developer Dashboard](https://developer-dashboard.whoop.com/apps).
- Redirect URI **must match** what WHOOP sends on the wire: either set [`NEMOWLAW_WHOOP_REDIRECT_URI`](runtime/nemoclaw_health/settings.py) to the exact registered URL (e.g. `https://your-host/v1/connectors/whoop/callback`), or **omit** it and use **Authorize** from the same origin you open in the browser (the API then derives `redirect_uri` from the request; behind a reverse proxy, set `X-Forwarded-Proto` / `X-Forwarded-Host`). The JSON from `GET /v1/connectors/whoop/authorize-url` includes `redirect_uri` so you can paste it into the [WHOOP Developer Dashboard](https://developer-dashboard.whoop.com/apps) if needed.
- Set `NEMOWLAW_WHOOP_CLIENT_ID`, `NEMOWLAW_WHOOP_CLIENT_SECRET`, and optionally `NEMOWLAW_WHOOP_REDIRECT_URI` (optional overrides: `NEMOWLAW_WHOOP_AUTH_URL`, `NEMOWLAW_WHOOP_TOKEN_URL`, `NEMOWLAW_WHOOP_API_BASE`, `NEMOWLAW_WHOOP_FETCH_ACTIVITY_DETAIL` to refetch each workout/sleep by id after listing).
- **Troubleshooting:** If “Open authorize URL” shows nothing useful, check Integrations: `503` from `/v1/connectors/whoop/authorize-url` means missing or invalid WHOOP env vars; if the browser blocks pop-ups, use **Copy authorize URL** or the same-tab link shown after the click.

Flow: `GET /v1/connectors/whoop/authorize-url` → browser → `GET /v1/connectors/whoop/callback` → `POST /v1/connectors/whoop/sync`.

### Apple Health export (Phase 2)

On iPhone: Health → profile → Export All Health Data → use the decrypted export that contains **`apple_health_export/export.xml`** (usually packaged as a ZIP). Upload that ZIP to `POST /v1/connectors/apple-health/import` (multipart field `file`). Re-importing the same data dedupes on stable keys / metadata sync IDs.

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
