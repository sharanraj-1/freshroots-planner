"""Load validated SAP exports into the database, and read a planning Dataset back out.

A folder is loaded all-or-nothing: if any check fails, nothing is written except the
ingest report, which records every file, its row count, checksum and messages.
"""

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import delete, func, select

from planner import models as m
from planner.ingest.contracts import (
    CalendarRow,
    Dataset,
    DistributorRow,
    InventoryRow,
    OpenPoRow,
    SalesRow,
    SkuRow,
    SupplierRow,
)
from planner.ingest.files import read_folder
from planner.ingest.validate import errors, validate


@dataclass
class IngestResult:
    batch_id: str
    loaded: bool
    issues: list


def ingest_folder(session, folder, cfg, as_of):
    as_of = date.fromisoformat(str(as_of))
    ds, parsed, issues = read_folder(folder)
    if ds is not None:
        issues = issues + validate(ds, cfg, as_of)
    loaded = not errors(issues)
    batch_id = str(uuid.uuid4())
    per_file = Counter((i.file, i.level) for i in issues)
    for name, pf in parsed.items():
        session.add(
            m.IngestReport(
                batch_id=batch_id,
                file=name,
                rows=len(pf.rows),
                errors=per_file[(name, "error")],
                warnings=per_file[(name, "warn")],
                checksum=pf.checksum,
                status="loaded" if loaded else "rejected",
                messages=[i.as_dict() for i in issues if i.file == name],
                as_of_date=as_of,
            )
        )
    for name in {i.file for i in issues if i.file and i.file not in parsed}:  # required file missing
        session.add(
            m.IngestReport(
                batch_id=batch_id,
                file=name,
                rows=0,
                errors=per_file[(name, "error")],
                warnings=0,
                checksum="",
                status="rejected",
                as_of_date=as_of,
                messages=[i.as_dict() for i in issues if i.file == name],
            )
        )
    if loaded:
        _write(session, ds)
    session.commit()
    return IngestResult(batch_id, loaded, issues)


def _write(session, ds):
    new_skus = {s.sku_id for s in ds.skus}
    for sku in session.scalars(select(m.Sku)):
        sku.active = sku.sku_id in new_skus
    for s in ds.skus:
        session.merge(m.Sku(**s.model_dump(), active=True))
    for d in ds.distributors:
        session.merge(m.Distributor(**d.model_dump()))
    for c in ds.calendar:
        session.merge(m.CalendarMonth(**c.model_dump()))
    session.flush()

    existing = {(r.sku_id, r.week): r for r in session.scalars(select(m.WeeklySales))}
    for r in ds.sales:
        row = existing.get((r.sku_id, r.week))
        if row:
            row.units_sold = r.units_sold
        else:
            session.add(
                m.WeeklySales(sku_id=r.sku_id, week=r.week, week_start=r.week_start, units_sold=r.units_sold)
            )
    existing = {(r.sku_id, r.location, r.as_of_date): r for r in session.scalars(select(m.InventorySnapshot))}
    for r in ds.inventory:
        row = existing.get((r.sku_id, r.location, r.as_of_date))
        if row:
            row.units_on_hand = r.units_on_hand
        else:
            session.add(m.InventorySnapshot(**r.model_dump()))
    if ds.open_pos is not None:
        session.execute(delete(m.OpenPo))
        session.add_all(m.OpenPo(**p.model_dump()) for p in ds.open_pos)
    if ds.suppliers is not None:
        session.execute(delete(m.Supplier))
        known = new_skus
        session.add_all(m.Supplier(**s.model_dump()) for s in ds.suppliers if s.sku_id in known)


def _loaded(session, file):
    """True when the most recent successful batch included this optional file."""
    last = session.scalar(
        select(m.IngestReport.batch_id)
        .where(m.IngestReport.status == "loaded")
        .order_by(m.IngestReport.id.desc())
        .limit(1)
    )
    return (
        last is not None
        and session.scalar(
            select(func.count())
            .select_from(m.IngestReport)
            .where(m.IngestReport.batch_id == last, m.IngestReport.file == file)
        )
        > 0
    )


def dataset_for(session, run_date):
    """The data a run on run_date may use: active SKUs, complete weeks, inventory up to run_date."""
    run_date = date.fromisoformat(str(run_date))

    def rows(model, contract, *where):
        return [
            contract.model_validate(o, from_attributes=True)
            for o in session.scalars(select(model).where(*where))
        ]

    return Dataset(
        skus=rows(m.Sku, SkuRow, m.Sku.active.is_(True)),
        distributors=rows(m.Distributor, DistributorRow),
        calendar=rows(m.CalendarMonth, CalendarRow),
        sales=rows(m.WeeklySales, SalesRow, m.WeeklySales.week_start <= run_date - timedelta(days=7)),
        inventory=rows(m.InventorySnapshot, InventoryRow, m.InventorySnapshot.as_of_date <= run_date),
        open_pos=rows(m.OpenPo, OpenPoRow) if _loaded(session, "open_po.csv") else None,
        suppliers=rows(m.Supplier, SupplierRow) if _loaded(session, "supplier_master.csv") else None,
    )
