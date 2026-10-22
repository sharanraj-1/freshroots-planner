"""The true state of the system, for GET /status and the CLI.

Each section is read on its own, so one broken part never hides the others.
"""

from datetime import date

from sqlalchemy import func, select

from planner import models as m
from planner.control import switches


def _section(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - status must report, never fail
        return {"error": f"{type(e).__name__}: {e}"}


def _switches(session):
    rows = switches.read_all(session)
    return [
        {
            "name": n,
            "enabled": bool(r and r.enabled),
            "missing": r is None,
            "updated_at": r.updated_at.isoformat(timespec="seconds") if r else None,
            "updated_by": r.updated_by if r else None,
            "reason": r.reason if r else "missing: treated as OFF",
        }
        for n, r in rows.items()
    ]


def _run_dict(r):
    return {
        "id": r.id,
        "run_date": r.run_date.isoformat(),
        "status": r.status,
        "finished_at": r.finished_at.isoformat(timespec="seconds") if r.finished_at else None,
        "summary": r.summary,
        "error": r.error,
        "hold_reasons": r.hold_reasons,
        "tripwires": r.tripwires,
    }


def _last_run(session):
    r = session.scalar(select(m.PlanningRun).order_by(m.PlanningRun.started_at.desc()).limit(1))
    return _run_dict(r) if r else None


def _held(session):
    return [
        _run_dict(r) for r in session.scalars(select(m.PlanningRun).where(m.PlanningRun.status == "held"))
    ]


def _freshness(session, cfg, today):
    inv = session.scalar(select(func.max(m.InventorySnapshot.as_of_date)))
    sales = session.scalar(select(func.max(m.WeeklySales.week_start)))
    f = cfg["data_freshness"]
    inv_age = (today - inv).days if inv else None
    sales_age = (today - sales).days if sales else None
    return {
        "today": today.isoformat(),
        "latest_inventory_date": inv.isoformat() if inv else None,
        "inventory_age_days": inv_age,
        "latest_sales_week_start": sales.isoformat() if sales else None,
        "sales_age_days": sales_age,
        "fresh": inv_age is not None
        and sales_age is not None
        and 0 <= inv_age <= f["max_inventory_age_days"]
        and sales_age <= f["max_sales_age_days"],
    }


def _alerts(session):
    return [
        {
            "id": a.id,
            "kind": a.kind,
            "severity": a.severity,
            "message": a.message,
            "run_id": a.run_id,
            "created_at": a.created_at.isoformat(timespec="seconds"),
            "emailed": a.emailed,
        }
        for a in session.scalars(
            select(m.Alert).where(m.Alert.resolved_at.is_(None)).order_by(m.Alert.id.desc()).limit(20)
        )
    ]


def status(session, cfg, today=None):
    today = today or date.today()
    out = {
        "switches": _section(lambda: _switches(session)),
        "last_run": _section(lambda: _last_run(session)),
        "held_runs": _section(lambda: _held(session)),
        "data_freshness": _section(lambda: _freshness(session, cfg, today)),
        "open_alerts": _section(lambda: _alerts(session)),
    }
    out["planner_enabled"] = switches.is_enabled(session, switches.PLANNER)
    return out
