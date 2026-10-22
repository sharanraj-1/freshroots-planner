"""Builds workbench.html from engine output so the page shows exactly what the engine computed."""
import json, os
from engine import run
HERE = os.path.dirname(os.path.abspath(__file__))
runs = {}
for basis in ("unadjusted", "adjusted"):
    rows, summary, issues, cfg = run(basis, "out" if basis == "unadjusted" else "out_adjusted")
    runs[basis] = {"rows": rows, "summary": summary}
checks = [
    "No open purchase orders or in-transit stock in the data. Cover counts on-hand stock only, so a twice-weekly run could re-order stock already on its way.",
    "No supplier or lead-time data. Every draft PO needs a vendor in Zoho, and the 6-week minimum is assumed to cover lead time.",
    "Two distributors are in Karnataka and Gujarat, outside the Maharashtra and Goa footprint in the brief. Confirm the master data.",
    "Sales are SKU-level only, so distributor-level shifts (the Wednesday drop in the ops brief) cannot be detected yet.",
    "Festival uplift uses the run month. A run in late September would not prepare stock for October.",
]
data = {"run_date": cfg["run_date"], "runs": runs, "checks": checks,
        "agent_prompt": open(os.path.join(HERE, "exception_agent_prompt.md")).read()}
html = open(os.path.join(HERE, "workbench_template.html")).read().replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
open(os.path.join(HERE, "..", "freshroots_workbench.html"), "w").write(html)
print("built", len(html), "bytes")
