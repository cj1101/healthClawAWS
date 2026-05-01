#!/usr/bin/env bash
# Upload (or create) a single AWS Secrets Manager secret whose value is the raw .env file contents.
#
# Prerequisites: AWS CLI v2, credentials with secretsmanager:CreateSecret / PutSecretValue / DescribeSecret.
#
# Usage (from repo root on a machine that has your .env):
#   export AWS_REGION=us-east-1
#   export AWS_SECRETS_MANAGER_SECRET_ID=healthClaw/prod/env
#   chmod +x deploy/ec2/scripts/upload-env-to-secrets-manager.sh
#   ./deploy/ec2/scripts/upload-env-to-secrets-manager.sh /path/to/.env
#
# On EC2, load into a file before restart (example; grant the instance role GetSecretValue on this secret):
#   aws secretsmanager get-secret-value --region "$AWS_REGION" --secret-id "$AWS_SECRETS_MANAGER_SECRET_ID" \
#     --query SecretString --output text > ~/healthClaw/.env && chmod 600 ~/healthClaw/.env
#   sudo systemctl restart nemoclaw-health nemoclaw-telegram-bot
#
set -euo pipefail

SECRET_ID="${AWS_SECRETS_MANAGER_SECRET_ID:?Set AWS_SECRETS_MANAGER_SECRET_ID (e.g. healthClaw/prod/env)}"
REGION="${AWS_REGION:-us-east-1}"
ENV_FILE="${1:-.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "File not found: $ENV_FILE" >&2
  exit 1
fi

ENV_ABS="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"

if aws secretsmanager describe-secret --region "$REGION" --secret-id "$SECRET_ID" &>/dev/null; then
  aws secretsmanager put-secret-value \
    --region "$REGION" \
    --secret-id "$SECRET_ID" \
    --secret-string "file://${ENV_ABS}"
  echo "Updated secret: $SECRET_ID"
else
  aws secretsmanager create-secret \
    --region "$REGION" \
    --name "$SECRET_ID" \
    --secret-string "file://${ENV_ABS}"
  echo "Created secret: $SECRET_ID"
fi
