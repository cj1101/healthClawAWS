#!/usr/bin/env bash
set -euo pipefail

REPO=/home/ubuntu/healthClaw
DEPLOY_USER=ubuntu

echo "=== Rendering systemd units ==="

sed -e "s|@REPO_ROOT@|${REPO}|g" -e "s|@DEPLOY_USER@|${DEPLOY_USER}|g" \
  "${REPO}/deploy/ec2/systemd/nemoclaw-health.service.in" \
  | sudo tee /etc/systemd/system/nemoclaw-health.service > /dev/null

sed -e "s|@REPO_ROOT@|${REPO}|g" -e "s|@DEPLOY_USER@|${DEPLOY_USER}|g" \
  "${REPO}/deploy/ec2/systemd/nemoclaw-whoop-sync.service.in" \
  | sudo tee /etc/systemd/system/nemoclaw-whoop-sync.service > /dev/null

sed -e "s|@REPO_ROOT@|${REPO}|g" -e "s|@DEPLOY_USER@|${DEPLOY_USER}|g" \
  "${REPO}/deploy/ec2/systemd/nemoclaw-prune.service.in" \
  | sudo tee /etc/systemd/system/nemoclaw-prune.service > /dev/null

sudo cp "${REPO}/deploy/ec2/systemd/nemoclaw-whoop-sync.timer" /etc/systemd/system/
sudo cp "${REPO}/deploy/ec2/systemd/nemoclaw-prune.timer" /etc/systemd/system/

sudo chmod +x "${REPO}/deploy/ec2/scripts/curl-job.sh" \
              "${REPO}/deploy/ec2/scripts/prune-all.sh"

echo "=== Reloading systemd ==="
sudo systemctl daemon-reload

sudo systemctl enable nemoclaw-health.service
sudo systemctl enable nemoclaw-whoop-sync.timer nemoclaw-prune.timer
sudo systemctl start nemoclaw-whoop-sync.timer nemoclaw-prune.timer

echo "=== Starting nemoclaw-health ==="
sudo systemctl restart nemoclaw-health.service
sleep 4
sudo systemctl status nemoclaw-health.service --no-pager

echo ""
echo "=== Quick health check ==="
curl -s http://127.0.0.1:8000/healthz || echo "healthz not yet ready"
