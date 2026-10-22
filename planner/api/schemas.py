from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RunOut(_Out):
    id: int
    run_date: date
    status: str
    trend_basis: str
    festival_month: bool | None
    summary: dict | None
    started_at: datetime
    finished_at: datetime | None
    error: str | None
    decided: int = 0


class RunDetail(RunOut):
    data_checks: list
    config: dict


class DecisionOut(_Out):
    id: int
    action: str
    qty: int
    reason: str | None
    decided_by: str
    decided_at: datetime
    zoho_po_id: str | None
    zoho_status: str | None
    zoho_error: str | None


class RecommendationOut(_Out):
    id: int
    run_id: int
    sku_id: str
    decision: str
    recommended_qty: int
    needs_review: bool
    detail: dict
    review_flags: list
    exception_reasons: list
    rule_trace: list
    comparison: dict | None
    current_decision: DecisionOut | None = None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "edit", "reject"]
    qty: int | None = Field(None, ge=0)
    reason: str | None = None
