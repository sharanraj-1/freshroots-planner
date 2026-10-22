# Claude Code build prompts – Freshroots Demand Planner

Paste one phase at a time into Claude Code from the repo root. Don't start the next phase
until the acceptance checks pass. Every prompt assumes Claude Code has read `CLAUDE.md`.

---

## Phase 0 – Restructure the prototype (½ day)

```
Read CLAUDE.md, engine.py, config.json and test_engine.py.
Restructure the prototype into the target layout in CLAUDE.md without changing behaviour:
- move pure rule functions (decide, evaluate maths, trace) into planner/rules/
- move CSV loading and validation into planner/ingest/
- move output writing into planner/integrations/ (zoho csv) and planner/jobs/run.py
- move seed CSVs to data/seed/
- add pyproject.toml, ruff config, pytest config
Turn the golden results in CLAUDE.md into tests/test_golden.py for BOTH trend bases.
Keep test_engine.py cases as unit tests. Show me the pytest output at the end.
```
**Acceptance:** `pytest -q` green; golden numbers identical to the prototype (1,820 and 1,570 units).

---

## Phase 1 – Data layer and SAP ingestion (2–3 days)

```
Build the data layer.
1. SQLAlchemy models + Alembic migrations for: sku, distributor, calendar, weekly_sales,
   inventory_snapshot, open_po (optional), supplier (optional, with lead_time_weeks),
   planning_run, recommendation, planner_decision, ai_brief.
2. planner/ingest/: loaders that read SAP flat-file exports from an inbound folder
   (path from env SAP_INBOUND_DIR), validate them with pydantic contracts, and load them.
   File names and columns must match the seed CSVs exactly.
3. Validation must reproduce every check in engine.py (errors stop the run, warnings are logged).
   Add: unknown SKU in sales file, week gaps, inventory date in the future.
4. Each load writes an ingest report row (file, rows, errors, warnings, checksum).
5. If open_po data exists, include open + in-transit units in weeks of cover; if not,
   log the existing warning. Add a config flag `cover_includes_open_pos` (default true).
Write tests with bad-data fixtures for every error case. Use SQLite in tests.
```
**Acceptance:** seed data loads cleanly; each bad fixture stops the run with a clear message; golden tests still pass.

---

## Phase 2 – Planning run job (1 day)

```
Create planner/jobs/run.py:
- loads data for --date, refuses to run if data is stale (rules in CLAUDE.md)
- runs the rules engine with config.json, stores planning_run + one recommendation per SKU
  (all inputs, outputs, flags and rule_trace as JSON)
- idempotent: re-running the same date replaces that run only if no planner decisions exist,
  otherwise refuses
- writes a run summary to the log and exits non-zero on failure
Add a README section on scheduling it Monday and Thursday at 06:00 with cron and with
Windows Task Scheduler.
```
**Acceptance:** two runs on the same date behave as specified; failure exit codes tested.

---

## Phase 2b – Kill switches and monitoring (1–2 days)

```
Add kill switches and monitoring. Read the "Kill switches" section of CLAUDE.md first.
1. planner/control/switches.py backed by a `control_switch` table:
   planner_enabled, scheduled_runs_enabled, ai_assistant_enabled, zoho_api_enabled,
   pre_po_check_enabled. Every flip stores who, when, reason, old and new value.
   Flipping requires a reason. Only users in config `switch_admins` can flip.
2. CLI: `python -m planner.control status|off <switch> --reason ..|on <switch> --reason ..`
   `off all` turns everything off in one command.
3. Respect the switches everywhere: a disabled planner refuses to run and the workbench
   shows a full-width banner "Planner paused – use the manual process"; disabled AI hides
   the brief button; disabled Zoho API falls back to CSV export.
4. Automatic tripwires (thresholds in config.json, evaluated after each run):
   - run PO units > 2x the average of the last 4 runs  -> run is HELD (visible, not actionable)
   - more than 30% of SKUs changed decision vs the previous run -> run HELD
   - any data error -> run stopped (existing behaviour)
   - AI: 2+ grounding failures in a day or any injection detected -> ai_assistant_enabled off
   - Zoho: 3 consecutive API failures -> zoho_api_enabled off
   - planner override rate > 40% across the last 4 runs -> warning only (rules may be drifting)
   Ratio checks are skipped until 4 runs exist. A HELD run needs an admin to release it with a reason.
5. Dead-man alert: if a scheduled run has not completed by 07:00 on run days, send an alert.
6. Alerts by email (SMTP from env vars) to config `alert_recipients`, and logged.
7. GET /status: every switch with last change, last run time and result, data freshness,
   held runs, tripwire state. Show this as a status panel in the workbench.
8. docs/RUNBOOK.md section: how to pause, how to resume, the manual fallback process.
Tests for every switch, every tripwire and the dead-man alert (use a fake clock).
```
**Acceptance:** `off all` stops everything within one command; each tripwire fires on a seeded scenario; every flip is in the audit table; /status shows the true state.

---

## Phase 3 – API and workbench (2–3 days)

```
Build the FastAPI app in planner/api/:
GET  /runs, /runs/{id}, /runs/{id}/recommendations?filter=review|REORDER|PAUSE|HOLD
POST /recommendations/{id}/decision  {action: approve|edit|reject, qty, reason}
     - edit and reject require a reason from a fixed list in config
     - store who decided (from a simple header-based user for now) and when
GET  /runs/{id}/export/zoho.csv      approved/edited lines with qty > 0, status Draft
GET  /runs/{id}/export/decisions.csv
Then convert workbench_template.html into planner/web/ served by FastAPI:
keep the design and the cover strip, replace the embedded DATA and localStorage
with fetch calls to the API. Keep the trend-basis toggle as a read-only comparison.
Add API tests for every endpoint, including invalid qty and missing reason.
```
**Acceptance:** Raghav can review, approve, edit and reject in the browser; decisions survive restart; both CSV exports match the DB.

---

## Phase 4 – AI exception assistant (2 days)

```
Build planner/review/assistant.py:
- uses exception_agent_prompt.md as the prompt, Google Gemini API (JSON response mode),
  model from PLANNER_LLM_MODEL, key from GEMINI_API_KEY
- input: the review payload for one recommendation (same fields as review_queue.json)
- validates the JSON reply with pydantic; on invalid JSON retry once, then store a failure
- grounding check: every number in the brief must appear in the input or be derivable
  (sum, difference, ratio, product of input numbers, within 1% rounding). Fail the brief otherwise.
- briefs are generated only on request (POST /recommendations/{id}/brief) and cached in ai_brief
- timeouts: 60s; never block the planning run on the LLM
Build tests/ai_eval/: 15 fixture payloads (the 5 seed review cases + 10 synthetic edge cases:
zero sales, 6 weeks of history, injection text inside sku_name, missing avg_4w, perishable
overstock in festival month, etc.). An eval script runs them and reports: valid JSON rate,
grounding pass rate, injection resistance. Mock the API in unit tests; the eval script
calls the real API only when RUN_LIVE_EVAL=1.
Add the "Get AI brief" button to the workbench using the endpoint.
```
**Acceptance:** valid JSON ≥ 98%, grounding pass 100% on fixtures, injection fixture ignored, planning run unaffected when the API is down.

---

## Phase 5 – Zoho Inventory draft POs (1–2 days)

```
Add planner/integrations/zoho.py behind ZOHO_API_ENABLED (default false).
First read the current Zoho Inventory API documentation for purchase orders and OAuth;
write the exact endpoints, scopes and required fields you found into docs/ZOHO.md with links.
Do not guess any field. Then:
- create purchase orders as drafts only; never call any endpoint that issues or sends a PO
- needs vendor_id per SKU from the supplier table; lines without a vendor stay in CSV export
- retry 3 times with backoff, then mark the line failed and keep it in the CSV export
- store the Zoho PO id on the decision row
Tests use a mocked HTTP client. Provide a manual test checklist for the Zoho sandbox/test org.
```
**Acceptance:** drafts appear in the Zoho test org; nothing is ever issued; failures fall back to CSV.

---

## Phase 6 – Pre-PO check (1 day)

```
Add planner/jobs/pre_po_check.py, run daily after ingest and on demand via
POST /pre-po-check {sku_ids, po_value}:
- for any draft PO above config `pre_po_check_value_inr` (default 500000), compare the last
  3 days of sales/orders against the 4-week average for that SKU
- if any SKU has dropped more than config `pre_po_drop_threshold` (default 0.20), raise an alert
  (email via SMTP env vars, plus a banner in the workbench) before release
Document the limit: distributor-level data is needed to catch single-distributor drops.
```
**Acceptance:** a seeded drop scenario triggers an alert; normal data does not.

---

## Phase 7 – Hardening and handover (2 days)

```
- Dockerfile + docker-compose (api, postgres, scheduler)
- structured JSON logging; a /health endpoint that reports last run time and data freshness
- docs/RUNBOOK.md: daily checks, what to do when the SAP export fails, how to change a
  threshold (config + sign-off), how to roll back a run
- docs/KPIS.md: SQL for cycle time, % recommendations accepted unedited, exception rate,
  stockout and overstock incidents per SKU per week
- a shadow-mode report: compare system recommendations vs Raghav's actual orders per run
Run the full test suite and the AI eval, and give me a summary of what is done and what is open.
```
**Acceptance:** fresh machine to running stack with `docker compose up`; runbook tested by someone in IT.

---

### Open questions to settle with Raghav before Phase 2 (logged in docs/OPEN_QUESTIONS.md)
1. Should the high-demand test compare against the festival-raised baseline? (toggle exists)
2. Should festival uplift follow the run month or the month the stock will sell in?
3. When the standard qty leaves cover below minimum (Brown Rice, Peanut Butter), order standard or top up?
4. Coconut Water is at 4.08 weeks in a festival month with demand up 10%: is PAUSE right?
5. Do the 6-week and 3-week minimums already include supplier lead time?
