#!/usr/bin/env bash
# Run on the EC2/Linux host (repo checkout). Pulls latest from origin and restarts the API unit.
# Windows/laptop: use deploy/ec2/ec2-sync-and-restart.cmd instead (SSH + remote commands).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
git pull
sudo systemctl restart nemoclaw-health
