#!/bin/sh
# Twice-weekly planning run (cron). Ingests $SAP_INBOUND_DIR, then plans today's date.
set -e
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
exec .venv/bin/python -m planner.jobs.run >> logs/run.log 2>&1
