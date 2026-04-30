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

## Wave D — production-lite on one Ubuntu EC2 (Nginx, systemd, timers)

Cheap single-node layout: **Nginx** → **uvicorn** (loopback `:8000`) → SQLite. **No HA**. Automated **WHOOP sync** and **retention prunes** use `systemd` timers calling `POST /v1/jobs/*` with **`NEMOWLAW_JOB_TOKEN`** (Bearer), so timers work even when `NEMOWLAW_DASHBOARD_PASSWORD` is set.

### Quick install

From the repo root on the instance (after `git clone` + `requirements.txt` exist):

```bash
chmod +x deploy/ec2/bootstrap.sh
./deploy/ec2/bootstrap.sh
```

Defaults: deploy root = repo root containing `deploy/ec2/`, service user `ubuntu`, nginx `server_name` `_`. Override:

- `NEMOWLAW_DEPLOY_ROOT` — alternate checkout path
- `NEMOWLAW_DEPLOY_USER` — non-`ubuntu` service account
- `NEMOWLAW_PUBLIC_HOSTNAME` — real hostname for `server_name` (recommended before TLS)
- `NEMOWLAW_INSTALL_FAIL2BAN=1` — install fail2ban

Configure secrets:

```bash
cp deploy/ec2/ec2.env.example .env
chmod 600 .env
# edit .env — set NEMOWLAW_DASHBOARD_PASSWORD, NEMOWLAW_JOB_TOKEN, WHOOP + OpenRouter vars
sudo systemctl restart nemoclaw-health
```

### TLS (choose one path)

**A — Let’s Encrypt + Nginx (default documented)**

1. Point a public DNS **A** record at the instance (Elastic IP recommended).
2. Set `NEMOWLAW_PUBLIC_HOSTNAME` and re-run the nginx site generation (or edit `/etc/nginx/sites-available/nemoclaw-health`), then `sudo nginx -t && sudo systemctl reload nginx`.
3. `sudo certbot --nginx -d your.hostname` — Certbot adds TLS to the site; renewals use the distro’s certbot timer.
4. Set `NEMOWLAW_WHOOP_REDIRECT_URI` to `https://your.hostname/v1/connectors/whoop/callback` and update the WHOOP developer app to match.

**B — Cloudflare Tunnel**

Skip opening **443** on the instance if you prefer: run `cloudflared` with a tunnel token, map a public hostname to `http://127.0.0.1:8000` (or to Nginx on 80). Use that hostname in **`NEMOWLAW_WHOOP_REDIRECT_URI`**. Nginx may still be used for `client_max_body_size` and static hardening, or you may point the tunnel directly at uvicorn for a minimal setup (then tune upload limits in the tunnel config if needed).

### Operations

| Task | Command |
|------|---------|
| API + dashboard logs | `journalctl -u nemoclaw-health -f` |
| WHOOP timer schedule | `systemctl list-timers nemoclaw-whoop-sync.timer` |
| Prune timer | `systemctl list-timers nemoclaw-prune.timer` |
| Health | `curl -sf http://127.0.0.1:8000/healthz` |
| Manual WHOOP job | `./deploy/ec2/scripts/curl-job.sh "$(pwd)" /v1/jobs/whoop-sync` |
| Manual prunes | `./deploy/ec2/scripts/prune-all.sh "$(pwd)"` |

Timers: **WHOOP** roughly every 6 hours (`02,08,14,20`); **prune** daily at **03:30** (raw-event + delegation). Adjust in `deploy/ec2/systemd/*.timer` then `sudo systemctl daemon-reload` + restart the timer.

### Firewall / SSH

`bootstrap.sh` enables **UFW**: allow **OpenSSH**, **80**, **443**. Prefer SSH keys; disable password auth when comfortable. Keep `.env` **`chmod 600`**.

### Python version

Target **Python 3.11+**. Ubuntu **24.04 LTS** ships a suitable `python3`. On **22.04**, install **3.11** (e.g. [deadsnakes](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa)) before `bootstrap.sh`, or edit the script to match your policy.

### Apple Health ZIP uploads

Nginx sets **`client_max_body_size 200m`**. If a larger export fails with **413**, raise this in the site config and reload Nginx.

Large `export.xml` inside the ZIP can take many minutes to ingest. Site templates set **`proxy_read_timeout`** / **`proxy_send_timeout`** to **3600s** so nginx does not return **504 Gateway Timeout** while uvicorn is still working. After editing the site file: **`sudo nginx -t && sudo systemctl reload nginx`**. If you terminate TLS or HTTP at an **AWS ALB** (or similar) in front of the instance, also raise that load balancer’s **idle timeout** so it is not shorter than the longest expected import.

### Rollback

```bash
sudo systemctl disable --now nemoclaw-whoop-sync.timer nemoclaw-prune.timer
sudo systemctl disable --now nemoclaw-health
sudo rm /etc/nginx/sites-enabled/nemoclaw-health
sudo systemctl reload nginx
```
