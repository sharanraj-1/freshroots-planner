#!/bin/sh
# Dead-man check at the run deadline (cron): alerts if today's scheduled run has not completed.
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
exec .venv/bin/python -m planner.jobs.deadman >> logs/run.log 2>&1
