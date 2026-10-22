# Runbook – Freshroots demand planner

Who: Freshroots IT (3 people) and Raghav. Times are IST.

## Daily checks (5 minutes, before 09:00)
1. Open `http://<server>:8000/health`. `ok: true`; `data_freshness.fresh: true` on run days.
2. Open the workbench. The status panel shows every switch as expected, with no red alerts.
   On Monday and Thursday the last run is today's and is `completed` (or `held`, see below).
3. Check `logs/run.log` and `logs/pre_po.log` (Docker: `docker compose logs scheduler --since 24h`)
   for `"level": "ERROR"` or `"CRITICAL"`.
4. On run days, confirm Raghav has started reviewing. The KPI "cycle time" (docs/KPIS.md) shows
   how long decisions took.

## When the SAP export fails or is late
Symptoms: the run exits with code 1 and the log says `Run stopped - data errors`, or the
dead-man alert fires at 07:00, or `/health` shows stale data.
1. `python -m planner.control status` shows the last run's error and the data freshness.
   The `ingest_report` table lists every file in the batch: rows, checksum, errors.
2. **File missing or late.** Ask the SAP team to re-send to `SAP_INBOUND_DIR`, then run it by hand:
   `python -m planner.jobs.run --date <today> --ingest-dir <dir>` (Docker:
   `docker compose exec scheduler /app/scripts/run_planning.sh`).
3. **File rejected** (header mismatch, negative or duplicate rows, unknown SKU, stale stock).
   Nothing was loaded. Send the exact message to the SAP team; do not edit the export by hand.
   docs/DATA_CONTRACTS.md lists every rule.
4. If it cannot be fixed by 11:00 on a run day, use the manual process below for this cycle.

## Changing a threshold
Every business threshold is in `config.json`. It is Raghav's rule, so a change needs his sign-off.
1. Raghav agrees the change in writing (email or ticket). Record it in `docs/OPEN_QUESTIONS.md`
   if it settles a question there.
2. Edit `config.json` in git, in one commit whose message quotes the sign-off.
3. `pytest -q`. The golden tests pin the signed-off results, so a deliberate rule change also
   updates `tests/golden/` and `CLAUDE.md` in the same commit, after review.
4. Deploy (`git pull`; Docker: `docker compose up -d --build`). The next run stores the config it
   used in `planning_run.config`, so every run shows which rules applied.
A typo or an unknown key in `config.json` stops every job with a validation error (fail closed).

## Rolling back a run
Use this when a run went out with bad data or a bad rule, and decisions were already made on it.
1. Pause: `python -m planner.control off planner_enabled --reason "rolling back run <id>"`.
2. `python -m planner.control rollback <run_id> --reason "..."`. The run is kept for audit as
   `rolled_back`; nobody can act on it or export it.
3. If the command lists Zoho draft POs, delete those drafts in Zoho by hand. The planner never
   deletes anything in Zoho.
4. Fix the cause (data or config), then resume: `python -m planner.control on planner_enabled --reason "..."`.
5. Re-run the same date: `python -m planner.jobs.run --date <run date> --ingest-dir <dir>`.
A run with no decisions does not need a rollback: running the date again replaces it.

## Deployment (Docker)
```bash
cp .env.example .env        # set POSTGRES_PASSWORD, LLM and Zoho settings
docker compose up -d --build
docker compose ps           # api healthy, scheduler running
```
The API container runs `alembic upgrade head` on start. The scheduler container runs the cron
jobs in `docker/crontab`. SAP exports go in `./inbound`. Back up the `pgdata` volume daily, for
example with `docker compose exec postgres pg_dump -U planner planner > backup.sql`.

## Kill switches

| Switch | When OFF |
|---|---|
| `planner_enabled` | No ingest, no runs, no decisions. The workbench shows "Planner paused – use the manual process". |
| `scheduled_runs_enabled` | The Monday/Thursday scheduled run skips (exit code 4). Manual runs still work. |
| `ai_assistant_enabled` | The "Get AI brief" button is hidden and the brief endpoint refuses. |
| `zoho_api_enabled` | Draft POs are not sent to Zoho; the CSV export is the only route. Starts OFF. |
| `pre_po_check_enabled` | The daily pre-PO sales check and the on-demand check do not run. Release big POs only after checking recent orders by hand. |

A switch that is missing or cannot be read counts as **OFF**. Every flip needs a reason and is
stored in `switch_audit` (who, when, why, old and new value). Only users listed in `switch_admins`
in `config.json` can flip switches or release held runs.

### Pause
```bash
python -m planner.control off planner_enabled --reason "why" --user raghav
python -m planner.control off all --reason "why" --user raghav    # everything, one command
```
Or in the workbench status panel (admins only), or `POST /api/switches/{name}` with
`{"enabled": false, "reason": "..."}` and the `X-Planner-User` header.

### Resume
Switches come back on one at a time, only by a person:
```bash
python -m planner.control status
python -m planner.control on planner_enabled --reason "data fixed, checked run 41" --user raghav
```
`on all` is refused on purpose. Turn `zoho_api_enabled` on last, after checking a CSV export.

### Held runs
A tripwire HOLDs a run when its PO units are more than 2x the average of the last 4 runs, or when
more than 30% of SKUs changed decision since the previous run (ratio checks start after 4 runs).
A held run is visible but nobody can decide on it or export it. An admin checks the reason
(`python -m planner.control status`, or the workbench) and either releases it:
```bash
python -m planner.control release <run_id> --reason "Diwali stock-up confirmed with sales" --user raghav
```
or leaves it held and uses the manual process for that cycle.

### Automatic switch-offs
- 2 AI briefs failing the grounding check in one day, or any prompt injection: `ai_assistant_enabled` OFF.
- 3 Zoho API calls failing in a row: `zoho_api_enabled` OFF.
- Planner override rate above 40% over the last 4 runs: warning alert only.
Each one raises an alert. Find the cause before turning the switch back on.

### Dead-man alert
`scripts/deadman.sh` (or `.cmd`) runs at 07:00 on run days. If no run completed that morning it
raises a critical alert (email to `alert_recipients` when SMTP is configured, always in the log and
on the workbench). No alert when the planner or scheduled runs were switched off on purpose.

## Manual fallback process
When the planner is paused, ordering goes back to Raghav's Excel process. The planner keeps the
inputs he needs:
1. The latest SAP exports are in `SAP_INBOUND_DIR` as CSV files and open directly in Excel.
2. The last good run's decisions: workbench export, or `GET /api/runs/{id}/export/decisions.csv`.
3. Raise POs in Zoho by hand as before. Record each one so the next run can see it as an open PO
   once `open_po.csv` is part of the SAP export.

> To be completed by Raghav: the location of the Excel workbook, its tabs, and who signs off the
> POs while the planner is paused.
