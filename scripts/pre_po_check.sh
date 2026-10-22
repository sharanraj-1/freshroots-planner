#!/bin/sh
# Daily pre-PO check (cron): load today's SAP exports, then check big draft POs for demand drops.
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
.venv/bin/python -m planner.ingest >> logs/pre_po.log 2>&1
exec .venv/bin/python -m planner.jobs.pre_po_check >> logs/pre_po.log 2>&1
