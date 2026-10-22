# Freshroots Demand Planner

Twice-weekly demand planning for Freshroots. The system **recommends**; planners **decide**.
It never releases a purchase order. See [CLAUDE.md](CLAUDE.md) for the business rules and invariants.

## Setup (local)
```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # .venv/bin/python on Linux
cp .env.example .env                                 # then fill in values
.venv/Scripts/alembic upgrade head                   # creates planner.db (SQLite) or migrates DATABASE_URL
```

## Run
```bash
python -m planner.ingest --dir data/seed --date 2026-10-19        # load SAP exports only
python -m planner.jobs.run --date 2026-10-19 --ingest-dir data/seed  # ingest + plan
pytest -q
```

`planner.jobs.run` exit codes: `0` done, `1` data error (nothing recommended), `2` the run for
that date already has planner decisions and was not replaced, `3` any other failure.
Re-running a date with no planner decisions replaces that run.

## Scheduling: Monday and Thursday at 06:00

Both wrappers load `.env`, ingest `SAP_INBOUND_DIR`, plan today's date and append to `logs/run.log`.
The SAP export must land in `SAP_INBOUND_DIR` before 06:00.

### Linux (cron)
```bash
chmod +x scripts/run_planning.sh
crontab -e
```
Add this line (server time zone must be Asia/Kolkata, or set `CRON_TZ=Asia/Kolkata` above it):
```
0 6 * * 1,4 /opt/freshroots_planner/scripts/run_planning.sh
```

### Windows (Task Scheduler)
From an administrator command prompt, with your install path:
```
schtasks /Create /TN "Freshroots planning run" /SC WEEKLY /D MON,THU /ST 06:00 /RU SYSTEM /TR "\"C:\freshroots_planner\scripts\run_planning.cmd\""
```
Check it with `schtasks /Query /TN "Freshroots planning run" /V`, and run it once now with
`schtasks /Run /TN "Freshroots planning run"`. A non-zero "Last Result" is the exit code above.

### Monitoring
A failed run exits non-zero and writes the reason to `logs/run.log`. The `planning_run` table keeps
every run, including failed ones with their data errors.
