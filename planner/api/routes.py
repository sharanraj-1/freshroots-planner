"""Runs, recommendations, planner decisions and CSV exports."""

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from planner import models as m
from planner.api.deps import USER_HEADER, current_user, get_config, get_session
from planner.api.schemas import DecisionIn, DecisionOut, RecommendationOut, RunDetail, RunOut
from planner.control import switches
from planner.integrations import zoho
from planner.integrations.zoho_csv import HEADER as ZOHO_HEADER
from planner.review import assistant

router = APIRouter()
FILTERS = ("review", "REORDER", "PAUSE", "HOLD", "EXCEPTION")


def _run_or_404(session, run_id):
    run = session.get(m.PlanningRun, run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    return run


def _decided_counts(session):
    rows = session.execute(
        select(m.Recommendation.run_id, func.count(func.distinct(m.PlannerDecision.recommendation_id)))
        .join(m.PlannerDecision)
        .group_by(m.Recommendation.run_id)
    )
    return dict(rows.all())


def _current(rec):
    return rec.decisions[-1] if rec.decisions else None


def _rec_out(rec):
    out = RecommendationOut.model_validate(rec)
    current = _current(rec)
    out.current_decision = DecisionOut.model_validate(current) if current else None
    return out


@router.get("/meta")
def meta(cfg=Depends(get_config)):
    return {
        "decision_reasons": cfg["decision_reasons"],
        "switch_admins": cfg["switch_admins"],
        "user_header": USER_HEADER,
    }


@router.get("/runs", response_model=list[RunOut])
def list_runs(session: Session = Depends(get_session)):
    decided = _decided_counts(session)
    runs = session.scalars(select(m.PlanningRun).order_by(m.PlanningRun.run_date.desc())).all()
    return [RunOut.model_validate(r).model_copy(update={"decided": decided.get(r.id, 0)}) for r in runs]


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: int, session: Session = Depends(get_session)):
    run = _run_or_404(session, run_id)
    return RunDetail.model_validate(run).model_copy(
        update={"decided": _decided_counts(session).get(run.id, 0)}
    )


@router.get("/runs/{run_id}/recommendations", response_model=list[RecommendationOut])
def list_recommendations(
    run_id: int,
    filter: str | None = Query(None, description="review | REORDER | PAUSE | HOLD | EXCEPTION"),
    session: Session = Depends(get_session),
):
    run = _run_or_404(session, run_id)
    if filter is not None and filter not in FILTERS:
        raise HTTPException(422, f"filter must be one of {list(FILTERS)}")
    recs = run.recommendations
    if filter == "review":
        recs = [r for r in recs if r.needs_review]
    elif filter:
        recs = [r for r in recs if r.decision == filter]
    return [_rec_out(r) for r in recs]


@router.post("/recommendations/{rec_id}/decision", response_model=RecommendationOut)
def decide(
    rec_id: int,
    body: DecisionIn,
    user: str = Depends(current_user),
    cfg=Depends(get_config),
    session: Session = Depends(get_session),
):
    rec = session.get(m.Recommendation, rec_id)
    if rec is None:
        raise HTTPException(404, f"recommendation {rec_id} not found")
    if not switches.is_enabled(session, switches.PLANNER):
        raise HTTPException(409, "the planner is paused: use the manual process")
    if rec.run.status != "completed":
        raise HTTPException(409, f"run {rec.run_id} is {rec.run.status}; decisions are not allowed")
    current = _current(rec)
    if current and current.zoho_po_id:
        raise HTTPException(409, f"already in Zoho as draft PO {current.zoho_po_id}; change it there")

    if body.action == "approve":
        if body.qty is not None and body.qty != rec.recommended_qty:
            raise HTTPException(422, "approve keeps the recommended quantity; use edit to change it")
        qty = rec.recommended_qty
    elif body.action == "edit":
        if body.qty is None or body.qty < 1:
            raise HTTPException(422, "edit needs a quantity of 1 or more; use reject for no order")
        qty = body.qty
    else:
        if body.qty not in (None, 0):
            raise HTTPException(422, "reject cannot carry a quantity")
        qty = 0
    if body.action in ("edit", "reject") and body.reason not in cfg["decision_reasons"]:
        raise HTTPException(422, f"{body.action} needs a reason, one of {cfg['decision_reasons']}")

    session.add(
        m.PlannerDecision(
            recommendation_id=rec.id,
            action=body.action,
            qty=qty,
            reason=body.reason if body.action != "approve" else None,
            decided_by=user,
            decided_at=datetime.now(),
        )
    )
    session.commit()
    session.refresh(rec)
    return _rec_out(rec)


def _csv_response(header, rows, filename):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _names_and_vendors(session):
    names = dict(session.execute(select(m.Sku.sku_id, m.Sku.sku_name)).all())
    vendors = {s.sku_id: s for s in session.scalars(select(m.Supplier))}
    return names, vendors


@router.get("/runs/{run_id}/export/zoho.csv")
def export_zoho(run_id: int, session: Session = Depends(get_session)):
    """Approved and edited lines with qty > 0, status Draft. Never a released order."""
    run = _run_or_404(session, run_id)
    if run.status != "completed":
        raise HTTPException(409, f"run {run_id} is {run.status}; no PO export")
    names, vendors = _names_and_vendors(session)
    rows = []
    for rec in run.recommendations:
        d = _current(rec)
        if not d or d.action not in ("approve", "edit") or d.qty <= 0 or d.zoho_po_id:
            continue  # lines already in Zoho as drafts are not exported twice
        supplier = vendors.get(rec.sku_id)
        vendor = supplier.supplier_name if supplier and supplier.zoho_vendor_id else "TO BE ASSIGNED"
        reason = (
            rec.rule_trace[-1] if d.action == "approve" else f"Edited from {rec.recommended_qty}: {d.reason}"
        )
        rows.append(
            [
                rec.sku_id,
                names.get(rec.sku_id, ""),
                d.qty,
                vendor,
                "Draft",
                reason,
                "Yes" if rec.needs_review else "No",
                d.decided_by,
            ]
        )
    return _csv_response(ZOHO_HEADER + ["Decided By"], rows, f"zoho_draft_po_{run.run_date}.csv")


DECISION_HEADER = [
    "Run date",
    "Trend basis",
    "SKU",
    "SKU name",
    "Rule decision",
    "Rule qty",
    "Needs review",
    "Planner action",
    "Planner qty",
    "Reason",
    "Decided by",
    "Decided at",
    "Zoho PO id",
    "Zoho status",
]


@router.get("/runs/{run_id}/export/decisions.csv")
def export_decisions(run_id: int, session: Session = Depends(get_session)):
    """One row per recommendation with its current decision (blank when undecided)."""
    run = _run_or_404(session, run_id)
    names, _ = _names_and_vendors(session)
    rows = []
    for rec in run.recommendations:
        d = _current(rec)
        rows.append(
            [
                run.run_date,
                run.trend_basis,
                rec.sku_id,
                names.get(rec.sku_id, ""),
                rec.decision,
                rec.recommended_qty,
                "Yes" if rec.needs_review else "No",
            ]
            + (
                [
                    d.action,
                    d.qty,
                    d.reason or "",
                    d.decided_by,
                    d.decided_at.isoformat(timespec="seconds"),
                    d.zoho_po_id or "",
                    d.zoho_status or "",
                ]
                if d
                else [""] * 7
            )
        )
    return _csv_response(DECISION_HEADER, rows, f"planner_decisions_{run.run_date}.csv")


def get_llm_client():
    return assistant.GeminiClient()


@router.post("/recommendations/{rec_id}/brief")
def ai_brief(
    rec_id: int,
    refresh: bool = False,
    cfg=Depends(get_config),
    session: Session = Depends(get_session),
    client=Depends(get_llm_client),
):
    """Advisory brief for one recommendation. Cached; made only on request; never blocks a run."""
    rec = session.get(m.Recommendation, rec_id)
    if rec is None:
        raise HTTPException(404, f"recommendation {rec_id} not found")
    try:
        brief, cached = assistant.brief_for(session, rec, cfg, client, refresh)
    except assistant.AssistantOff as e:
        raise HTTPException(409, str(e)) from e
    return {
        "id": brief.id,
        "status": brief.status,
        "model": brief.model,
        "brief": brief.brief,
        "grounding": brief.grounding,
        "error": brief.error,
        "created_at": brief.created_at,
        "cached": cached,
    }


def get_zoho_client():
    return None  # push_run builds the client only after its gates pass


@router.post("/runs/{run_id}/push-to-zoho")
def push_to_zoho(
    run_id: int,
    user: str = Depends(current_user),
    cfg=Depends(get_config),
    session: Session = Depends(get_session),
    client=Depends(get_zoho_client),
):
    """Create draft POs in Zoho for approved lines. Lines without a vendor, or that fail, stay in the CSV."""
    run = _run_or_404(session, run_id)
    try:
        return zoho.push_run(session, run, cfg, client)
    except zoho.ForbiddenCall as e:
        raise HTTPException(500, str(e)) from e
    except zoho.ZohoError as e:
        raise HTTPException(409, str(e)) from e
