#!/bin/sh
# Scheduler container: cron runs the same scripts as a plain server install.
# Cron jobs do not inherit the container environment, so it is written to /app/.env (mode 600),
# which scripts/*.sh load. Logs go to the logs volume and are also streamed to stdout.
set -e
umask 077
export -p | grep -E '^export (DATABASE_URL|SAP_INBOUND_DIR|PLANNER_|GEMINI_|OPENROUTER_|ZOHO_|SMTP_|LOG_FORMAT|TZ)' > /app/.env
crontab /app/docker/crontab
touch /app/logs/run.log /app/logs/pre_po.log
cron
exec tail -F /app/logs/run.log /app/logs/pre_po.log
