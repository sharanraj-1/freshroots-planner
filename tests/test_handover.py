"""Phase 7: health endpoint, JSON logs, rollback, shadow report and the KPI SQL."""

import json
import logging
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from planner import models as m
from planner.api import deps
from planner.api.main import app
from planner.config import ROOT
from planner.control import switches, tripwires
from planner.ingest.load import ingest_folder
from planner.jobs import shadow_report
from planner.jobs.run import plan
from planner.logs import JsonFormatter
from tests.conftest import RUN_DATE, SEED, write_csv


@pytest.fixture
def run(session, cfg):
    ingest_folder(session, SEED, cfg, RUN_DATE)
    return plan(session, RUN_DATE, cfg)


def decide(session, run, sku, action, qty):
    rec = next(r for r in run.recommendations if r.sku_id == sku)
    session.add(
        m.PlannerDecision(
            recommendation_id=rec.id,
            action=action,
            qty=qty,
            decided_by="raghav",
            decided_at=datetime.now(),
            reason=None if action == "approve" else "Other",
        )
    )
    session.commit()


# ---------- health ----------
def test_health(engine, run):
    from planner.db import make_session_factory

    factory = make_session_factory(engine)

    def s():
        with factory() as x:
            yield x

    app.dependency_overrides[deps.get_session] = s
    try:
        h = TestClient(app).get("/health").json()
    finally:
        app.dependency_overrides.clear()
    assert h["ok"] and h["database"] == "ok" and h["last_run"]["status"] == "completed"
    assert h["data_freshness"]["latest_inventory_date"] == RUN_DATE


def test_health_reports_a_dead_database():
    class Broken:
        def execute(self, *a):
            raise RuntimeError("connection refused")

    app.dependency_overrides[deps.get_session] = lambda: Broken()
    try:
        r = TestClient(app).get("/health")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["ok"] is False


# ---------- logging ----------
def test_json_log_lines():
    rec = logging.makeLogRecord(
        {
            "name": "planner.run",
            "levelname": "INFO",
            "levelno": 20,
            "msg": "run %s done",
            "args": (7,),
            "run_id": 7,
        }
    )
    line = json.loads(JsonFormatter().format(rec))
    assert line["msg"] == "run 7 done" and line["level"] == "INFO" and line["run_id"] == 7 and "ts" in line


# ---------- rollback ----------
def test_rollback_allows_a_fresh_run_for_the_same_date(session, run, cfg):
    decide(session, run, "SKU001", "approve", 750)
    first_id = run.id
    with pytest.raises(switches.NotAdmin):
        tripwires.rollback(session, run, "wrong stock file", "intern", cfg)
    assert tripwires.rollback(session, run, "wrong stock file", "raghav", cfg) == []
    assert run.status == "rolled_back"
    again = plan(session, RUN_DATE, cfg)  # allowed: the rolled-back run no longer blocks the date
    assert again.id != first_id and again.status == "completed"
    assert session.get(m.PlanningRun, first_id).status == "rolled_back"  # kept for audit
    alert = session.scalars(select(m.Alert).where(m.Alert.kind == "rollback")).one()
    assert "wrong stock file" in alert.message


def test_rollback_lists_zoho_drafts_to_delete(session, run, cfg):
    decide(session, run, "SKU001", "approve", 750)
    d = run.recommendations[0].decisions[-1]
    d.zoho_po_id = "PO-77"
    session.commit()
    assert tripwires.rollback(session, run, "bad run", "raghav", cfg) == ["PO-77"]


# ---------- shadow report ----------
def test_shadow_report(session, run, tmp_path):
    actual = tmp_path / "actual.csv"
    write_csv(
        actual,
        ["SKU_ID", "Units_Ordered"],
        [
            {"SKU_ID": "SKU001", "Units_Ordered": "600"},
            {"SKU_ID": "SKU003", "Units_Ordered": "300"},
            {"SKU_ID": "SKU002", "Units_Ordered": "200"},
        ],
    )
    rows, summary = shadow_report.compare(run, shadow_report.read_actual(actual))
    assert summary["rule_units"] == 1820 and summary["actual_units"] == 1100
    assert summary["rule_ordered_planner_did_not"] == ["SKU004", "SKU011", "SKU013"]
    assert summary["planner_ordered_rule_did_not"] == ["SKU002"]
    assert summary["agreement_rate"] == pytest.approx(11 / 15)
    base = shadow_report.write(rows, summary, tmp_path / "shadow")
    assert "Order / no-order agreement: 73% of 15 SKUs" in base.with_suffix(".md").read_text()


def test_shadow_report_rejects_a_bad_file(tmp_path):
    bad = tmp_path / "a.csv"
    write_csv(bad, ["SKU", "Qty"], [{"SKU": "SKU001", "Qty": "1"}])
    with pytest.raises(ValueError, match="SKU_ID, Units_Ordered"):
        shadow_report.read_actual(bad)


# ---------- KPI SQL ----------
def portable_queries():
    doc = (ROOT / "docs" / "KPIS.md").read_text(encoding="utf-8")
    return [q for q in re.findall(r"```sql\n(.*?)```", doc, re.S) if q.startswith("-- portable")]


def test_portable_kpi_queries_run(session, run):
    decide(session, run, "SKU001", "approve", 750)
    decide(session, run, "SKU003", "edit", 350)
    decide(session, run, "SKU003", "approve", 300)  # the latest decision counts
    queries = portable_queries()
    assert len(queries) == 3
    cycle, unedited, review = (session.execute(text(q)).mappings().all() for q in queries)
    assert cycle[0]["decided"] == 2 and cycle[0]["recommendations"] == 15
    assert (unedited[0]["decided"], unedited[0]["approved_unedited"]) == (2, 2)
    assert (review[0]["skus"], review[0]["need_review"], review[0]["exceptions"]) == (15, 5, 0)
