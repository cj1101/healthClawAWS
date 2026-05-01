#!/usr/bin/env bash
# POST to /v1/jobs/* with Bearer NEMOWLAW_JOB_TOKEN from repo .env (bash-sourceable).
set -euo pipefail
REPO_ROOT="${1:?repo root}"
EP="${2:?path e.g. /v1/jobs/whoop-sync}"
if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${REPO_ROOT}/.env"
  set +a
fi
: "${NEMOWLAW_JOB_TOKEN:?set NEMOWLAW_JOB_TOKEN in ${REPO_ROOT}/.env}"
auth=( -H "Authorization: Bearer ${NEMOWLAW_JOB_TOKEN}" )
if [[ "${3-}" == "" ]]; then
  exec curl -fsS -X POST "${auth[@]}" "http://127.0.0.1:8000${EP}"
fi
exec curl -fsS -X POST "${auth[@]}" -H "Content-Type: application/json" -d "$3" "http://127.0.0.1:8000${EP}"
