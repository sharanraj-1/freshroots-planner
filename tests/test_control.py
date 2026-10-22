"""Kill switches, tripwires, the dead-man alert and /status."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from planner import models as m
from planner.api import deps
from planner.api.main import app
from planner.control import __main__ as control_cli
from planner.control import alerts, switches, tripwires
from planner.control.status import status
from planner.ingest.load import ingest_folder
from planner.jobs import deadman
from planner.jobs.run import PlannerPaused, RunLocked, plan
from tests.conftest import RUN_DATE, SEED

ADMIN = "raghav"


@pytest.fixture
def loaded(session, cfg):
    assert ingest_folder(session, SEED, cfg, RUN_DATE).loaded
    return session


def audit_rows(session, name=None):
    q = select(m.SwitchAudit).where(m.SwitchAudit.source != "install")
    return session.scalars(q.where(m.SwitchAudit.name == name) if name else q).all()


# ---------- switches ----------
def test_defaults(session):
    assert {n: switches.is_enabled(session, n) for n in switches.SWITCHES} == switches.DEFAULTS
    assert switches.DEFAULTS[switches.ZOHO_API] is False


def test_flip_is_audited(session, cfg):
    switches.flip(session, switches.AI_ASSISTANT, False, "model is misbehaving", ADMIN, cfg)
    assert not switches.is_enabled(session, switches.AI_ASSISTANT)
    (a,) = audit_rows(session, switches.AI_ASSISTANT)
    assert (a.old_value, a.new_value, a.changed_by, a.reason, a.source) == (
        True,
        False,
        ADMIN,
        "model is misbehaving",
        "cli",
    )


@pytest.mark.parametrize(
    "name,reason,user,error",
    [
        ("planner_enabled", "", ADMIN, "a reason is required"),
        ("planner_enabled", "   ", ADMIN, "a reason is required"),
        ("planner_enabled", "testing", "intern", "not in switch_admins"),
        ("coffee_enabled", "testing", ADMIN, "unknown switch"),
    ],
)
def test_flip_refusals(session, cfg, name, reason, user, error):
    with pytest.raises(switches.SwitchError, match=error):
        switches.flip(session, name, False, reason, user, cfg)
    assert audit_rows(session) == []


def test_tripwires_cannot_turn_switches_on(session, cfg):
    with pytest.raises(switches.SwitchError, match="only turn switches off"):
        switches.flip(session, switches.ZOHO_API, True, "auto", "tripwire", cfg, source="tripwire")


def test_off_all_in_one_command(engine, session, cfg, monkeypatch):
    from planner.db import make_session_factory

    monkeypatch.setattr(control_cli, "make_session_factory", lambda: make_session_factory(engine))
    monkeypatch.setenv("PLANNER_USER", ADMIN)
    assert control_cli.main(["off", "all", "--reason", "supplier data corrupted"]) == 0
    session.expire_all()
    assert not any(switches.is_enabled(session, n) for n in switches.SWITCHES)
    assert len(audit_rows(session)) == 5
    assert control_cli.main(["on", "all", "--reason", "x"]) == 1  # on all is not allowed
    assert control_cli.main(["on", "planner_enabled", "--reason", "fixed", "--user", "intern"]) == 1
    assert control_cli.main(["on", "planner_enabled", "--reason", "fixed"]) == 0


def test_missing_switch_is_off(session):
    session.execute(text("DELETE FROM control_switch WHERE name = 'planner_enabled'"))
    session.commit()
    assert switches.is_enabled(session, switches.PLANNER) is False


def test_unreadable_switch_is_off(session):
    session.execute(text("DROP TABLE control_switch"))
    session.commit()
    assert switches.is_enabled(session, switches.PLANNER) is False
    assert status(session, {"data_freshness": {"max_inventory_age_days": 1, "max_sales_age_days": 13}})[
        "switches"
    ]["error"].startswith("OperationalError")


def test_paused_planner_refuses_to_run(loaded, cfg):
    switches.flip(loaded, switches.PLANNER, False, "Raghav on leave, manual process", ADMIN, cfg)
    with pytest.raises(PlannerPaused):
        plan(loaded, RUN_DATE, cfg)
    assert loaded.scalar(select(func.count()).select_from(m.PlanningRun)) == 0


# ---------- run tripwires ----------
def past_run(session, days_before, po_units, decisions=None, status="completed"):
    run = m.PlanningRun(
        run_date=date.fromisoformat(RUN_DATE) - timedelta(days=days_before),
        trend_basis="unadjusted",
        status=status,
        started_at=datetime.now(),
        finished_at=datetime.now(),
        summary={"po_units": po_units},
        config={},
        data_checks=[],
    )
    for sku, d in (decisions or {}).items():
        run.recommendations.append(
            m.Recommendation(
                sku_id=sku,
                decision=d,
                recommended_qty=0,
                needs_review=False,
                detail={},
                review_flags=[],
                exception_reasons=[],
                rule_trace=[],
            )
        )
    session.add(run)
    session.commit()
    return run


def tripwire(run, name):
    return next(t for t in run.tripwires if t["name"] == name)


def test_ratio_checks_skipped_until_four_runs(loaded, cfg):
    for i in range(3):
        past_run(loaded, 3 + 4 * i, 100)
    run = plan(loaded, RUN_DATE, cfg)
    assert run.status == "completed"
    assert {t["status"] for t in run.tripwires} == {"skipped"}


def test_po_unit_spike_holds_the_run(loaded, cfg):
    for i in range(4):
        past_run(loaded, 3 + 4 * i, 800)  # average 800; this run proposes 1,820 > 2 x 800
    run = plan(loaded, RUN_DATE, cfg)
    assert run.status == "held" and tripwire(run, "po_units_spike")["status"] == "tripped"
    assert "more than 2.0x the average of the last 4 runs (800)" in run.hold_reasons[0]
    alert = loaded.scalars(select(m.Alert).where(m.Alert.run_id == run.id)).one()
    assert alert.severity == "critical" and "HELD" in alert.message


def test_normal_run_is_not_held(loaded, cfg):
    skus = [r.sku_id for r in loaded.scalars(select(m.Sku))]
    for i in range(4):
        past_run(loaded, 3 + 4 * i, 1700, decisions={s: "HOLD" for s in skus} if i == 0 else None)
    # previous run had all HOLD: 7 of 15 changed (47%) > 30%
    run = plan(loaded, RUN_DATE, cfg)
    assert tripwire(run, "po_units_spike")["status"] == "ok"
    assert tripwire(run, "decision_changes")["status"] == "tripped" and run.status == "held"


def test_decision_changes_within_limit(loaded, cfg):
    first = plan(loaded, RUN_DATE, cfg)
    same = {r.sku_id: r.decision for r in first.recommendations}
    loaded.delete(first)
    loaded.commit()
    for i in range(4):
        past_run(loaded, 3 + 4 * i, 1800, decisions=same if i == 0 else None)
    run = plan(loaded, RUN_DATE, cfg)
    assert run.status == "completed"
    assert tripwire(run, "decision_changes")["detail"].startswith("0 of 15 SKUs (0%) changed")


def test_override_rate_warns_but_does_not_hold(loaded, cfg):
    skus = [r.sku_id for r in loaded.scalars(select(m.Sku))][:4]
    for i in range(4):
        prev = past_run(loaded, 3 + 4 * i, 1800, decisions={s: "REORDER" for s in skus})
        for j, rec in enumerate(prev.recommendations):
            loaded.add(
                m.PlannerDecision(
                    recommendation_id=rec.id,
                    action="edit" if j < 2 else "reject" if j < 3 else "approve",
                    qty=1,
                    reason="Other",
                    decided_by="raghav",
                    decided_at=datetime.now(),
                )
            )
    loaded.commit()
    run = plan(loaded, RUN_DATE, cfg)
    assert tripwire(run, "override_rate")["status"] == "warning"  # 12 of 16 overridden = 75%
    assert run.hold_reasons == [] or "override" not in " ".join(run.hold_reasons)
    assert loaded.scalar(select(func.count()).select_from(m.Alert).where(m.Alert.severity == "warning")) == 1


def test_held_run_needs_an_admin_release(loaded, cfg):
    for i in range(4):
        past_run(loaded, 3 + 4 * i, 100)
    run = plan(loaded, RUN_DATE, cfg)
    assert run.status == "held"
    with pytest.raises(RunLocked):
        plan(loaded, RUN_DATE, cfg)  # re-running cannot clear a hold
    loaded.rollback()
    with pytest.raises(switches.NotAdmin):
        tripwires.release(loaded, run, "checked with Raghav", "intern", cfg)
    with pytest.raises(switches.SwitchError, match="reason"):
        tripwires.release(loaded, run, "", ADMIN, cfg)
    tripwires.release(loaded, run, "Diwali stock-up confirmed with sales", ADMIN, cfg)
    assert (run.status, run.released_by) == ("completed", ADMIN)


# ---------- integration tripwires ----------
def brief(session, status, at):
    rec = session.scalars(select(m.Recommendation)).first()
    session.add(m.AiBrief(recommendation_id=rec.id, status=status, model="x", created_at=at))
    session.commit()


def test_ai_grounding_failures_turn_the_assistant_off(loaded, cfg):
    plan(loaded, RUN_DATE, cfg)
    now = datetime(2026, 10, 19, 15, 0)
    brief(loaded, "grounding_failed", now - timedelta(days=1))  # yesterday does not count
    brief(loaded, "grounding_failed", now - timedelta(hours=2))
    assert tripwires.check_ai(loaded, cfg, now) is False
    brief(loaded, "grounding_failed", now - timedelta(hours=1))
    assert tripwires.check_ai(loaded, cfg, now) is True
    assert not switches.is_enabled(loaded, switches.AI_ASSISTANT)
    (a,) = audit_rows(loaded, switches.AI_ASSISTANT)
    assert a.source == "tripwire" and "2 AI briefs failed the grounding check today" in a.reason


def test_any_injection_turns_the_assistant_off(loaded, cfg):
    plan(loaded, RUN_DATE, cfg)
    brief(loaded, "injection_detected", datetime.now())
    assert tripwires.check_ai(loaded, cfg) is True
    assert not switches.is_enabled(loaded, switches.AI_ASSISTANT)


def zoho_call(session, ok):
    session.add(m.IntegrationCall(system="zoho", ok=ok, called_at=datetime.now()))
    session.commit()


def test_three_zoho_failures_turn_the_api_off(session, cfg):
    switches.flip(session, switches.ZOHO_API, True, "Zoho org configured", ADMIN, cfg)
    for ok in (False, False, True, False, False):
        zoho_call(session, ok)
    assert tripwires.check_zoho(session, cfg) is False  # only 2 in a row
    zoho_call(session, False)
    assert tripwires.check_zoho(session, cfg) is True
    assert not switches.is_enabled(session, switches.ZOHO_API)


# ---------- dead-man ----------
MONDAY_0705 = datetime(2026, 10, 19, 7, 5)


def test_deadman_alerts_when_no_run_by_seven(session, cfg):
    alert = deadman.check(session, cfg, MONDAY_0705)
    assert alert.severity == "critical" and "had not completed by 07:00" in alert.message
    assert deadman.check(session, cfg, MONDAY_0705 + timedelta(minutes=30)) is alert  # not repeated


@pytest.mark.parametrize(
    "now",
    [datetime(2026, 10, 19, 6, 59), datetime(2026, 10, 20, 7, 5)],
    ids=["before deadline", "not a run day"],
)
def test_deadman_quiet(session, cfg, now):
    assert deadman.check(session, cfg, now) is None


def test_deadman_quiet_after_a_completed_run(loaded, cfg):
    plan(loaded, RUN_DATE, cfg)
    assert deadman.check(loaded, cfg, MONDAY_0705) is None


def test_deadman_reports_a_failed_run(loaded, cfg):
    loaded.add(
        m.PlanningRun(
            run_date=date(2026, 10, 19),
            trend_basis="unadjusted",
            status="failed",
            started_at=datetime.now(),
            config={},
            data_checks=[],
            error="inventory is 3 days old",
        )
    )
    loaded.commit()
    assert "The run failed: inventory is 3 days old" in deadman.check(loaded, cfg, MONDAY_0705).message


def test_deadman_quiet_when_runs_are_paused_on_purpose(session, cfg):
    switches.flip(session, switches.SCHEDULED_RUNS, False, "year-end freeze", ADMIN, cfg)
    assert deadman.check(session, cfg, MONDAY_0705) is None


# ---------- email ----------
class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_alert_email(session, cfg, monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(alerts.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    a = alerts.raise_alert(session, {**cfg, "alert_recipients": ["planning@example.test"]}, "deadman", "late")
    assert a.emailed and FakeSMTP.sent[0]["To"] == "planning@example.test"


def test_alert_without_smtp_is_still_stored(session, cfg, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    a = alerts.raise_alert(session, cfg, "deadman", "late")
    assert a.id and not a.emailed and "SMTP_HOST is not set" in a.email_error


# ---------- API ----------
@pytest.fixture
def client(engine, loaded, cfg):
    from planner.db import make_session_factory

    plan(loaded, RUN_DATE, cfg)
    factory = make_session_factory(engine)

    def session():
        with factory() as s:
            yield s

    app.dependency_overrides[deps.get_session] = session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_status_endpoint(client):
    for path in ("/status", "/api/status"):
        s = client.get(path).json()
        assert s["planner_enabled"] is True
        assert {x["name"]: x["enabled"] for x in s["switches"]}["zoho_api_enabled"] is False
        assert s["last_run"]["status"] == "completed" and s["held_runs"] == []
        assert s["data_freshness"]["latest_inventory_date"] == RUN_DATE


def test_switch_endpoint_and_paused_planner(client, loaded):
    rec = loaded.scalars(select(m.Recommendation)).first()
    r = client.post(
        "/api/switches/planner_enabled",
        json={"enabled": False, "reason": "test"},
        headers={"X-Planner-User": "intern"},
    )
    assert r.status_code == 403
    r = client.post(
        "/api/switches/planner_enabled",
        json={"enabled": False, "reason": "manual week"},
        headers={"X-Planner-User": ADMIN},
    )
    assert r.status_code == 200
    assert client.get("/status").json()["planner_enabled"] is False
    r = client.post(
        f"/api/recommendations/{rec.id}/decision",
        json={"action": "approve"},
        headers={"X-Planner-User": ADMIN},
    )
    assert r.status_code == 409 and "paused" in r.text
    r = client.post(
        "/api/switches/all", json={"enabled": True, "reason": "x"}, headers={"X-Planner-User": ADMIN}
    )
    assert r.status_code == 422


def test_held_run_blocks_decisions_and_export_until_released(client, loaded):
    run = loaded.scalars(select(m.PlanningRun)).one()
    run.status = "held"
    loaded.commit()
    rec = run.recommendations[0]
    hdr = {"X-Planner-User": ADMIN}
    assert (
        client.post(
            f"/api/recommendations/{rec.id}/decision", json={"action": "approve"}, headers=hdr
        ).status_code
        == 409
    )
    assert client.get(f"/api/runs/{run.id}/export/zoho.csv").status_code == 409
    assert (
        client.post(
            f"/api/runs/{run.id}/release", json={"reason": "ok"}, headers={"X-Planner-User": "intern"}
        ).status_code
        == 403
    )
    assert (
        client.post(f"/api/runs/{run.id}/release", json={"reason": "checked"}, headers=hdr).status_code == 200
    )
    assert (
        client.post(
            f"/api/recommendations/{rec.id}/decision", json={"action": "approve"}, headers=hdr
        ).status_code
        == 200
    )
