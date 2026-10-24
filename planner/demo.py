"""Demo mode for serverless hosting (Vercel): a throwaway SQLite database seeded from data/seed.

Every fresh instance starts from the seed run, so decisions last only as long as that instance.
For a real deployment set DATABASE_URL to Postgres, run `alembic upgrade head`, and do not call this.

When ZOHO_API_ENABLED=true the demo also loads data/zoho_demo/supplier_master.csv (the SKU to Zoho
Books vendor and item mapping) and turns the zoho_api_enabled switch on, audited like any flip.
"""

import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from planner import models as m
from planner.config import ROOT, load_config
from planner.control import switches
from planner.db import make_engine, make_session_factory
from planner.ingest.load import ingest_folder
from planner.integrations.zoho import api_enabled
from planner.jobs.run import plan
from planner.models import Base, PlanningRun

DEMO_RUN_DATE = "2026-10-19"
ZOHO_MAPPING = ROOT / "data" / "zoho_demo" / "supplier_master.csv"


def _inbound(tmp):
    """The seed exports, plus the Zoho mapping when the Zoho API is configured."""
    folder = Path(tmp) / "inbound"
    shutil.copytree(ROOT / "data" / "seed", folder)
    if api_enabled() and ZOHO_MAPPING.exists():
        shutil.copy(ZOHO_MAPPING, folder / "supplier_master.csv")
    return folder


def _zoho_switch_on(session):
    row = session.get(m.ControlSwitch, switches.ZOHO_API)
    if row is None or row.enabled:
        return
    reason = "Demo deployment: ZOHO_API_ENABLED is set, Zoho Books connected"
    now = datetime.now()
    row.enabled, row.updated_at, row.updated_by, row.reason = True, now, "demo-bootstrap", reason
    session.add(
        m.SwitchAudit(
            name=switches.ZOHO_API,
            old_value=False,
            new_value=True,
            changed_by="demo-bootstrap",
            changed_at=now,
            reason=reason,
            source="install",
        )
    )
    session.commit()


def bootstrap(run_date=DEMO_RUN_DATE):
    engine = make_engine()
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        switches.install_defaults(session)
        if session.scalar(select(func.count()).select_from(PlanningRun)) == 0:
            cfg = load_config(run_date=run_date)
            with tempfile.TemporaryDirectory() as tmp:
                if ingest_folder(session, _inbound(tmp), cfg, run_date).loaded:
                    plan(session, run_date, cfg)
        if api_enabled():
            _zoho_switch_on(session)
