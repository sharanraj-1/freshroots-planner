"""Shadow mode: compare one run's recommendations with what Raghav actually ordered by hand.

  python -m planner.jobs.shadow_report --run-id 7 --actual raghav_orders_2026-10-19.csv

The actual-orders file has two columns, SKU_ID and Units_Ordered (one row per SKU ordered in that
cycle; a missing SKU means nothing was ordered). Writes out/shadow/run<id>.csv and .md.
"""

import argparse
import csv
import sys
from pathlib import Path

from planner import models as m
from planner.config import ROOT
from planner.db import make_session_factory


def read_actual(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if set(reader.fieldnames or []) != {"SKU_ID", "Units_Ordered"}:
            raise ValueError("actual orders file needs exactly the columns SKU_ID, Units_Ordered")
        return {r["SKU_ID"].strip(): int(r["Units_Ordered"]) for r in reader}


def compare(run, actual):
    rows = []
    for rec in run.recommendations:
        d = rec.decisions[-1] if rec.decisions else None
        got = actual.get(rec.sku_id, 0)
        rows.append(
            {
                "sku_id": rec.sku_id,
                "sku_name": rec.detail.get("sku_name", ""),
                "rule_decision": rec.decision,
                "rule_qty": rec.recommended_qty,
                "planner_qty": d.qty if d else None,
                "actual_qty": got,
                "difference": rec.recommended_qty - got,
                "agree": (rec.recommended_qty > 0) == (got > 0),
            }
        )
    unknown = sorted(set(actual) - {r["sku_id"] for r in rows})
    n = len(rows)
    summary = {
        "run_id": run.id,
        "run_date": str(run.run_date),
        "skus": n,
        "agreement_rate": sum(r["agree"] for r in rows) / n if n else 0.0,
        "rule_units": sum(r["rule_qty"] for r in rows),
        "actual_units": sum(r["actual_qty"] for r in rows),
        "mean_abs_difference": sum(abs(r["difference"]) for r in rows) / n if n else 0.0,
        "rule_ordered_planner_did_not": [
            r["sku_id"] for r in rows if r["rule_qty"] > 0 and r["actual_qty"] == 0
        ],
        "planner_ordered_rule_did_not": [
            r["sku_id"] for r in rows if r["rule_qty"] == 0 and r["actual_qty"] > 0
        ],
        "actual_skus_not_in_run": unknown,
    }
    return rows, summary


def write(rows, summary, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    base = outdir / f"run{summary['run_id']}"
    with open(base.with_suffix(".csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["sku_id"])
        w.writeheader()
        w.writerows(rows)
    lines = [
        f"# Shadow report: run {summary['run_id']} ({summary['run_date']})",
        "",
        f"- Order / no-order agreement: {summary['agreement_rate']:.0%} of {summary['skus']} SKUs",
        f"- Units: rules {summary['rule_units']:,}, Raghav {summary['actual_units']:,}",
        f"- Mean absolute difference per SKU: {summary['mean_abs_difference']:.0f} units",
        f"- Rules ordered, Raghav did not: {', '.join(summary['rule_ordered_planner_did_not']) or 'none'}",
        f"- Raghav ordered, rules did not: {', '.join(summary['planner_ordered_rule_did_not']) or 'none'}",
    ]
    if summary["actual_skus_not_in_run"]:
        lines.append(
            f"- In the actual orders but not in the run: {', '.join(summary['actual_skus_not_in_run'])}"
        )
    lines += ["", "| SKU | Rule | Rule qty | Raghav qty | Difference |", "|---|---|---:|---:|---:|"]
    lines += [
        f"| {r['sku_id']} | {r['rule_decision']} | {r['rule_qty']} | {r['actual_qty']} | {r['difference']} |"
        for r in rows
    ]
    base.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return base


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-id", type=int, required=True)
    ap.add_argument("--actual", required=True, help="CSV with SKU_ID, Units_Ordered")
    ap.add_argument("--out", default=str(ROOT / "out" / "shadow"))
    a = ap.parse_args(argv)
    with make_session_factory()() as session:
        run = session.get(m.PlanningRun, a.run_id)
        if run is None or run.status == "failed":
            print(f"run {a.run_id} not found or failed", file=sys.stderr)
            return 1
        rows, summary = compare(run, read_actual(a.actual))
    base = write(rows, summary, Path(a.out))
    print(
        f"agreement {summary['agreement_rate']:.0%}; rules {summary['rule_units']} units vs actual "
        f"{summary['actual_units']} -> {base}.md"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
