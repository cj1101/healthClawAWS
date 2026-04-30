# Health Coach Skill

The health-coach skill is the core of the OpenClaw health platform.
It ingests WHOOP biometrics, food macros, calendar/task load, weather/AQI,
and qualitative notes into a SQLite database, then provides analytics and
a multi-agent coaching hierarchy.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `WHOOP_CLIENT_ID` | Yes | WHOOP API OAuth2 client ID |
| `WHOOP_CLIENT_SECRET` | Yes | WHOOP API OAuth2 client secret |
| `WHOOP_REDIRECT_URI` | No | Default: `http://localhost:8765/callback` |
| `WHOOP_API_BASE` | No | Default: `https://api.prod.whoop.com/developer` (v2 paths are `v2/...` under this base) |
| `WHOOP_AUTH_URL` | No | Default: `https://api.prod.whoop.com/oauth/oauth2/auth` — **not** under `/developer/`; same URL for API v2 |
| `WHOOP_TOKEN_URL` | No | Default: `https://api.prod.whoop.com/oauth/oauth2/token` |

## Storage

| Path | Description |
|------|-------------|
| `skills/health-coach/data/health_store.json` | Legacy JSON snapshot store (preserved) |
| `skills/health-coach/data/health.db` | SQLite canonical store (all streams) |
| `skills/health-coach/data/raw/` | Raw WHOOP API response dumps |
| `skills/health-coach/tokens/whoop_token.json` | WHOOP OAuth token |

## Agent Hierarchy

See `workspace/AGENTS.md § Health Platform` for the full role contract.

| Role | Script | Trigger |
|------|--------|---------|
| Manager | `goal_manager.py morning_synthesis` | Every morning brief |
| Nurse Monitor | `nurse_engine.py assess` | Called by morning_synthesis |
| Nutritionist | `nutritionist_engine.py analyze` | Called by morning_synthesis |
| Movement Specialist | `health_coach.py query "workout"` | On-demand only |

## Core Commands

### WHOOP + Coaching

```bash
# Full sync + today's snapshot
python health_coach.py sync_all [--days 7]

# Coaching outputs
python health_coach.py morning_brief [--proactive]
python health_coach.py midday_adjust [--proactive]
python health_coach.py evening_review [--proactive]
python health_coach.py weekly_review

# Ask a question
python health_coach.py query "should I train today?"

# WHOOP OAuth
python health_coach.py auth_start
python health_coach.py auth_finish --code <code>
python health_coach.py auth_status
python health_coach.py auth_refresh
```

### Manager / Goal Lifecycle

```bash
python goal_manager.py morning_synthesis [--date YYYY-MM-DD]
python goal_manager.py propose --title "..." --description "..." --rationale "..."
python goal_manager.py list [--status pending|approved|rejected|modified|all]
python goal_manager.py approve <id>
python goal_manager.py reject <id>
python goal_manager.py modify <id> --modification "new direction"
python goal_manager.py status <id>
python goal_manager.py note "qualitative text about physical state"
```

### Nurse Monitor

```bash
python nurse_engine.py assess [--date YYYY-MM-DD]
python nurse_engine.py report [--days 7]
```

### Nutritionist

```bash
python nutritionist_engine.py analyze [--date YYYY-MM-DD]
python nutritionist_engine.py report [--days 14]
```

### Weather + AQI

```bash
python weather_ingest.py fetch     # fetch Fort Greene, Brooklyn from Open-Meteo
python weather_ingest.py status    # latest stored record
```

### Backfill + DB

```bash
python backfill.py all             # one-time migration from JSON/CSV → SQLite
python backfill.py biometrics      # WHOOP data only
python backfill.py meals           # food tracker data only
python backfill.py status          # show DB row counts
```

## Thresholds

All scoring thresholds are in `thresholds.json`. Edit that file to tune behavior
without touching Python code. Key sections:

- `injury_risk` — nurse engine thresholds
- `nutrient_timing` — nutritionist thresholds
- `allostatic_load` — schedule load scaling weights
- `manager` — check-in intervals, streak triggers

## Goal Proposal Protocol

1. Agent runs `morning_synthesis` → checks `manager_flags`
2. If `FORCE_RECOVERY_PROPOSAL`: call `goal_manager.py propose` → present to Charles
3. Charles replies: `Approve <id>` / `Reject <id>` / `Modify <id> <text>`
4. Agent calls corresponding CLI command
5. **No goal is activated without explicit user approval**

## Notes

- All Python scripts are designed to run from `skills/health-coach/` directory.
- Import paths assume `health-coach/` is on `sys.path` or CWD.
- The `food-macro-tracker` skill's `macro_storage.py` has been patched to dual-write
  new meal entries to SQLite automatically on every `add_entry()` call.
- The `schedule_adapter.py` dual-writes allostatic scores to SQLite on every
  `load_schedule_day()` call.
