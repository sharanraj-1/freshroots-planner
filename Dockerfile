# One image for the API and the scheduler. Python 3.12 on Debian slim.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Kolkata LOG_FORMAT=json
RUN apt-get update \
 && apt-get install -y --no-install-recommends cron tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# The same layout as a local install, so scripts/*.sh work unchanged (.venv/bin/python).
RUN python -m venv /app/.venv
COPY pyproject.toml README.md ./
COPY planner ./planner
RUN /app/.venv/bin/pip install --no-cache-dir ".[postgres]"
COPY config.json alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY docker ./docker
RUN chmod +x scripts/*.sh docker/*.sh && mkdir -p logs out

EXPOSE 8000
CMD ["docker/api.sh"]
