"""One planning run: (optionally) ingest the SAP exports, check the data, apply the rules, store
the run and one recommendation per SKU, and write the run files.

  out/recommendations.csv   one row per SKU, with the full calculation trace
  out/zoho_draft_po.csv     REORDER lines only, status Draft (never released)
  out/review_queue.json     structured payloads for the AI exception assistant
  out/run_log.json          data-quality checks, config used, summary

Usage:  python -m planner.jobs.run [--date YYYY-MM-DD] [--ingest-dir DIR] [--trend-basis B] [--out out]
        --date defaults to today; --ingest-dir defaults to $SAP_INBOUND_DIR (skip ingest if unset).

Exit codes: 0 done (or held by a tripwire), 1 data error (nothing recommended), 2 run locked by planner
decisions, 3 other failure, 4 paused by a kill switch.
"""

import argparse
import csv
import json
import logging
import os
import sys
from datetime import date, datetime

from sqlalchemy import delete, func, select

from planner import models as m
from planner.config import ROOT, load_config
from planner.control import switches, tripwires
from planner.db import make_engine, make_session_factory
from planner.ingest.load import dataset_for, ingest_folder
from planner.ingest.validate import errors, validate
from planner.integrations.zoho_csv import write_draft_po_csv
from planner.review.queue import build_review_queue, needs_review
from planner.rules.engine import evaluate

log = logging.getLogger("planner.run")

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
COMPARISON_FIELDS = ("decision", "recommended_qty", "trend_ratio", "high_demand", "review_flags")


class DataError(Exception):
    """The input data failed validation, so no recommendations were made."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("Run stopped - data errors:\n" + "\n".join(errors))


class RunLocked(Exception):
    """A run for this date already has planner decisions, so it cannot be replaced."""


class PlannerPaused(Exception):
    """A kill switch is off."""


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
    summary = summarise(rows)
    run_log = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "config": cfg,
        "festival_month": is_festival,
        "summary": summary,
        "data_checks": [i.as_dict() for i in issues],
    }
    with open(os.path.join(outdir, "run_log.json"), "w", encoding="utf-8") as f:
        json.dump(run_log, f, indent=2)
    return summary


def summarise(rows):
    summary = {d: sum(r["decision"] == d for r in rows) for d in ("REORDER", "PAUSE", "HOLD", "EXCEPTION")}
    summary["needs_review"] = sum(needs_review(r) for r in rows)
    summary["po_units"] = sum(r["recommended_qty"] for r in rows)
    return summary


def _clear_previous(session, run_date):
    """Delete an earlier run for run_date, unless a planner has already decided on it."""
    previous = session.scalar(select(m.PlanningRun).where(m.PlanningRun.run_date == run_date))
    if previous is None:
        return
    if previous.status == "held":
        raise RunLocked(f"run {previous.id} for {run_date} is HELD; an admin must release it first")
    rec_ids = select(m.Recommendation.id).where(m.Recommendation.run_id == previous.id)
    decided = session.scalar(
        select(func.count())
        .select_from(m.PlannerDecision)
        .where(m.PlannerDecision.recommendation_id.in_(rec_ids))
    )
    if decided:
        raise RunLocked(f"run {previous.id} for {run_date} has {decided} planner decision(s); not replaced")
    session.execute(delete(m.AiBrief).where(m.AiBrief.recommendation_id.in_(rec_ids)))
    session.delete(previous)
    session.flush()


def plan(session, run_date, cfg, outdir=None):
    """Plan one date from the database. Returns the stored PlanningRun.

    Raises DataError (a failed run is stored) or RunLocked (nothing changes).
    """
    run_date = date.fromisoformat(str(run_date))
    cfg = {**cfg, "run_date": run_date.isoformat()}
    if not switches.is_enabled(session, switches.PLANNER):
        raise PlannerPaused("planner_enabled is OFF: no run. Use the manual process (see RUNBOOK).")
    _clear_previous(session, run_date)
    run = m.PlanningRun(
        run_date=run_date,
        trend_basis=cfg["trend_test_basis"],
        status="running",
        started_at=datetime.now(),
        config=cfg,
        data_checks=[],
    )
    session.add(run)

    ds = dataset_for(session, run_date)
    issues = validate(ds, cfg, run_date)
    run.data_checks = [i.as_dict() for i in issues]
    if errors(issues):
        messages = [f"{i.file}: {i.message}" if i.file else i.message for i in errors(issues)]
        run.status, run.finished_at, run.error = "failed", datetime.now(), "\n".join(messages)
        session.commit()
        raise DataError(messages)

    rows, is_festival, _ = evaluate(cfg, ds)
    other = "adjusted" if cfg["trend_test_basis"] == "unadjusted" else "unadjusted"
    alt = {r["sku_id"]: r for r in evaluate({**cfg, "trend_test_basis": other}, ds)[0]}
    for r in rows:
        run.recommendations.append(
            m.Recommendation(
                sku_id=r["sku_id"],
                decision=r["decision"],
                recommended_qty=r["recommended_qty"],
                needs_review=needs_review(r),
                detail={
                    k: v for k, v in r.items() if k not in ("exception_reasons", "review_flags", "rule_trace")
                },
                review_flags=r["review_flags"],
                exception_reasons=r["exception_reasons"],
                rule_trace=r.get("rule_trace", []),
                comparison={"trend_test_basis": other}
                | {k: alt[r["sku_id"]].get(k) for k in COMPARISON_FIELDS},
            )
        )
    run.summary = write_outputs(rows, issues, cfg, is_festival, outdir) if outdir else summarise(rows)
    run.festival_month = is_festival
    run.status, run.finished_at = "completed", datetime.now()
    session.flush()
    tripwires.evaluate_run(session, run, cfg)  # may HOLD the run
    session.commit()
    return run


def plan_folder(folder, cfg, outdir=None):
    """Ingest a folder into a throwaway in-memory database and plan it (tests, what-if runs)."""
    from planner.models import Base

    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        switches.install_defaults(session)
        result = ingest_folder(session, folder, cfg, cfg["run_date"])
        if not result.loaded:
            raise DataError([f"{i.file}: {i.message}" for i in errors(result.issues)])
        run = plan(session, cfg["run_date"], cfg, outdir)
        return run, [_row(r) for r in run.recommendations]


def _row(rec):
    """A stored recommendation back in the engine's row shape."""
    return {
        **rec.detail,
        "exception_reasons": rec.exception_reasons,
        "review_flags": rec.review_flags,
        "rule_trace": rec.rule_trace,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run one Freshroots planning run.")
    ap.add_argument("--date", default=date.today().isoformat(), help="run date YYYY-MM-DD (default: today)")
    ap.add_argument("--ingest-dir", default=os.environ.get("SAP_INBOUND_DIR"), help="SAP export folder")
    ap.add_argument("--trend-basis", choices=["unadjusted", "adjusted"])
    ap.add_argument("--out", default="out", help="folder for the run files, relative to the repo root")
    ap.add_argument(
        "--scheduled", action="store_true", help="set by cron/Task Scheduler: obey scheduled_runs_enabled"
    )
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        cfg = load_config(run_date=a.date, trend_basis=a.trend_basis)
        with make_session_factory()() as session:
            needed = [switches.PLANNER] + ([switches.SCHEDULED_RUNS] if a.scheduled else [])
            off = [s for s in needed if not switches.is_enabled(session, s)]
            if off:
                raise PlannerPaused(f"{', '.join(off)} OFF (or unreadable): no ingest, no run")
            if a.ingest_dir:
                result = ingest_folder(session, a.ingest_dir, cfg, a.date)
                for i in result.issues:
                    log.log(
                        logging.ERROR if i.level == "error" else logging.WARNING,
                        "ingest %s: %s",
                        i.file,
                        i.message,
                    )
                if not result.loaded:
                    log.error("ingest batch %s rejected; run stopped, no recommendations", result.batch_id)
                    return 1
            run = plan(session, a.date, cfg, ROOT / a.out)
    except DataError as e:
        log.error("%s", e)
        return 1
    except RunLocked as e:
        log.error("%s", e)
        return 2
    except PlannerPaused as e:
        log.warning("%s", e)
        return 4
    except Exception:
        log.exception("planning run for %s failed", a.date)
        return 3
    log.info("run %s for %s %s: %s", run.id, run.run_date, run.status, json.dumps(run.summary))
    for reason in run.hold_reasons:
        log.warning("run HELD: %s", reason)
    for w in (c for c in run.data_checks if c["level"] == "warn"):
        log.warning("data check: %s", w["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
