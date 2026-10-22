"""Dead-man alert: on a run day, if no run has completed by the deadline, raise a critical alert.

Schedule it at the deadline (07:00, Monday and Thursday) next to the planning run:
  python -m planner.jobs.deadman
Exit code 0 when all is well (or no run was due), 1 when an alert was raised.
"""

import logging
import sys
from datetime import datetime, time

from sqlalchemy import select

from planner import models as m
from planner.config import load_config
from planner.control import switches
from planner.control.alerts import raise_alert
from planner.db import make_session_factory
from planner.logs import setup_logging

log = logging.getLogger("planner.deadman")


def check(session, cfg, now):
    """Returns the alert raised, or None. `now` is passed in so tests can use a fake clock."""
    sched = cfg["schedule"]
    if now.strftime("%a") not in sched["run_days"] or now.time() < time.fromisoformat(sched["deadline"]):
        return None
    # A switch someone deliberately turned off means no run is expected. A switch that cannot be
    # read is not a deliberate pause, so it does not silence the alarm.
    try:
        rows = switches.read_all(session)
        paused = [
            n
            for n in (switches.PLANNER, switches.SCHEDULED_RUNS)
            if rows[n] is not None and not rows[n].enabled
        ]
    except Exception:  # noqa: BLE001
        paused = []
    if paused:
        log.info("no run expected on %s: %s off", now.date(), ", ".join(paused))
        return None
    done = session.scalar(
        select(m.PlanningRun).where(
            m.PlanningRun.run_date == now.date(), m.PlanningRun.status.in_(("completed", "held"))
        )
    )
    if done:
        return None
    failed = session.scalar(select(m.PlanningRun).where(m.PlanningRun.run_date == now.date()))
    detail = f" The run failed: {failed.error}" if failed and failed.error else " No run was recorded."
    return raise_alert(
        session,
        cfg,
        "deadman",
        f"The scheduled planning run for {now.date()} had not completed by {sched['deadline']}.{detail} "
        "Check logs/run.log; if it cannot be fixed this morning, use the manual process (RUNBOOK).",
        severity="critical",
        key=f"deadman:{now.date()}",
    )


def main():
    setup_logging()
    with make_session_factory()() as session:
        alert = check(session, load_config(), datetime.now())
    return 1 if alert else 0


if __name__ == "__main__":
    sys.exit(main())
