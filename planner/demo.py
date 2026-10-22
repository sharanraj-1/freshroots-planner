"""Demo mode for serverless hosting (Vercel): a throwaway SQLite database seeded from data/seed.

Every fresh instance starts from the seed run, so decisions last only as long as that instance.
For a real deployment set DATABASE_URL to Postgres, run `alembic upgrade head`, and do not call this.
"""

from sqlalchemy import func, select

from planner.config import ROOT, load_config
from planner.control.switches import install_defaults
from planner.db import make_engine, make_session_factory
from planner.ingest.load import ingest_folder
from planner.jobs.run import plan
from planner.models import Base, PlanningRun

DEMO_RUN_DATE = "2026-10-19"


def bootstrap(run_date=DEMO_RUN_DATE):
    engine = make_engine()
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        install_defaults(session)
        if session.scalar(select(func.count()).select_from(PlanningRun)) == 0:
            cfg = load_config(run_date=run_date)
            if ingest_folder(session, ROOT / "data" / "seed", cfg, run_date).loaded:
                plan(session, run_date, cfg)
