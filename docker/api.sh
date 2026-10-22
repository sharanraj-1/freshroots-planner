#!/bin/sh
# API container: migrate, then serve the API and workbench.
set -e
/app/.venv/bin/alembic upgrade head
exec /app/.venv/bin/uvicorn planner.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers
