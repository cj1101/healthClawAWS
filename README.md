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

Open `http://localhost:8000/` for the dashboard (static UI). Set `NEMOWLAW_DASHBOARD_PASSWORD` to require sign-in for all `/v1/*` routes except `GET /healthz`, `POST /v1/auth/login`, `GET /v1/connectors/whoop/callback`, and **`POST /v1/jobs/*` when `Authorization: Bearer <NEMOWLAW_JOB_TOKEN>` matches** (for systemd / cron). When the dashboard password is enabled, you can also set **`NEMOWLAW_CHAT_BEARER_TOKEN`** on the API: **`POST /v1/chat`** then accepts **`Authorization: Bearer <NEMOWLAW_CHAT_BEARER_TOKEN>`** (matching value) and skips the session cookie, so automation such as the Telegram bridge can call chat without browser login. If the password is set and this token is unset or empty, `/v1/chat` behaves like other `/v1/*` routes and requires an authenticated session. Optional `NEMOWLAW_SESSION_SECRET` overrides the derived session signing key. Example EC2 env: [`deploy/ec2/ec2.env.example`](deploy/ec2/ec2.env.example).

**Session auth & Telegram (`runtime/nemoclaw_health/telegram_bot.py`):** the bot reads **`TELEGRAM_BOT_TOKEN`** (BotFather), **`TELEGRAM_ALLOWED_USER_IDS`** (comma-separated **numeric** user ids; `@username` is not supported—use `/start` with the bot or e.g. @userinfobot to learn your id), **`TELEGRAM_NEMOWLAW_API_BASE`** (default `http://127.0.0.1:8000`), and **`NEMOWLAW_CHAT_BEARER_TOKEN`** (must match the API’s value when `NEMOWLAW_DASHBOARD_PASSWORD` is set). On startup it calls **`setMyCommands`** so the `/` menu matches implemented commands (`/start`, `/help`, `/new`, `/summary`). Plain text forwards to **`POST /v1/chat`**; **`TELEGRAM_CHAT_HTTP_TIMEOUT_S`** (optional, default **900**) controls how long the bot waits — a single turn may chain several LLM requests. **Quick checks if the bot is silent:** ids are numeric and your account is listed; the bot process is actually running; the host can reach the API base (`curl` `GET /healthz` from the same machine); API and bot share the same `NEMOWLAW_CHAT_BEARER_TOKEN` when the dashboard password is on (a 401 from `/v1/chat` usually means missing/mismatched bearer or bearer not set on the API).

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
- Redirect URI **must match** [`NEMOWLAW_WHOOP_REDIRECT_URI`](runtime/nemoclaw_health/settings.py), e.g. `https://your-host/v1/connectors/whoop/callback`.
- Set `NEMOWLAW_WHOOP_CLIENT_ID`, `NEMOWLAW_WHOOP_CLIENT_SECRET`, `NEMOWLAW_WHOOP_REDIRECT_URI` (optional overrides: `NEMOWLAW_WHOOP_AUTH_URL`, `NEMOWLAW_WHOOP_TOKEN_URL`, `NEMOWLAW_WHOOP_API_BASE`, `NEMOWLAW_WHOOP_FETCH_ACTIVITY_DETAIL` to refetch each workout/sleep by id after listing).
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
