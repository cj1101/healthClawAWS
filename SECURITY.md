# Security Policy

Nemoclaw Health handles sensitive health-adjacent data and deployment secrets. Please report security issues privately rather than opening a public GitHub issue.

## Reporting A Vulnerability

Email the repository owner or maintainer with:

- A short summary of the issue.
- The affected files, endpoints, or deployment steps.
- Reproduction steps, if safe to share.
- Whether any credentials, health data, or server access may be exposed.

Do not include real API keys, `.env` files, private health exports, database files, or SSH keys in the report.

## Sensitive Data Rules

- Never commit `.env`, `.pem`, SQLite databases, raw Apple Health exports, WHOOP tokens, Telegram bot tokens, OpenRouter keys, or log files containing personal data.
- Rotate any credential that may have been pasted into chat, logs, Git history, screenshots, or issues.
- Keep EC2 `.env` files restricted with `chmod 600`.
- Prefer HTTPS for public access, especially for WHOOP OAuth redirects.

## Supported Branch

Security fixes should target the default branch first. If release branches are added later, document their support windows here.
