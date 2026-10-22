"""SAP ingestion: contracts, validation, all-or-nothing loading and the ingest report."""

import hashlib

import pytest
from sqlalchemy import func, select

from planner import models as m
from planner.config import load_config
from planner.ingest.load import dataset_for, ingest_folder
from planner.jobs.run import DataError, run
from planner.rules.engine import evaluate
from tests.conftest import RUN_DATE, write_csv


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_seed_loads_cleanly(session, seed_dir, cfg):
    result = ingest_folder(session, seed_dir, cfg, RUN_DATE)
    assert result.loaded
    assert not [i for i in result.issues if i.level == "error"]
    assert (count(session, m.Sku), count(session, m.WeeklySales), count(session, m.InventorySnapshot)) == (
        15,
        240,
        15,
    )
    reports = session.scalars(select(m.IngestReport)).all()
    assert {r.file for r in reports} == {
        "sku_master.csv",
        "distributor_master.csv",
        "calendar.csv",
        "weekly_sales.csv",
        "current_inventory.csv",
    }
    assert all(r.status == "loaded" and r.errors == 0 for r in reports)
    sales = next(r for r in reports if r.file == "weekly_sales.csv")
    assert sales.rows == 240
    assert sales.checksum == hashlib.sha256((seed_dir / "weekly_sales.csv").read_bytes()).hexdigest()


def test_reload_is_idempotent(session, seed_dir, cfg):
    ingest_folder(session, seed_dir, cfg, RUN_DATE)
    ingest_folder(session, seed_dir, cfg, RUN_DATE)
    assert (count(session, m.Sku), count(session, m.WeeklySales), count(session, m.InventorySnapshot)) == (
        15,
        240,
        15,
    )


def test_database_round_trip_gives_same_recommendations(session, seed_dir, cfg):
    from planner.ingest.files import read_folder

    ingest_folder(session, seed_dir, cfg, RUN_DATE)
    from_db, _, _ = evaluate(cfg, dataset_for(session, RUN_DATE))
    from_files, _, _ = evaluate(cfg, read_folder(seed_dir)[0])
    assert from_db == from_files


def drop(pred):
    def fn(c):
        c.rows = [r for r in c.rows if not pred(r)]

    return fn


def set_where(pred, **values):
    def fn(c):
        for r in c.rows:
            if pred(r):
                r.update(values)

    return fn


def sku(s):
    return lambda r: r["SKU_ID"] == s


def first(c):
    return c.rows[0]


# (description, file, edit, as_of, expected text in an error)
BAD = [
    (
        "negative sales",
        "weekly_sales.csv",
        set_where(lambda r: r["SKU_ID"] == "SKU001" and r["Week"] == "2026-W40", Units_Sold="-5"),
        RUN_DATE,
        "negative sales",
    ),
    (
        "duplicate sales row",
        "weekly_sales.csv",
        lambda c: c.rows.append(dict(first(c))),
        RUN_DATE,
        "duplicate sales row",
    ),
    (
        "unknown SKU in sales",
        "weekly_sales.csv",
        lambda c: c.rows.append({"SKU_ID": "SKU999", "Week": "2026-W42", "Units_Sold": "10"}),
        RUN_DATE,
        "SKU999 2026-W42: SKU not in sku_master",
    ),
    ("SKU with no sales", "weekly_sales.csv", drop(sku("SKU005")), RUN_DATE, "SKU005: no sales history"),
    (
        "non-numeric sales",
        "weekly_sales.csv",
        lambda c: first(c).update(Units_Sold="abc"),
        RUN_DATE,
        "line 2: Units_Sold",
    ),
    (
        "bad week format",
        "weekly_sales.csv",
        lambda c: first(c).update(Week="2026-27"),
        RUN_DATE,
        "line 2: Week",
    ),
    (
        "header mismatch",
        "weekly_sales.csv",
        lambda c: (
            c.columns.__setitem__(2, "Units"),
            [r.__setitem__("Units", r.pop("Units_Sold")) for r in c.rows],
        ),
        RUN_DATE,
        "header mismatch: missing ['Units_Sold'], unexpected ['Units']",
    ),
    (
        "SKU with no inventory",
        "current_inventory.csv",
        drop(sku("SKU003")),
        RUN_DATE,
        "SKU003: no inventory record",
    ),
    (
        "inventory older than 1 day",
        "current_inventory.csv",
        set_where(sku("SKU002"), As_Of_Date="2026-10-17"),
        RUN_DATE,
        "SKU002: inventory is 2 days old (limit 1)",
    ),
    (
        "inventory dated in the future",
        "current_inventory.csv",
        set_where(sku("SKU002"), As_Of_Date="2026-10-20"),
        RUN_DATE,
        "SKU002: inventory dated 2026-10-20, after the run date 2026-10-19",
    ),
    (
        "negative inventory",
        "current_inventory.csv",
        set_where(sku("SKU002"), Units_On_Hand="-1"),
        RUN_DATE,
        "Units_On_Hand",
    ),
    (
        "unknown inventory SKU",
        "current_inventory.csv",
        lambda c: c.rows.append({**first(c), "SKU_ID": "SKU999"}),
        RUN_DATE,
        "SKU999: inventory for a SKU not in",
    ),
    (
        "unknown perishability",
        "sku_master.csv",
        set_where(sku("SKU004"), Perishability="Medium"),
        RUN_DATE,
        "SKU004: unknown perishability 'Medium'",
    ),
    (
        "duplicate SKU in master",
        "sku_master.csv",
        lambda c: c.rows.append(dict(first(c))),
        RUN_DATE,
        "duplicate SKU_ID",
    ),
    (
        "bad festival flag",
        "calendar.csv",
        lambda c: first(c).update(Is_Festival_Month="maybe"),
        RUN_DATE,
        "Is_Festival_Month",
    ),
    # 2026-10-27: inventory is fresh, but the latest sales week (starting 2026-10-12) is 15 days old
    (
        "stale sales",
        "current_inventory.csv",
        lambda c: [r.update(As_Of_Date="2026-10-27") for r in c.rows],
        "2026-10-27",
        "latest sales week starts 2026-10-12; data is stale",
    ),
]


@pytest.mark.parametrize("desc,file,fn,as_of,expected", BAD, ids=[b[0] for b in BAD])
def test_bad_data_stops_load_and_run(session, seed_dir, edit, desc, file, fn, as_of, expected, tmp_path):
    edit(file, fn)
    cfg = load_config(run_date=as_of)
    result = ingest_folder(session, seed_dir, cfg, as_of)
    assert not result.loaded
    messages = [i.message for i in result.issues if i.level == "error"]
    assert any(expected in msg for msg in messages), messages
    assert count(session, m.Sku) == 0 and count(session, m.WeeklySales) == 0  # nothing loaded
    assert {r.status for r in session.scalars(select(m.IngestReport))} == {"rejected"}
    with pytest.raises(DataError, match="Run stopped"):
        run(run_date=as_of, outdir=tmp_path / "out", data_dir=seed_dir)


def test_missing_required_file(session, seed_dir, cfg):
    (seed_dir / "weekly_sales.csv").unlink()
    result = ingest_folder(session, seed_dir, cfg, RUN_DATE)
    assert not result.loaded
    report = session.scalars(select(m.IngestReport).where(m.IngestReport.file == "weekly_sales.csv")).one()
    assert report.status == "rejected" and report.messages[0]["message"] == "required file missing"


def test_week_gap_is_a_warning(session, seed_dir, edit, cfg):
    edit("weekly_sales.csv", drop(lambda r: r["SKU_ID"] == "SKU008" and r["Week"] == "2026-W35"))
    edit("weekly_sales.csv", drop(lambda r: r["SKU_ID"] == "SKU009" and r["Week"] == "2026-W42"))
    result = ingest_folder(session, seed_dir, cfg, RUN_DATE)
    assert result.loaded
    warnings = [i.message for i in result.issues if i.level == "warn"]
    assert any(w.startswith("SKU008: missing weeks before") for w in warnings)
    assert any(w.startswith("SKU009: no sales rows after week starting 2026-10-05") for w in warnings)


OPEN_PO_COLS = ["PO_Number", "SKU_ID", "Units_Open", "Status", "Expected_Date"]


def test_open_pos_count_towards_cover(session, seed_dir, cfg):
    write_csv(
        seed_dir / "open_po.csv",
        OPEN_PO_COLS,
        [
            {
                "PO_Number": "PO-1",
                "SKU_ID": "SKU004",
                "Units_Open": "200",
                "Status": "In_Transit",
                "Expected_Date": "2026-10-22",
            }
        ],
    )
    assert ingest_folder(session, seed_dir, cfg, RUN_DATE).loaded
    rows, _, _ = evaluate(cfg, dataset_for(session, RUN_DATE))
    almond = next(r for r in rows if r["sku_id"] == "SKU004")
    # 180 on hand + 200 in transit = 380 / 99.79 a week = 3.81 weeks: inside the 3-4 window, so no reorder
    assert almond["open_po_units"] == 200 and almond["weeks_cover"] == 3.81 and almond["decision"] == "HOLD"
    assert almond["rule_trace"][0] == "Stock position 380 = 180 on hand + 200 on order"

    rows, _, _ = evaluate({**cfg, "cover_includes_open_pos": False}, dataset_for(session, RUN_DATE))
    almond = next(r for r in rows if r["sku_id"] == "SKU004")
    assert almond["decision"] == "REORDER" and almond["open_po_units"] is None


def test_no_open_po_file_logs_the_warning(session, seed_dir, cfg):
    result = ingest_folder(session, seed_dir, cfg, RUN_DATE)
    assert "no open-PO / in-transit data: cover uses on-hand stock only" in [i.message for i in result.issues]
    assert dataset_for(session, RUN_DATE).open_pos is None


def test_open_po_for_unknown_sku_is_an_error(session, seed_dir, cfg):
    write_csv(
        seed_dir / "open_po.csv",
        OPEN_PO_COLS,
        [{"PO_Number": "PO-9", "SKU_ID": "SKU999", "Units_Open": "5", "Status": "Open", "Expected_Date": ""}],
    )
    result = ingest_folder(session, seed_dir, cfg, RUN_DATE)
    assert not result.loaded
    assert "PO-9: open PO for SKU999, not in sku_master" in [i.message for i in result.issues]


def test_migrations_match_models(tmp_path):
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext

    from planner.config import ROOT
    from planner.db import make_engine
    from planner.models import Base

    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    alembic_cfg = Config(str(ROOT / "alembic.ini"))
    with engine.begin() as conn:
        alembic_cfg.attributes["connection"] = conn
        command.upgrade(alembic_cfg, "head")
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
