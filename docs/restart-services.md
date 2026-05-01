# Restart Nemoclaw services (EC2)

After code or `.env` changes, restart processes so they pick up updates. Development `uvicorn --reload` auto-reloads; production uses systemd + a manual Telegram bot.

## Health API (uvicorn on port 8000)

Managed by systemd:

```bash
sudo systemctl restart nemoclaw-health
sudo systemctl is-active nemoclaw-health   # expect: active
```

Logs:

```bash
journalctl -u nemoclaw-health -f
```

Unit: `/etc/systemd/system/nemoclaw-health.service` — `WorkingDirectory` is `runtime/`, env from `/home/ubuntu/healthClaw/.env`.

Quick HTTP check:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/docs
```

## Telegram bot

Often run manually (no systemd unit). From repo root:

```bash
cd /home/ubuntu/healthClaw
ps aux | grep telegram_bot | grep -v grep    # note PID
kill <PID>                                     # SIGTERM first; kill -KILL <PID> if stuck
nohup .venv/bin/python runtime/nemoclaw_health/telegram_bot.py \
  >> runtime/data/telegram_bot.log 2>&1 &
```

Logs:

```bash
tail -f runtime/data/telegram_bot.log
```

Do not paste raw log lines publicly — they may contain sensitive URLs.

## Optional next step

Add a `nemoclaw-telegram.service` unit (same pattern as `nemoclaw-health`) so you can `sudo systemctl restart nemoclaw-telegram` instead of `kill`/`nohup`.
