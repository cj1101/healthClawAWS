#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="${1:?repo root}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${DIR}/curl-job.sh" "${REPO_ROOT}" /v1/jobs/raw-event-prune '{"dry_run":false}'
"${DIR}/curl-job.sh" "${REPO_ROOT}" /v1/jobs/delegation-prune '{"dry_run":false}'
