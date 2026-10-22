"""Database tables. Schema changes go through Alembic (migrations/)."""

from datetime import date, datetime

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------- master and transaction data, loaded from SAP exports ----------
class Sku(Base):
    __tablename__ = "sku"
    sku_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    sku_name: Mapped[str]
    category: Mapped[str]
    perishability: Mapped[str]
    standard_reorder_qty: Mapped[int]
    unit_of_measure: Mapped[str]
    distributor_id: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(default=True)  # in the latest sku_master export


class Distributor(Base):
    __tablename__ = "distributor"
    distributor_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    distributor_name: Mapped[str]
    region: Mapped[str]
    status: Mapped[str]


class CalendarMonth(Base):
    __tablename__ = "calendar"
    year_month: Mapped[str] = mapped_column(String(7), primary_key=True)
    year: Mapped[int]
    month: Mapped[int]
    is_festival: Mapped[bool]


class WeeklySales(Base):
    __tablename__ = "weekly_sales"
    __table_args__ = (UniqueConstraint("sku_id", "week"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.sku_id"))
    week: Mapped[str] = mapped_column(String(8))
    week_start: Mapped[date]
    units_sold: Mapped[int]


class InventorySnapshot(Base):
    __tablename__ = "inventory_snapshot"
    __table_args__ = (UniqueConstraint("sku_id", "location", "as_of_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.sku_id"))
    location: Mapped[str]
    units_on_hand: Mapped[int]
    as_of_date: Mapped[date]


class OpenPo(Base):
    """Current open and in-transit purchase orders. Replaced on every load."""

    __tablename__ = "open_po"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_number: Mapped[str]
    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.sku_id"))
    units_open: Mapped[int]
    status: Mapped[str]
    expected_date: Mapped[date | None]


class Supplier(Base):
    """Primary supplier per SKU. Replaced on every load."""

    __tablename__ = "supplier"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.sku_id"), unique=True)
    supplier_id: Mapped[str]
    supplier_name: Mapped[str]
    lead_time_weeks: Mapped[float | None]
    zoho_vendor_id: Mapped[str | None]
    zoho_item_id: Mapped[str | None]
    unit_cost_inr: Mapped[float | None]


class IngestReport(Base):
    __tablename__ = "ingest_report"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    file: Mapped[str]
    rows: Mapped[int]
    errors: Mapped[int]
    warnings: Mapped[int]
    checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[str]  # loaded | rejected
    messages: Mapped[list] = mapped_column(JSON, default=list)
    as_of_date: Mapped[date]
    loaded_at: Mapped[datetime] = mapped_column(server_default=func.now())


# ---------- planning ----------
class PlanningRun(Base):
    __tablename__ = "planning_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_date: Mapped[date] = mapped_column(unique=True)
    trend_basis: Mapped[str]
    status: Mapped[str]  # completed | failed
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    festival_month: Mapped[bool | None]
    summary: Mapped[dict | None] = mapped_column(JSON)
    config: Mapped[dict] = mapped_column(JSON)
    data_checks: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None]
    recommendations: Mapped[list["Recommendation"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Recommendation.sku_id"
    )


class Recommendation(Base):
    __tablename__ = "recommendation"
    __table_args__ = (UniqueConstraint("run_id", "sku_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("planning_run.id"))
    sku_id: Mapped[str] = mapped_column(ForeignKey("sku.sku_id"))
    decision: Mapped[str]
    recommended_qty: Mapped[int]
    needs_review: Mapped[bool]
    detail: Mapped[dict] = mapped_column(JSON)  # every input and computed figure for this SKU
    review_flags: Mapped[list] = mapped_column(JSON, default=list)
    exception_reasons: Mapped[list] = mapped_column(JSON, default=list)
    rule_trace: Mapped[list] = mapped_column(JSON, default=list)
    comparison: Mapped[dict | None] = mapped_column(JSON)  # same SKU under the other trend basis
    run: Mapped[PlanningRun] = relationship(back_populates="recommendations")
    decisions: Mapped[list["PlannerDecision"]] = relationship(
        back_populates="recommendation", order_by="PlannerDecision.id"
    )


class PlannerDecision(Base):
    """Append-only: the latest row for a recommendation is the current decision."""

    __tablename__ = "planner_decision"
    id: Mapped[int] = mapped_column(primary_key=True)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("recommendation.id"), index=True)
    action: Mapped[str]  # approve | edit | reject
    qty: Mapped[int]
    reason: Mapped[str | None]
    decided_by: Mapped[str]
    decided_at: Mapped[datetime]
    zoho_po_id: Mapped[str | None]
    zoho_status: Mapped[str | None]  # draft_created | failed | csv_only
    zoho_error: Mapped[str | None]
    recommendation: Mapped[Recommendation] = relationship(back_populates="decisions")


class AiBrief(Base):
    __tablename__ = "ai_brief"
    id: Mapped[int] = mapped_column(primary_key=True)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("recommendation.id"), index=True)
    status: Mapped[str]  # ok | failed
    model: Mapped[str]
    brief: Mapped[dict | None] = mapped_column(JSON)
    grounding: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None]
    created_at: Mapped[datetime]
