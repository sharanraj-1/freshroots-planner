"""Pre-PO check: a seeded demand drop raises an alert; normal data does not."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from planner import models as m
from planner.api import deps
from planner.api.main import app
from planner.control import switches
from planner.ingest.load import ingest_folder
from planner.jobs import pre_po_check
from planner.jobs.run import plan
from tests.conftest import RUN_DATE, write_csv

CHECK_DAY = date(2026, 10, 20)  # orders for 17, 18 and 19 October are "the last 3 days"
ORDER_COLS = ["Date", "SKU_ID", "Units_Ordered"]
SUPPLIER_COLS = [
    "SKU_ID",
    "Supplier_ID",
    "Supplier_Name",
    "Lead_Time_Weeks",
    "Zoho_Vendor_ID",
    "Zoho_Item_ID",
    "Unit_Cost_INR",
]
# Mango Drink's 4-week average is 261 units a week = 37.3 a day.


def orders(seed_dir, per_day, sku="SKU001", days=3):
    write_csv(
        seed_dir / "daily_orders.csv",
        ORDER_COLS,
        [
            {
                "Date": (CHECK_DAY - timedelta(days=i)).isoformat(),
                "SKU_ID": sku,
                "Units_Ordered": str(per_day),
            }
            for i in range(1, days + 1)
        ],
    )


def setup(session, seed_dir, cfg, unit_cost="1000"):
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
                "Unit_Cost_INR": unit_cost,
            }
        ],
    )
    assert ingest_folder(session, seed_dir, cfg, RUN_DATE).loaded
    run = plan(session, RUN_DATE, cfg)
    mango = next(r for r in run.recommendations if r.sku_id == "SKU001")
    session.add(
        m.PlannerDecision(
            recommendation_id=mango.id,
            action="approve",
            qty=750,
            decided_by="raghav",
            decided_at=datetime.now(),
        )
    )
    session.commit()


def alerts(session):
    return session.scalars(select(m.Alert).where(m.Alert.kind == "pre_po")).all()


def test_seeded_drop_raises_an_alert(session, seed_dir, cfg):
    orders(seed_dir, 20)  # 20 a day vs 37.3: down 46%
    setup(session, seed_dir, cfg)  # 750 x INR 1,000 = INR 7.5 lakh, above the 5 lakh threshold
    (po,) = pre_po_check.run_daily(session, cfg, CHECK_DAY)
    (r,) = po["results"]
    assert (r["status"], r["recent_daily"], r["baseline_daily"], r["drop"]) == ("dropped", 20.0, 37.3, 0.464)
    (a,) = alerts(session)
    assert (
        a.severity == "critical" and "SKU001 orders dropped 46%" in a.message and "INR 750,000" in a.message
    )
    pre_po_check.run_daily(session, cfg, CHECK_DAY)
    assert len(alerts(session)) == 1  # not repeated while unresolved


def test_normal_orders_do_not_alert(session, seed_dir, cfg):
    orders(seed_dir, 36)  # down 3.5%: inside the 20% threshold
    setup(session, seed_dir, cfg)
    (po,) = pre_po_check.run_daily(session, cfg, CHECK_DAY)
    assert po["results"][0]["status"] == "ok" and alerts(session) == []


def test_small_po_is_not_checked(session, seed_dir, cfg):
    orders(seed_dir, 20)
    setup(session, seed_dir, cfg, unit_cost="100")  # INR 75,000
    assert pre_po_check.run_daily(session, cfg, CHECK_DAY) == [] and alerts(session) == []


def test_unknown_value_is_checked_anyway(session, seed_dir, cfg):
    orders(seed_dir, 20)
    setup(session, seed_dir, cfg, unit_cost="")
    (po,) = pre_po_check.run_daily(session, cfg, CHECK_DAY)
    assert po["value_known"] is False and "value unknown" in alerts(session)[0].message


def test_missing_days_mean_no_check(session, seed_dir, cfg):
    orders(seed_dir, 20, days=2)
    setup(session, seed_dir, cfg)
    (po,) = pre_po_check.run_daily(session, cfg, CHECK_DAY)
    assert po["results"][0]["status"] == "no_data" and alerts(session) == []


def test_switched_off(session, seed_dir, cfg):
    orders(seed_dir, 20)
    setup(session, seed_dir, cfg)
    switches.flip(session, switches.PRE_PO_CHECK, False, "test", "raghav", cfg)
    with pytest.raises(pre_po_check.CheckOff):
        pre_po_check.run_daily(session, cfg, CHECK_DAY)


@pytest.mark.parametrize(
    "bad,error",
    [
        (
            {"Date": "2026-10-21", "SKU_ID": "SKU999", "Units_Ordered": "5"},
            "daily orders for a SKU not in sku_master",
        ),
        ({"Date": "2026-10-25", "SKU_ID": "SKU001", "Units_Ordered": "5"}, "after 2026-10-19"),
        ({"Date": "2026-10-18", "SKU_ID": "SKU001", "Units_Ordered": "-5"}, "Units_Ordered"),
    ],
)
def test_daily_orders_validation(session, seed_dir, cfg, bad, error):
    write_csv(seed_dir / "daily_orders.csv", ORDER_COLS, [bad])
    result = ingest_folder(session, seed_dir, cfg, RUN_DATE)
    assert not result.loaded and any(error in i.message for i in result.issues if i.level == "error")


@pytest.fixture
def client(engine, session, seed_dir, cfg, monkeypatch):
    from planner.db import make_session_factory

    orders(seed_dir, 20)
    setup(session, seed_dir, cfg)
    monkeypatch.setattr(
        pre_po_check, "date", type("D", (), {"today": staticmethod(lambda: CHECK_DAY)}), raising=False
    )
    factory = make_session_factory(engine)

    def s():
        with factory() as x:
            yield x

    app.dependency_overrides[deps.get_session] = s
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_on_demand_endpoint_and_banner_dismissal(client, session, monkeypatch):
    from planner.api import control_routes

    monkeypatch.setattr(control_routes, "date", type("D", (), {"today": staticmethod(lambda: CHECK_DAY)}))
    small = client.post("/api/pre-po-check", json={"sku_ids": ["SKU001"], "po_value": 100000}).json()
    assert small["checked"] is False
    big = client.post("/api/pre-po-check", json={"sku_ids": ["SKU001"], "po_value": 900000}).json()
    assert big["checked"] and big["alert"] and big["results"][0]["status"] == "dropped"
    status = client.get("/status").json()
    (a,) = [x for x in status["open_alerts"] if x["kind"] == "pre_po"]
    assert client.post(f"/api/alerts/{a['id']}/resolve").status_code == 401
    assert (
        client.post(f"/api/alerts/{a['id']}/resolve", headers={"X-Planner-User": "raghav"}).status_code == 200
    )
    assert not [x for x in client.get("/status").json()["open_alerts"] if x["kind"] == "pre_po"]
