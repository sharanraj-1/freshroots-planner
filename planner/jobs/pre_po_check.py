"""Pre-PO check: before a big PO is released, make sure demand has not just dropped.

For each draft PO worth more than config `pre_po_check_value_inr`, every SKU on it is checked:
average daily orders over the last `pre_po_recent_days` days (from daily_orders.csv) against the
planner's 4-week average weekly sales / 7. A drop of more than `pre_po_drop_threshold` raises a
critical alert (email + workbench banner) before release.

A PO whose value cannot be worked out (no unit cost in supplier_master.csv) is checked anyway.

Limit: the data is per SKU, not per distributor. A drop at one distributor is hidden when the
others hold up; catching that needs a distributor column in the daily export.

Usage:  python -m planner.jobs.pre_po_check [--date YYYY-MM-DD]   (daily, after ingest)
Exit codes: 0 no drop, 1 drop alert raised, 4 switched off.
"""

import argparse
import logging
import sys
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select

from planner import models as m
from planner.config import load_config
from planner.control import switches
from planner.control.alerts import raise_alert
from planner.db import make_session_factory
from planner.logs import setup_logging

log = logging.getLogger("planner.pre_po")


class CheckOff(Exception):
    pass


def sku_drop(session, cfg, sku, as_of):
    """Compare the last N days of orders with the 4-week average. N days end the day before as_of."""
    n = cfg["pre_po_recent_days"]
    start = as_of - timedelta(days=n)
    days = session.execute(
        select(m.DailyOrders.order_date, m.DailyOrders.units_ordered).where(
            m.DailyOrders.sku_id == sku, m.DailyOrders.order_date >= start, m.DailyOrders.order_date < as_of
        )
    ).all()
    weeks = session.scalars(
        select(m.WeeklySales.units_sold)
        .where(m.WeeklySales.sku_id == sku, m.WeeklySales.week_start <= as_of - timedelta(days=7))
        .order_by(m.WeeklySales.week_start.desc())
        .limit(cfg["trend_weeks"])
    ).all()
    out = {"sku_id": sku, "recent_days": n, "recent_daily": None, "baseline_daily": None, "drop": None}
    if len(days) < n:
        return out | {
            "status": "no_data",
            "detail": f"only {len(days)} of the last {n} days of orders are loaded",
        }
    if len(weeks) < cfg["trend_weeks"] or not sum(weeks):
        return out | {"status": "no_data", "detail": "no 4-week sales average to compare with"}
    recent = sum(u for _, u in days) / n
    baseline = sum(weeks) / len(weeks) / 7
    drop = 1 - recent / baseline
    status = "dropped" if drop > cfg["pre_po_drop_threshold"] else "ok"
    return out | {
        "recent_daily": round(recent, 1),
        "baseline_daily": round(baseline, 1),
        "drop": round(drop, 3),
        "status": status,
        "detail": f"last {n} days {recent:.1f}/day vs 4-week average {baseline:.1f}/day ({-drop:+.0%})",
    }


def check_skus(session, cfg, sku_ids, as_of, po_label):
    if not switches.is_enabled(session, switches.PRE_PO_CHECK) or not switches.is_enabled(
        session, switches.PLANNER
    ):
        raise CheckOff("the pre-PO check is switched off")
    results = [sku_drop(session, cfg, s, as_of) for s in sku_ids]
    for r in results:
        if r["status"] == "dropped":
            raise_alert(
                session,
                cfg,
                "pre_po",
                f"Before releasing {po_label}: {r['sku_id']} orders dropped {r['drop']:.0%} "
                f"({r['detail']}). Check with sales before releasing the PO.",
                severity="critical",
                key=f"pre_po:{as_of}:{r['sku_id']}",
            )
        elif r["status"] == "no_data":
            log.warning("pre-PO check for %s on %s not possible: %s", po_label, r["sku_id"], r["detail"])
    return results


def draft_pos(session):
    """Draft POs from the latest completed run: approved lines grouped by vendor, with value if known."""
    run = session.scalar(
        select(m.PlanningRun)
        .where(m.PlanningRun.status == "completed")
        .order_by(m.PlanningRun.run_date.desc())
    )
    if run is None:
        return None, []
    suppliers = {s.sku_id: s for s in session.scalars(select(m.Supplier))}
    groups = defaultdict(lambda: {"skus": [], "value": 0.0, "value_known": True})
    for rec in run.recommendations:
        d = rec.decisions[-1] if rec.decisions else None
        if not d or d.action not in ("approve", "edit") or d.qty <= 0:
            continue
        s = suppliers.get(rec.sku_id)
        g = groups[(s.supplier_name if s else "vendor to be assigned", d.zoho_po_id)]
        g["skus"].append(rec.sku_id)
        if s and s.unit_cost_inr is not None:
            g["value"] += d.qty * s.unit_cost_inr
        else:
            g["value_known"] = False
    return run, [{"vendor": v, "zoho_po_id": po, **g} for (v, po), g in groups.items()]


def run_daily(session, cfg, as_of):
    run, pos = draft_pos(session)
    checked = []
    for po in pos:
        if po["value_known"] and po["value"] <= cfg["pre_po_check_value_inr"]:
            continue
        label = f"the {po['vendor']} PO from run {run.id}" + (
            f" (Zoho {po['zoho_po_id']})" if po["zoho_po_id"] else ""
        )
        value = f"INR {po['value']:,.0f}" if po["value_known"] else "value unknown"
        checked.append({**po, "results": check_skus(session, cfg, po["skus"], as_of, f"{label}, {value}")})
    return checked


def main(argv=None):
    ap = argparse.ArgumentParser(description="Daily pre-PO demand check")
    ap.add_argument("--date", default=date.today().isoformat())
    a = ap.parse_args(argv)
    setup_logging()
    with make_session_factory()() as session:
        try:
            checked = run_daily(session, load_config(), date.fromisoformat(a.date))
        except CheckOff as e:
            log.warning("%s", e)
            return 4
    dropped = [r for po in checked for r in po["results"] if r["status"] == "dropped"]
    log.info("pre-PO check: %d PO(s) above the threshold, %d SKU drop(s)", len(checked), len(dropped))
    return 1 if dropped else 0


if __name__ == "__main__":
    sys.exit(main())
