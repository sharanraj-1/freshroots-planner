"""API: runs, recommendations, decisions and exports."""

import csv
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from planner import models as m
from planner.api import deps
from planner.api.main import app
from planner.control.switches import install_defaults
from planner.db import make_engine, make_session_factory
from planner.ingest.load import ingest_folder
from planner.jobs.run import plan
from planner.models import Base
from tests.conftest import RUN_DATE, SEED

USER = {"X-Planner-User": "raghav"}


@pytest.fixture
def factory(tmp_path, cfg):
    engine = make_engine(f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        install_defaults(s)
        ingest_folder(s, SEED, cfg, RUN_DATE)
        plan(s, RUN_DATE, cfg)
    return factory


def client_for(factory):
    def session():
        with factory() as s:
            yield s

    app.dependency_overrides[deps.get_session] = session
    return TestClient(app)


@pytest.fixture
def client(factory):
    yield client_for(factory)
    app.dependency_overrides.clear()


@pytest.fixture
def run_id(client):
    return client.get("/api/runs").json()[0]["id"]


def recs(client, run_id, **params):
    r = client.get(f"/api/runs/{run_id}/recommendations", params=params)
    assert r.status_code == 200, r.text
    return {x["sku_id"]: x for x in r.json()}


def post(client, rec_id, headers=USER, **body):
    return client.post(f"/api/recommendations/{rec_id}/decision", json=body, headers=headers)


def test_runs(client, run_id):
    runs = client.get("/api/runs").json()
    assert len(runs) == 1 and runs[0]["run_date"] == RUN_DATE and runs[0]["summary"]["po_units"] == 1820
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["config"]["trend_test_basis"] == "unadjusted" and detail["data_checks"]
    assert client.get("/api/runs/999").status_code == 404


@pytest.mark.parametrize(
    "flt,expected", [(None, 15), ("review", 5), ("REORDER", 5), ("PAUSE", 2), ("HOLD", 8)]
)
def test_recommendation_filters(client, run_id, flt, expected):
    assert len(recs(client, run_id, **({"filter": flt} if flt else {}))) == expected


def test_bad_filter(client, run_id):
    assert client.get(f"/api/runs/{run_id}/recommendations", params={"filter": "nope"}).status_code == 422


def test_recommendation_carries_trace_and_comparison(client, run_id):
    mango = recs(client, run_id)["SKU001"]
    assert mango["rule_trace"][-1] == "Below 6 wks -> reorder 750 (= 500 x 1.5)"
    assert mango["comparison"]["recommended_qty"] == 500 and mango["current_decision"] is None


def test_approve_edit_reject(client, run_id):
    r = recs(client, run_id)
    ok = post(client, r["SKU001"]["id"], action="approve")
    assert ok.status_code == 200
    d = ok.json()["current_decision"]
    assert (d["action"], d["qty"], d["decided_by"], d["reason"]) == ("approve", 750, "raghav", None)

    d = post(client, r["SKU003"]["id"], action="edit", qty=350, reason="Supplier or MOQ constraint").json()
    assert d["current_decision"]["qty"] == 350

    d = post(client, r["SKU004"]["id"], action="reject", reason="Shelf-life risk").json()
    assert (d["current_decision"]["action"], d["current_decision"]["qty"]) == ("reject", 0)


@pytest.mark.parametrize(
    "body,status,text",
    [
        ({"action": "edit", "qty": -5, "reason": "Other"}, 422, "greater than or equal to 0"),
        ({"action": "edit", "qty": 12.5, "reason": "Other"}, 422, "integer"),
        ({"action": "edit", "qty": "lots", "reason": "Other"}, 422, "integer"),
        ({"action": "edit", "qty": 0, "reason": "Other"}, 422, "use reject"),
        ({"action": "edit", "reason": "Other"}, 422, "use reject"),
        ({"action": "edit", "qty": 400}, 422, "edit needs a reason"),
        ({"action": "edit", "qty": 400, "reason": "Because I said so"}, 422, "edit needs a reason"),
        ({"action": "reject"}, 422, "reject needs a reason"),
        ({"action": "reject", "qty": 10, "reason": "Other"}, 422, "cannot carry a quantity"),
        ({"action": "approve", "qty": 100}, 422, "use edit"),
        ({"action": "release"}, 422, "approve"),
        ({"action": "approve", "note": "x"}, 422, "Extra inputs"),
    ],
)
def test_invalid_decisions(client, run_id, body, status, text):
    rid = recs(client, run_id)["SKU001"]["id"]
    resp = post(client, rid, **body)
    assert resp.status_code == status and text in resp.text, resp.text
    assert recs(client, run_id)["SKU001"]["current_decision"] is None


def test_decision_needs_a_user(client, run_id):
    rid = recs(client, run_id)["SKU001"]["id"]
    assert post(client, rid, headers={}, action="approve").status_code == 401
    assert post(client, rid, headers={"X-Planner-User": "  "}, action="approve").status_code == 401
    assert post(client, 9999, action="approve").status_code == 404


def test_latest_decision_wins_and_history_is_kept(client, run_id, factory):
    rid = recs(client, run_id)["SKU001"]["id"]
    post(client, rid, action="approve")
    post(client, rid, action="reject", reason="Data looks wrong")
    assert recs(client, run_id)["SKU001"]["current_decision"]["action"] == "reject"
    with factory() as s:
        assert [d.action for d in s.scalars(select(m.PlannerDecision))] == ["approve", "reject"]


def test_decisions_survive_restart(factory, tmp_path, run_id, client):
    post(client, recs(client, run_id)["SKU001"]["id"], action="approve")
    app.dependency_overrides.clear()
    fresh = make_session_factory(make_engine(f"sqlite:///{(tmp_path / 'api.db').as_posix()}"))
    restarted = client_for(fresh)
    assert recs(restarted, run_id)["SKU001"]["current_decision"]["qty"] == 750


def read_csv(resp):
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(resp.text)))


def test_exports_match_the_database(client, run_id, factory):
    r = recs(client, run_id)
    post(client, r["SKU001"]["id"], action="approve")
    post(client, r["SKU003"]["id"], action="edit", qty=350, reason="Supplier or MOQ constraint")
    post(client, r["SKU004"]["id"], action="reject", reason="Shelf-life risk")
    post(client, r["SKU002"]["id"], action="approve")  # HOLD: approved with qty 0, so not on the PO

    zoho = read_csv(client.get(f"/api/runs/{run_id}/export/zoho.csv"))
    assert [(z["Item SKU"], z["Quantity"], z["Status"]) for z in zoho] == [
        ("SKU001", "750", "Draft"),
        ("SKU003", "350", "Draft"),
    ]
    assert zoho[1]["Reason"] == "Edited from 300: Supplier or MOQ constraint"
    assert all(z["Vendor"] == "TO BE ASSIGNED" and z["Decided By"] == "raghav" for z in zoho)

    rows = read_csv(client.get(f"/api/runs/{run_id}/export/decisions.csv"))
    assert len(rows) == 15
    with factory() as s:
        for rec in s.scalars(select(m.Recommendation)):
            row = next(x for x in rows if x["SKU"] == rec.sku_id)
            current = rec.decisions[-1] if rec.decisions else None
            assert row["Rule qty"] == str(rec.recommended_qty)
            assert row["Planner action"] == (current.action if current else "")
            assert row["Planner qty"] == (str(current.qty) if current else "")


def test_meta_lists_the_reasons(client):
    assert "Supplier or MOQ constraint" in client.get("/api/meta").json()["decision_reasons"]
