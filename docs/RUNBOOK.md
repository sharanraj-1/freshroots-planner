# Runbook – Freshroots demand planner

## Kill switches

| Switch | When OFF |
|---|---|
| `planner_enabled` | No ingest, no runs, no decisions. The workbench shows "Planner paused – use the manual process". |
| `scheduled_runs_enabled` | The Monday/Thursday scheduled run skips (exit code 4). Manual runs still work. |
| `ai_assistant_enabled` | The "Get AI brief" button is hidden and the brief endpoint refuses. |
| `zoho_api_enabled` | Draft POs are not sent to Zoho; the CSV export is the only route. Starts OFF. |
| `pre_po_check_enabled` | The pre-PO sales check does not run. |

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
