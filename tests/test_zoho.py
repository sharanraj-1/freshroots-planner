"""Zoho draft POs: allowlist, OAuth refresh, grouping, retries, CSV fallback and switches. HTTP is mocked."""

import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from planner import models as m
from planner.api import deps, routes
from planner.api.main import app
from planner.control import switches
from planner.ingest.load import ingest_folder
from planner.integrations import zoho
from planner.jobs.run import plan
from tests.conftest import RUN_DATE, write_csv

ENV = {
    "ZOHO_DC": "in",
    "ZOHO_ORGANIZATION_ID": "60001",
    "ZOHO_CLIENT_ID": "cid",
    "ZOHO_CLIENT_SECRET": "sec",
    "ZOHO_REFRESH_TOKEN": "rt",
}
SUPPLIER_COLS = [
    "SKU_ID",
    "Supplier_ID",
    "Supplier_Name",
    "Lead_Time_Weeks",
    "Zoho_Vendor_ID",
    "Zoho_Item_ID",
    "Unit_Cost_INR",
]


class FakeZoho:
    """Records every request; answers token, item lookup and PO creation."""

    def __init__(self, fail_creates=0, status="draft", token_401_once=False):
        self.requests, self.fail_creates, self.status, self.token_401_once = (
            [],
            fail_creates,
            status,
            token_401_once,
        )
        self.po = 0

    def __call__(self, request):
        self.requests.append(request)
        url = str(request.url)
        if url.startswith("https://accounts.zoho.in/oauth/v2/token"):
            return httpx.Response(200, json={"access_token": f"tok{len(self.requests)}", "expires_in": 3600})
        if self.token_401_once and request.url.path.endswith("/purchaseorders"):
            self.token_401_once = False
            return httpx.Response(401, json={"code": 57, "message": "invalid token"})
        if request.url.path.endswith("/items"):
            return httpx.Response(
                200, json={"code": 0, "items": [{"item_id": f"item-{request.url.params['sku']}"}]}
            )
        if request.method == "POST" and request.url.path.endswith("/purchaseorders"):
            if self.fail_creates:
                self.fail_creates -= 1
                return httpx.Response(503, text="busy")
            self.po += 1
            return httpx.Response(
                201,
                json={
                    "code": 0,
                    "message": "created",
                    "purchaseorder": {"purchaseorder_id": f"PO{self.po}", "status": self.status},
                },
            )
        return httpx.Response(404, json={"code": 404, "message": "unexpected"})

    def creates(self):
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == "POST" and r.url.path.endswith("/purchaseorders")
        ]


def client(fake):
    return zoho.ZohoClient(transport=httpx.MockTransport(fake), env=ENV)


@pytest.fixture
def run(session, seed_dir, cfg, monkeypatch):
    write_csv(
        seed_dir / "supplier_master.csv",
        SUPPLIER_COLS,
        [
            {
                "SKU_ID": "SKU001",
                "Supplier_ID": "S1",
                "Supplier_Name": "Konkan Fruits",
                "Lead_Time_Weeks": "2",
                "Zoho_Vendor_ID": "V1",
                "Zoho_Item_ID": "",
                "Unit_Cost_INR": "18.5",
            },
            {
                "SKU_ID": "SKU003",
                "Supplier_ID": "S2",
                "Supplier_Name": "Deccan Grains",
                "Lead_Time_Weeks": "3",
                "Zoho_Vendor_ID": "V2",
                "Zoho_Item_ID": "Z-ITEM-3",
                "Unit_Cost_INR": "",
            },
            {
                "SKU_ID": "SKU011",
                "Supplier_ID": "S1",
                "Supplier_Name": "Konkan Fruits",
                "Lead_Time_Weeks": "2",
                "Zoho_Vendor_ID": "V1",
                "Zoho_Item_ID": "Z-ITEM-11",
                "Unit_Cost_INR": "40",
            },
            {
                "SKU_ID": "SKU004",
                "Supplier_ID": "S3",
                "Supplier_Name": "Unlisted Dairy",
                "Lead_Time_Weeks": "",
                "Zoho_Vendor_ID": "",
                "Zoho_Item_ID": "",
                "Unit_Cost_INR": "",
            },
        ],
    )
    assert ingest_folder(session, seed_dir, cfg, RUN_DATE).loaded
    run = plan(session, RUN_DATE, cfg)
    recs = {r.sku_id: r for r in run.recommendations}
    for sku, action, qty in (
        ("SKU001", "approve", 750),
        ("SKU003", "edit", 350),
        ("SKU011", "approve", 500),
        ("SKU004", "approve", 150),
        ("SKU013", "reject", 0),
    ):
        session.add(
            m.PlannerDecision(
                recommendation_id=recs[sku].id,
                action=action,
                qty=qty,
                decided_by="raghav",
                decided_at=datetime.now(),
                reason=None if action == "approve" else "Other",
            )
        )
    session.commit()
    switches.flip(session, switches.ZOHO_API, True, "test org configured", "raghav", cfg)
    monkeypatch.setenv("ZOHO_API_ENABLED", "true")
    return run


def current(session, sku):
    rec = session.scalars(select(m.Recommendation).where(m.Recommendation.sku_id == sku)).one()
    return rec.decisions[-1]


# ---------- the allowlist ----------
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/purchaseorders/123/status/issued"),
        ("POST", "/purchaseorders/123/submit"),
        ("POST", "/purchaseorders/123/approve"),
        ("POST", "/purchaseorders/123/email"),
        ("PUT", "/purchaseorders/123"),
        ("DELETE", "/purchaseorders/123"),
        ("POST", "/contacts"),
    ],
)
def test_only_draft_creation_is_allowed(method, path):
    fake = FakeZoho()
    with pytest.raises(zoho.ForbiddenCall):
        client(fake).request(method, path)
    assert fake.requests == []  # refused before anything was sent


def test_token_refresh_and_headers():
    fake = FakeZoho(token_401_once=True)
    c = client(fake)
    c.create_draft_po("V1", [{"item_id": "I", "quantity": 1}], "ref", "notes")
    token = [r for r in fake.requests if "oauth" in str(r.url)]
    assert len(token) == 2  # first token, then one forced refresh after the 401
    form = dict(x.split("=") for x in token[0].content.decode().split("&"))
    assert form == {
        "grant_type": "refresh_token",
        "client_id": "cid",
        "client_secret": "sec",
        "refresh_token": "rt",
    }
    po = [r for r in fake.requests if r.url.path.endswith("/purchaseorders")][-1]
    assert str(po.url).startswith("https://www.zohoapis.in/inventory/v1/purchaseorders?organization_id=60001")
    assert po.headers["Authorization"].startswith("Zoho-oauthtoken tok")


def test_missing_credentials():
    with pytest.raises(zoho.ZohoError, match="credentials"):
        zoho.ZohoClient(transport=httpx.MockTransport(FakeZoho()), env={}).request("GET", "/items")


# ---------- pushing a run ----------
def test_push_groups_by_vendor_and_keeps_csv_for_the_rest(session, run, cfg):
    fake = FakeZoho()
    result = zoho.push_run(session, run, cfg, client(fake), sleep=lambda s: None)
    assert sorted(x["sku_id"] for x in result["created"]) == ["SKU001", "SKU003", "SKU011"]
    assert result["csv_only"] == ["SKU004"] and result["failed"] == []
    creates = fake.creates()
    assert len(creates) == 2  # one draft per vendor
    v1 = next(c for c in creates if c["vendor_id"] == "V1")
    assert v1["line_items"] == [
        {"item_id": "item-SKU001", "quantity": 750, "purchase_rate": 18.5},
        {"item_id": "Z-ITEM-11", "quantity": 500, "purchase_rate": 40.0},
    ]
    assert v1["reference_number"] == f"FR-{RUN_DATE}-run{run.id}"
    assert "purchaseorder_number" not in v1 and "status" not in v1
    assert current(session, "SKU001").zoho_po_id == current(session, "SKU011").zoho_po_id
    assert current(session, "SKU004").zoho_status == "csv_only"
    assert current(session, "SKU013").zoho_status is None  # rejected lines are never sent


def test_retries_then_success(session, run, cfg):
    sleeps = []
    fake = FakeZoho(fail_creates=2)
    result = zoho.push_run(session, run, cfg, client(fake), sleep=sleeps.append)
    assert len(result["created"]) == 3 and sleeps == [1, 2]
    assert switches.is_enabled(session, switches.ZOHO_API)


def test_repeated_failures_fall_back_to_csv_and_trip_the_switch(session, run, cfg):
    fake = FakeZoho(fail_creates=99)
    result = zoho.push_run(session, run, cfg, client(fake), sleep=lambda s: None)
    assert result["created"] == []
    assert sorted(result["failed"] + result["csv_only"]) == ["SKU001", "SKU003", "SKU004", "SKU011"]
    assert not switches.is_enabled(session, switches.ZOHO_API)  # 3 failures in a row
    assert len(fake.creates()) == 3  # stopped retrying once the switch went off


def test_non_draft_status_raises_an_alert(session, run, cfg):
    zoho.push_run(session, run, cfg, client(FakeZoho(status="issued")), sleep=lambda s: None)
    alert = session.scalars(select(m.Alert).where(m.Alert.kind == "zoho")).first()
    assert alert.severity == "critical" and "not draft" in alert.message


@pytest.mark.parametrize("gate", ["env", "switch"])
def test_gates(session, run, cfg, monkeypatch, gate):
    if gate == "env":
        monkeypatch.setenv("ZOHO_API_ENABLED", "false")
    else:
        switches.flip(session, switches.ZOHO_API, False, "pause", "raghav", cfg)
    fake = FakeZoho()
    with pytest.raises(zoho.ZohoError, match="CSV export"):
        zoho.push_run(session, run, cfg, client(fake))
    assert fake.requests == []


# ---------- API and CSV export ----------
@pytest.fixture
def api(engine, run):
    from planner.db import make_session_factory

    factory = make_session_factory(engine)

    def s():
        with factory() as x:
            yield x

    fake = FakeZoho()
    app.dependency_overrides[deps.get_session] = s
    app.dependency_overrides[routes.get_zoho_client] = lambda: client(fake)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_push_endpoint_and_csv_only_keeps_unsent_lines(api, run, monkeypatch):
    monkeypatch.setattr(zoho.time, "sleep", lambda s: None)
    r = api.post(f"/api/runs/{run.id}/push-to-zoho", headers={"X-Planner-User": "raghav"})
    assert r.status_code == 200 and len(r.json()["created"]) == 3
    csv_text = api.get(f"/api/runs/{run.id}/export/zoho.csv").text
    assert "SKU004" in csv_text and "SKU001" not in csv_text and "SKU003" not in csv_text
    rec_id = next(x for x in run.recommendations if x.sku_id == "SKU001").id
    r = api.post(
        f"/api/recommendations/{rec_id}/decision",
        json={"action": "reject", "reason": "Other"},
        headers={"X-Planner-User": "raghav"},
    )
    assert r.status_code == 409 and "already in Zoho" in r.text
    assert api.post(f"/api/runs/{run.id}/push-to-zoho").status_code == 401
