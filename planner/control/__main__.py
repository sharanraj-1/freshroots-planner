"""Kill switches from the command line.

  python -m planner.control status
  python -m planner.control off <switch|all> --reason "..." [--user NAME]
  python -m planner.control on  <switch>     --reason "..." [--user NAME]
  python -m planner.control release <run_id> --reason "..." [--user NAME]

--user defaults to $PLANNER_USER, then the login name; it must be in config `switch_admins`.
"""

import argparse
import getpass
import json
import os
import sys

from planner import models as m
from planner.config import load_config
from planner.control import switches, tripwires
from planner.control.status import status
from planner.db import make_session_factory


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m planner.control", description="Planner kill switches")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("off", "on", "release"):
        p = sub.add_parser(name)
        p.add_argument("target", help="switch name, 'all' (off only), or a run id (release)")
        p.add_argument("--reason", required=True)
        p.add_argument("--user", default=os.environ.get("PLANNER_USER") or getpass.getuser())
    a = ap.parse_args(argv)
    cfg = load_config()

    with make_session_factory()() as session:
        try:
            if a.cmd == "status":
                print(json.dumps(status(session, cfg), indent=2, default=str))
                return 0
            if a.cmd == "release":
                run = session.get(m.PlanningRun, int(a.target))
                if run is None:
                    print(f"run {a.target} not found", file=sys.stderr)
                    return 1
                tripwires.release(session, run, a.reason, a.user, cfg)
                print(f"run {run.id} released by {a.user}")
                return 0
            if a.target == "all":
                if a.cmd != "off":
                    print("'on all' is not allowed: turn switches on one at a time", file=sys.stderr)
                    return 1
                switches.off_all(session, a.reason, a.user, cfg)
            else:
                switches.flip(session, a.target, a.cmd == "on", a.reason, a.user, cfg, source="cli")
        except switches.SwitchError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
        for row in switches.read_all(session).values():
            print(f"{row.name:24} {'ON ' if row.enabled else 'OFF'}  {row.updated_by}: {row.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
