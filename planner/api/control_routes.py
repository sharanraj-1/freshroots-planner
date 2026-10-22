"""Kill switches, held runs and system status."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from planner import models as m
from planner.api.deps import current_user, get_config, get_session
from planner.control import switches, tripwires
from planner.control.status import status

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
