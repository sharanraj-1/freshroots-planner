"""One planning run: load and check the data, apply the rules, write the outputs.

  out/recommendations.csv   one row per SKU, with the full calculation trace
  out/zoho_draft_po.csv     REORDER lines only, status Draft (never released)
  out/review_queue.json     structured payloads for the AI exception assistant
  out/run_log.json          data-quality checks, config used, summary

Usage:  python -m planner.jobs.run [--date 2026-10-19] [--trend-basis unadjusted|adjusted] [--out out]
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime

from planner.config import ROOT, load_config
from planner.ingest.sap_files import SEED_DIR, load_and_validate
from planner.integrations.zoho_csv import write_draft_po_csv
from planner.review.queue import build_review_queue, needs_review
from planner.rules.engine import evaluate

RECOMMENDATION_COLS = [
    "sku_id",
    "sku_name",
    "perishability",
    "stock_on_hand",
    "avg_12w",
    "avg_4w",
    "adjusted_weekly",
    "trend_ratio",
    "high_demand",
    "weeks_cover",
    "min_cover",
    "max_cover",
    "decision",
    "recommended_qty",
    "post_order_cover",
    "qty_to_min_cover",
    "needs_review",
    "notes",
]


class DataError(Exception):
    """The input data failed validation, so no recommendations were made."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("Run stopped - data errors:\n" + "\n".join(errors))


def write_outputs(rows, issues, cfg, is_festival, outdir):
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "recommendations.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RECOMMENDATION_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    **r,
                    "needs_review": needs_review(r),
                    "notes": " | ".join(r["exception_reasons"] + r["review_flags"]),
                }
            )
    write_draft_po_csv(rows, os.path.join(outdir, "zoho_draft_po.csv"))
    queue = build_review_queue(rows)
    with open(os.path.join(outdir, "review_queue.json"), "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)
    summary = {d: sum(r["decision"] == d for r in rows) for d in ("REORDER", "PAUSE", "HOLD", "EXCEPTION")}
    summary["needs_review"] = len(queue)
    summary["po_units"] = sum(r["recommended_qty"] for r in rows)
    log = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "config": cfg,
        "festival_month": is_festival,
        "summary": summary,
        "data_checks": [{"level": level, "message": m} for level, m in issues],
    }
    with open(os.path.join(outdir, "run_log.json"), "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)
    return summary


def run(run_date=None, trend_basis=None, outdir="out", data_dir=SEED_DIR, config_path=None):
    cfg = load_config(config_path, run_date, trend_basis)
    skus, dists, inv, cal, series, issues = load_and_validate(cfg, data_dir)
    errors = [m for level, m in issues if level == "error"]
    if errors:
        raise DataError(errors)
    rows, is_festival, _ = evaluate(cfg, skus, dists, inv, cal, series)
    summary = write_outputs(rows, issues, cfg, is_festival, ROOT / outdir)
    return rows, summary, issues, cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run one Freshroots planning run.")
    ap.add_argument("--date", help="run date YYYY-MM-DD (default: run_date in config.json)")
    ap.add_argument("--trend-basis", choices=["unadjusted", "adjusted"])
    ap.add_argument("--out", default="out", help="output folder, relative to the repo root")
    a = ap.parse_args(argv)
    try:
        rows, summary, _, _ = run(a.date, a.trend_basis, a.out)
    except DataError as e:
        print(e, file=sys.stderr)
        return 1
    print(json.dumps(summary))
    for r in rows:
        print(
            f"{r['sku_id']} {r['decision']:<9} qty={r['recommended_qty']:<4} cover={r.get('weeks_cover')}  "
            + "; ".join(r["exception_reasons"] + r["review_flags"])
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
