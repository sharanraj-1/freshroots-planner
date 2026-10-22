"""Automatic tripwires. Thresholds live in config.json under `tripwires`.

Run tripwires HOLD a run (visible, not actionable until an admin releases it). Integration tripwires
turn their switch off. Only a person turns anything back on.
"""

from datetime import datetime, time

from sqlalchemy import func, select

from planner import models as m
from planner.control import switches
from planner.control.alerts import raise_alert

FINISHED = ("completed", "held")


def _result(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def _previous_runs(session, run, limit):
    return session.scalars(
        select(m.PlanningRun)
        .where(m.PlanningRun.run_date < run.run_date, m.PlanningRun.status.in_(FINISHED))
        .order_by(m.PlanningRun.run_date.desc())
        .limit(limit)
    ).all()


def check_po_units(run, previous, tw):
    if len(previous) < tw["min_runs_for_ratio_checks"]:
        return _result("po_units_spike", "skipped", f"needs {tw['min_runs_for_ratio_checks']} earlier runs")
    avg = sum(p.summary["po_units"] for p in previous) / len(previous)
    units = run.summary["po_units"]
    limit = tw["po_units_vs_recent_avg_max"]
    if avg == 0:
        return _result("po_units_spike", "skipped", "earlier runs ordered nothing")
    if units > limit * avg:
        return _result(
            "po_units_spike",
            "tripped",
            f"PO units {units} are more than {limit}x the average of the last {len(previous)} runs "
            f"({avg:.0f})",
        )
    return _result("po_units_spike", "ok", f"PO units {units} vs average {avg:.0f}")


def check_decision_changes(run, previous, tw):
    if len(previous) < tw["min_runs_for_ratio_checks"]:
        return _result("decision_changes", "skipped", f"needs {tw['min_runs_for_ratio_checks']} earlier runs")
    before = {r.sku_id: r.decision for r in previous[0].recommendations}
    common = [r for r in run.recommendations if r.sku_id in before]
    if not common:
        return _result("decision_changes", "skipped", "no SKUs in common with the previous run")
    changed = sum(r.decision != before[r.sku_id] for r in common)
    share, limit = changed / len(common), tw["decision_change_max_share"]
    status = "tripped" if share > limit else "ok"
    return _result(
        "decision_changes",
        status,
        f"{changed} of {len(common)} SKUs ({share:.0%}) changed decision since {previous[0].run_date} "
        f"(limit {limit:.0%})",
    )


def check_override_rate(session, run, previous, tw):
    """Warning only: planners editing or rejecting a lot of lines suggests the rules are drifting."""
    runs = [p for p in previous][: tw["min_runs_for_ratio_checks"]]
    if len(runs) < tw["min_runs_for_ratio_checks"]:
        return _result("override_rate", "skipped", f"needs {tw['min_runs_for_ratio_checks']} earlier runs")
    decided = overridden = 0
    for p in runs:
        for rec in p.recommendations:
            if rec.decisions:
                decided += 1
                overridden += rec.decisions[-1].action != "approve"
    if not decided:
        return _result("override_rate", "skipped", "no planner decisions in the last runs")
    share, limit = overridden / decided, tw["override_rate_warn_share"]
    status = "warning" if share > limit else "ok"
    return _result(
        "override_rate",
        status,
        f"planners overrode {overridden} of {decided} decisions ({share:.0%}) over the last "
        f"{len(runs)} runs (warn above {limit:.0%})",
    )


def evaluate_run(session, run, cfg):
    """Run after the recommendations are stored. May set the run to HELD. Returns the results."""
    tw = cfg["tripwires"]
    previous = _previous_runs(session, run, tw["min_runs_for_ratio_checks"])
    results = [
        check_po_units(run, previous, tw),
        check_decision_changes(run, previous, tw),
        check_override_rate(session, run, previous, tw),
    ]
    run.tripwires = results
    tripped = [r for r in results if r["status"] == "tripped"]
    if tripped:
        run.status = "held"
        run.hold_reasons = [r["detail"] for r in tripped]
        session.flush()
        raise_alert(
            session,
            cfg,
            "tripwire",
            f"Run {run.id} for {run.run_date} is HELD: "
            + "; ".join(run.hold_reasons)
            + ". An admin must release it with a reason before anyone can act on it.",
            severity="critical",
            run_id=run.id,
        )
    for r in results:
        if r["status"] == "warning":
            session.flush()
            raise_alert(session, cfg, "tripwire", r["detail"], severity="warning", run_id=run.id)
    return results


def release(session, run, reason, user, cfg):
    if user not in cfg["switch_admins"]:
        raise switches.NotAdmin(f"'{user}' is not in switch_admins")
    if not (reason or "").strip():
        raise switches.SwitchError("a reason is required to release a held run")
    if run.status != "held":
        raise switches.SwitchError(f"run {run.id} is {run.status}, not held")
    run.status, run.released_by, run.released_at, run.release_reason = (
        "completed",
        user,
        datetime.now(),
        reason,
    )
    session.commit()
    return run


def _trip_switch(session, cfg, name, why):
    if switches.is_enabled(session, name):
        switches.flip(session, name, False, why, "tripwire", cfg, source="tripwire")
        raise_alert(session, cfg, "tripwire", f"{name} turned OFF automatically: {why}", severity="critical")
        return True
    return False


def check_ai(session, cfg, now=None):
    """Turn the AI assistant off after too many grounding failures today, or any injection."""
    now = now or datetime.now()
    since = datetime.combine(now.date(), time.min)

    def count(status):
        return session.scalar(
            select(func.count())
            .select_from(m.AiBrief)
            .where(m.AiBrief.status == status, m.AiBrief.created_at >= since)
        )

    if count("injection_detected"):
        return _trip_switch(session, cfg, switches.AI_ASSISTANT, "prompt injection detected in an AI brief")
    failures, limit = count("grounding_failed"), cfg["tripwires"]["ai_grounding_failures_per_day"]
    if failures >= limit:
        return _trip_switch(
            session,
            cfg,
            switches.AI_ASSISTANT,
            f"{failures} AI briefs failed the grounding check today (limit {limit})",
        )
    return False


def check_zoho(session, cfg):
    """Turn the Zoho API off after N consecutive failed calls."""
    n = cfg["tripwires"]["zoho_consecutive_failures"]
    last = session.scalars(
        select(m.IntegrationCall.ok)
        .where(m.IntegrationCall.system == "zoho")
        .order_by(m.IntegrationCall.id.desc())
        .limit(n)
    ).all()
    if len(last) == n and not any(last):
        return _trip_switch(session, cfg, switches.ZOHO_API, f"{n} consecutive Zoho API calls failed")
    return False
