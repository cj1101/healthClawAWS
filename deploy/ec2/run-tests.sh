#!/usr/bin/env bash
cd /home/ubuntu/healthClaw
.venv/bin/python -m pytest tests/ -v --tb=short 2>&1
