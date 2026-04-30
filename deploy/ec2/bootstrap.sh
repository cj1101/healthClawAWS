#!/usr/bin/env bash
#
# Wave D: idempotent single-node Ubuntu setup for Nemoclaw Health.
# Run from the repo checkout with sudo available.
#
# Usage:
#   ./deploy/ec2/bootstrap.sh
# Optional overrides:
#   NEMOWLAW_DEPLOY_ROOT  — repo root (default: parent of deploy/ec2)
#   NEMOWLAW_DEPLOY_USER  — systemd service user (default: ubuntu)
#   NEMOWLAW_PUBLIC_HOSTNAME — server_name for nginx (default: _)
#   NEMOWLAW_INSTALL_FAIL2BAN=1 — install fail2ban
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DEPLOY_ROOT="${NEMOWLAW_DEPLOY_ROOT:-$DEFAULT_ROOT}"
DEPLOY_USER="${NEMOWLAW_DEPLOY_USER:-ubuntu}"
PUBLIC_HOSTNAME="${NEMOWLAW_PUBLIC_HOSTNAME:-_}"

umask 022

if [[ ! -d "$DEPLOY_ROOT" ]]; then
  echo "Deploy root does not exist: $DEPLOY_ROOT"
  exit 1
fi

if [[ ! -f "${DEPLOY_ROOT}/requirements.txt" ]]; then
  echo "No requirements.txt under $DEPLOY_ROOT — is this the healthClaw repo root?"
  exit 1
fi

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  nginx \
  curl \
  ufw \
  certbot \
  python3-certbot-nginx

# Python 3.11+ (prefer 3.11/3.12 from the image; Ubuntu 22.04 ships 3.10 — add deadsnakes if needed)
if command -v python3.12 &>/dev/null; then
  PY=python3.12
elif command -v python3.11 &>/dev/null; then
  PY=python3.11
else
  sudo apt-get install -y --no-install-recommends python3 python3-venv python3-pip
  PY=python3
fi

# Optional hardening
if [[ "${NEMOWLAW_INSTALL_FAIL2BAN:-}" == "1" ]]; then
  sudo apt-get install -y --no-install-recommends fail2ban
fi

sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable || true

PYTHON="${DEPLOY_ROOT}/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  "$PY" -m venv "${DEPLOY_ROOT}/.venv"
fi
"${DEPLOY_ROOT}/.venv/bin/pip" install -U pip
"${DEPLOY_ROOT}/.venv/bin/pip" install -r "${DEPLOY_ROOT}/requirements.txt"

replace_tokens() {
  sed -e "s|@REPO_ROOT@|${DEPLOY_ROOT}|g" -e "s|@DEPLOY_USER@|${DEPLOY_USER}|g" "$1"
}

SD_SRC="${DEPLOY_ROOT}/deploy/ec2/systemd"
for unit in nemoclaw-health.service nemoclaw-whoop-sync.service nemoclaw-prune.service; do
  replace_tokens "${SD_SRC}/${unit}.in" | sudo tee "/etc/systemd/system/${unit}" > /dev/null
done
sudo cp "${SD_SRC}/nemoclaw-whoop-sync.timer" /etc/systemd/system/
sudo cp "${SD_SRC}/nemoclaw-prune.timer" /etc/systemd/system/

sudo chmod +x "${DEPLOY_ROOT}/deploy/ec2/scripts/curl-job.sh"
sudo chmod +x "${DEPLOY_ROOT}/deploy/ec2/scripts/prune-all.sh"

replace_tokens "${DEPLOY_ROOT}/deploy/ec2/nginx/nemoclaw-health.conf.in" \
  | sed -e "s|@SERVER_NAME@|${PUBLIC_HOSTNAME}|g" \
  | sudo tee /etc/nginx/sites-available/nemoclaw-health > /dev/null
sudo ln -sf /etc/nginx/sites-available/nemoclaw-health /etc/nginx/sites-enabled/nemoclaw-health
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable nemoclaw-health.service
sudo systemctl restart nemoclaw-health.service
sudo systemctl enable nemoclaw-whoop-sync.timer nemoclaw-prune.timer
sudo systemctl start nemoclaw-whoop-sync.timer nemoclaw-prune.timer
sudo systemctl restart nginx

echo ""
echo "Bootstrap complete."
echo "- Copy deploy/ec2/ec2.env.example to ${DEPLOY_ROOT}/.env and chmod 600."
echo "- Set NEMOWLAW_JOB_TOKEN for systemd timers (see docs/ec2-debug.md)."
echo "- After DNS A record points here: sudo certbot --nginx -d YOUR_DOMAIN"
echo "- journalctl -u nemoclaw-health -f"
