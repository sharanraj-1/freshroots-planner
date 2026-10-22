"""Build the static workbench from engine output so the page shows exactly what the engine computed.

Usage:  python -m planner.web.build   (writes out/workbench.html)
"""

import json
from pathlib import Path

from planner.config import ROOT, load_config
from planner.jobs.run import plan_folder

SEED_DIR = ROOT / "data" / "seed"

HERE = Path(__file__).resolve().parent
CHECKS = [
    "No open purchase orders or in-transit stock in the data. Cover counts on-hand stock only, "
    "so a twice-weekly run could re-order stock already on its way.",
    "No supplier or lead-time data. Every draft PO needs a vendor in Zoho, "
    "and the 6-week minimum is assumed to cover lead time.",
    "Two distributors are in Karnataka and Gujarat, outside the Maharashtra and Goa footprint "
    "in the brief. Confirm the master data.",
    "Sales are SKU-level only, so distributor-level shifts (the Wednesday drop in the ops brief) "
    "cannot be detected yet.",
    "Festival uplift uses the run month. A run in late September would not prepare stock for October.",
]


def build(path=ROOT / "out" / "workbench.html"):
    runs = {}
    for basis in ("unadjusted", "adjusted"):
        cfg = load_config(trend_basis=basis)
        run, rows = plan_folder(SEED_DIR, cfg, ROOT / ("out" if basis == "unadjusted" else "out_adjusted"))
        runs[basis] = {"rows": rows, "summary": run.summary}
    data = {
        "run_date": cfg["run_date"],
        "runs": runs,
        "checks": CHECKS,
        "agent_prompt": (ROOT / "planner" / "review" / "exception_agent_prompt.md").read_text(
            encoding="utf-8"
        ),
    }
    template = (HERE / "workbench_template.html").read_text(encoding="utf-8")
    html = template.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
    Path(path).write_text(html, encoding="utf-8")
    return path, len(html)


if __name__ == "__main__":
    path, size = build()
    print("built", path, size, "bytes")
