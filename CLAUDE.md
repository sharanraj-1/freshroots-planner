# Freshroots Demand Planner

Twice-weekly demand-planning system for Freshroots (regional FMCG, Maharashtra and Goa).
It replaces a manual monthly Excel process run by the Senior Demand Planner (Raghav).
The system **recommends**; humans **decide**. It never places an order on its own.

## What exists today (keep it working)
- `planner/rules/engine.py` – pure rule functions. Source of truth for the business logic.
- `planner/ingest/` – SAP export contracts (pydantic), validation, all-or-nothing DB load. See docs/DATA_CONTRACTS.md.
- `planner/jobs/run.py` – scheduled planning run: ingest, check, plan, store, tripwires. `jobs/deadman.py` – 07:00 check.
- `planner/control/` – kill switches, tripwires, alerts, status. CLI: `python -m planner.control`.
- `planner/api/` – FastAPI: runs, recommendations, decisions, exports, switches, /status.
- `planner/web/` – the workbench (vanilla JS, no build step), served by the API at `/`.
- `planner/integrations/zoho_csv.py` – Zoho draft-PO CSV. `planner/review/` – review payloads and AI prompt.
- `config.json` – every business threshold. No thresholds hard-coded anywhere else.
- `migrations/` – Alembic. `tests/` – unit, golden, API and control tests. `tests/golden/` holds
  the prototype's exact outputs. Must stay green.

## Business rules (from Raghav, signed off – do not change without a config change)
1. Baseline = mean of last 12 complete weeks. Trend = mean of last 4.
2. Festival months (calendar table, today Oct, Nov, Mar): baseline × 1.25 before cover checks.
3. High demand: trend > 1.20 × baseline → reorder qty = standard × 1.5.
4. Weeks of cover = stock / adjusted weekly demand.
5. Windows: Low perishability 6–14 weeks; High perishability 3–4 weeks.
6. Below min → REORDER. Above max → PAUSE. Inside → HOLD.
   High demand AND above max → EXCEPTION (planner decides).
7. `trend_test_basis` in config: `unadjusted` (default) or `adjusted`. Open question with Raghav.

Golden results on seed data (run_date 2026-10-19, basis unadjusted):
REORDER SKU001=750, SKU003=300, SKU004=150, SKU011=500, SKU013=120; PAUSE SKU006, SKU007;
8 HOLD; 0 EXCEPTION; 5 need review; 1,820 PO units. Basis `adjusted`: SKU001=500, total 1,570.

## Invariants – never violate
- Never release, approve or send a purchase order. Only create **Draft** POs.
- Never write to SAP. SAP is read-only, via scheduled flat-file exports.
- Stop the run (no recommendations) on any data error: missing SKU, negative sales,
  duplicate rows, inventory older than 1 day, sales data older than 13 days.
- Every recommendation stores its inputs and rule trace so it can be audited.
- The AI assistant is advisory, read-only and has no tools that write anything.
- Every number in an AI brief must exist in, or be derivable from, its input. Reject the brief otherwise.
- No secrets in code. Read keys from environment variables.

## Kill switches (must always work, even if other parts are broken)
- Switches: planner_enabled, scheduled_runs_enabled, ai_assistant_enabled, zoho_api_enabled,
  pre_po_check_enabled. Stored in the DB, flipped by CLI or admin, every flip audited with a reason.
- Every job and endpoint checks its switch first. A switch read failure means OFF (fail closed).
- Tripwires can turn switches off or HOLD a run automatically; only a human turns them back on.
- When the planner is off, the fallback is Raghav's manual Excel process (see RUNBOOK).
- GET /status always reports the true state of every switch, last run and data freshness.

## Stack (keep it simple – a 3-person IT team maintains this)
- Python 3.11+, FastAPI, SQLAlchemy, Postgres (SQLite allowed for local dev), pydantic for schemas.
- pytest. ruff for lint/format.
- Frontend: `planner/web/` (vanilla JS, no build step) reads from the API.
- Scheduling: one cron/Task Scheduler entry calling `python -m planner.jobs.run`.
- Docker Compose for deployment.
- LLM: Google Gemini API (not Anthropic), key from env `GEMINI_API_KEY`, model from env
  `PLANNER_LLM_MODEL`. Check current Gemini docs for model names. Any other external API:
  use a free tier, and ask the owner before adding a new key.
- Frontend design work: use the `design-taste-frontend` (tasteskill) skill.
- Zoho Inventory: CSV export first; API integration behind `ZOHO_API_ENABLED` flag.
  Verify every endpoint and field against current Zoho Inventory API docs. Do not guess.

## Target layout
```
planner/
  config/        settings + business-rule loading (config.json)
  ingest/        SAP flat-file loaders, validation, schema contracts
  rules/         pure engine functions (no I/O)
  review/        exception queue, AI assistant client, grounding checks
  integrations/  zoho export + api client
  api/           FastAPI routes
  jobs/          scheduled run, pre-PO check
  web/           workbench html/js
tests/           unit, golden, ai-eval fixtures
data/seed/       the seed CSVs
```

## Commands
- `pip install -e ".[dev]"` – install into the venv
- `pytest -q` – all tests; `ruff format . && ruff check .` – lint
- `alembic upgrade head` – create or migrate the database (DATABASE_URL, default SQLite planner.db)
- `python -m planner.jobs.run --date 2026-10-19 --ingest-dir data/seed` – ingest + one planning run
- `python -m planner.control status|off|on|release|rollback` – kill switches, held runs, rollback
- `python -m planner.jobs.pre_po_check`, `python -m planner.jobs.deadman`, `python -m planner.jobs.shadow_report`
- `RUN_LIVE_EVAL=1 python -m tests.ai_eval.run_eval` – live AI eval (costs a few cents)
- `docker compose up -d --build` – postgres + api + scheduler
- `uvicorn planner.api.main:app --reload` – local API + workbench

## Working style
- Small commits, one phase at a time. Run tests before saying a step is done.
- When a business rule is ambiguous, stop and write the question in `docs/OPEN_QUESTIONS.md`.
  Do not invent a rule.
- Prefer deleting code to adding abstractions.
