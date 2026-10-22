"""The scheduled planning run: storage, idempotency, staleness and exit codes."""

from datetime import datetime

import pytest
from sqlalchemy import func, select

from planner import models as m
from planner.db import make_engine
from planner.ingest.load import ingest_folder
from planner.jobs import run as run_job
from planner.jobs.run import DataError, RunLocked, plan
from planner.models import Base
from tests.conftest import RUN_DATE, SEED


@pytest.fixture
def loaded(session, cfg):
    assert ingest_folder(session, SEED, cfg, RUN_DATE).loaded
    return session


def decide(session, rec, action="approve"):
    session.add(
        m.PlannerDecision(
            recommendation_id=rec.id,
            action=action,
            qty=rec.recommended_qty,
            decided_by="raghav",
            decided_at=datetime.now(),
        )
    )
    session.commit()


def test_run_stores_every_recommendation_with_its_trace(loaded, cfg):
    run = plan(loaded, RUN_DATE, cfg)
    assert run.status == "completed" and run.festival_month is True
    assert run.summary["po_units"] == 1820
    recs = {r.sku_id: r for r in run.recommendations}
    assert len(recs) == 15
    mango = recs["SKU001"]
    assert (mango.decision, mango.recommended_qty) == ("REORDER", 750)
    assert (
        mango.detail["units"][-4:]
        and mango.detail["avg_12w"] == 214.92
        and mango.detail["stock_on_hand"] == 900
    )
    assert mango.rule_trace[-1] == "Below 6 wks -> reorder 750 (= 500 x 1.5)"
    assert mango.comparison == {
        "trend_test_basis": "adjusted",
        "decision": "REORDER",
        "recommended_qty": 500,
        "trend_ratio": 0.972,
        "high_demand": False,
        "review_flags": [
            "Standard qty leaves only 5.21 wks cover; 712 units needed to reach 6 wks",
            "Volatile demand (CV 0.15) - check for a level shift",
        ],
    }
    assert run.config["trend_test_basis"] == "unadjusted"


def test_rerun_without_decisions_replaces_the_run(loaded, cfg):
    first = plan(loaded, RUN_DATE, cfg).started_at
    second = plan(loaded, RUN_DATE, cfg).started_at
    assert second > first
    assert loaded.scalar(select(func.count()).select_from(m.PlanningRun)) == 1
    assert loaded.scalar(select(func.count()).select_from(m.Recommendation)) == 15


def test_rerun_after_decisions_is_refused(loaded, cfg):
    run = plan(loaded, RUN_DATE, cfg)
    decide(loaded, run.recommendations[0])
    with pytest.raises(RunLocked):
        plan(loaded, RUN_DATE, cfg)
    loaded.rollback()
    assert loaded.scalars(select(m.PlanningRun.id)).all() == [run.id]
    assert loaded.scalar(select(func.count()).select_from(m.Recommendation)) == 15


def test_stale_data_stops_the_run(loaded, cfg):
    with pytest.raises(DataError, match="inventory is 3 days old"):
        plan(loaded, "2026-10-22", cfg)
    failed = loaded.scalars(select(m.PlanningRun)).one()
    assert failed.status == "failed" and failed.recommendations == []
    assert "data is stale" not in failed.error  # sales up to the week of 12 Oct are still fresh on the 22nd


def test_only_complete_weeks_and_past_inventory_are_used(loaded, cfg):
    # A run on 2026-10-18 cannot see week 42 (it ends on the 18th) or inventory dated the 19th.
    with pytest.raises(DataError) as e:
        plan(loaded, "2026-10-18", cfg)
    assert any("no inventory record" in msg for msg in e.value.errors)


@pytest.fixture
def db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'p.db').as_posix()}"
    Base.metadata.create_all(make_engine(url))
    monkeypatch.setenv("DATABASE_URL", url)
    return url


def test_cli_exit_codes(db_url, tmp_path):
    out = ["--out", str(tmp_path / "out")]
    assert run_job.main(["--date", RUN_DATE, "--ingest-dir", str(SEED), *out]) == 0
    assert (tmp_path / "out" / "recommendations.csv").exists()
    assert run_job.main(["--date", RUN_DATE, *out]) == 0  # no decisions yet: replaced

    from planner.db import make_session_factory

    with make_session_factory()() as s:
        decide(s, s.scalars(select(m.Recommendation)).first())
    assert run_job.main(["--date", RUN_DATE, *out]) == 2  # locked by a decision
    assert run_job.main(["--date", "2026-10-30", *out]) == 1  # stale data
    assert run_job.main(["--date", "2026-10-30", "--ingest-dir", str(SEED), *out]) == 1  # ingest rejected


def test_cli_unexpected_failure(db_url, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database gone")

    monkeypatch.setattr(run_job, "plan", boom)
    assert run_job.main(["--date", RUN_DATE]) == 3
