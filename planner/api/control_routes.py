"""Kill switches, held runs and system status."""

from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from planner import models as m
from planner.api.deps import current_user, get_config, get_session
from planner.control import switches, tripwires
from planner.control.status import status
from planner.jobs import pre_po_check

router = APIRouter()


class SwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    reason: str


class ReleaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


def _refused(e):
    return HTTPException(403 if isinstance(e, switches.NotAdmin) else 422, str(e))


@router.get("/status")
def get_status(session: Session = Depends(get_session), cfg=Depends(get_config)):
    return status(session, cfg)


@router.post("/switches/{name}")
def set_switch(
    name: str,
    body: SwitchIn,
    user: str = Depends(current_user),
    cfg=Depends(get_config),
    session: Session = Depends(get_session),
):
    try:
        if name == "all":
            if body.enabled:
                raise switches.SwitchError("'on all' is not allowed: turn switches on one at a time")
            switches.off_all(session, body.reason, user, cfg, source="api")
        else:
            switches.flip(session, name, body.enabled, body.reason, user, cfg, source="api")
    except switches.SwitchError as e:
        raise _refused(e) from e
    return status(session, cfg)["switches"]


@router.post("/runs/{run_id}/release")
def release_run(
    run_id: int,
    body: ReleaseIn,
    user: str = Depends(current_user),
    cfg=Depends(get_config),
    session: Session = Depends(get_session),
):
    run = session.get(m.PlanningRun, run_id)
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    try:
        tripwires.release(session, run, body.reason, user, cfg)
    except switches.SwitchError as e:
        raise _refused(e) from e
    return {"id": run.id, "status": run.status, "released_by": run.released_by}


class PrePoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku_ids: list[str] = Field(min_length=1)
    po_value: float = Field(ge=0)


@router.post("/pre-po-check")
def pre_po(body: PrePoIn, cfg=Depends(get_config), session: Session = Depends(get_session)):
    """On-demand check before releasing a PO. Below the value threshold no check is needed."""
    if body.po_value <= cfg["pre_po_check_value_inr"]:
        return {
            "checked": False,
            "reason": f"PO value is not above INR {cfg['pre_po_check_value_inr']:,.0f}",
            "results": [],
        }
    try:
        results = pre_po_check.check_skus(
            session, cfg, body.sku_ids, date.today(), f"a PO worth INR {body.po_value:,.0f}"
        )
    except pre_po_check.CheckOff as e:
        raise HTTPException(409, str(e)) from e
    return {"checked": True, "results": results, "alert": any(r["status"] == "dropped" for r in results)}


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: int, user: str = Depends(current_user), session: Session = Depends(get_session)):
    a = session.get(m.Alert, alert_id)
    if a is None:
        raise HTTPException(404, f"alert {alert_id} not found")
    a.resolved_at = datetime.now()
    a.message += f" [resolved by {user}]"
    session.commit()
    return {"id": a.id, "resolved_at": a.resolved_at}


@router.get("/health")
def health(session: Session = Depends(get_session), cfg=Depends(get_config)):
    """Liveness, when the last run finished, and whether the data is fresh."""
    from fastapi.responses import JSONResponse
    from sqlalchemy import text

    try:
        session.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "database": f"unreachable: {type(e).__name__}"}, status_code=503)
    s = status(session, cfg)
    return {
        "ok": True,
        "database": "ok",
        "planner_enabled": s["planner_enabled"],
        "last_run": s["last_run"],
        "data_freshness": s["data_freshness"],
    }
