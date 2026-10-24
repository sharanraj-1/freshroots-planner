"""The Vercel demo: a fresh database seeds itself once and serves the seed run."""

from fastapi.testclient import TestClient

from planner import demo
from planner.api import deps


def test_demo_bootstrap_seeds_once_and_serves(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'demo.db').as_posix()}")
    monkeypatch.setenv("PLANNER_DEMO", "1")
    monkeypatch.setattr(deps, "_factory", None)
    demo.bootstrap()
    demo.bootstrap()  # a second cold start on the same disk does not plan again
    from planner.api.main import app

    client = TestClient(app)
    runs = client.get("/api/runs").json()
    assert len(runs) == 1 and runs[0]["summary"]["po_units"] == 1820
    assert client.get("/api/meta").json()["demo"] is True
    assert "Freshroots" in client.get("/").text
    assert client.get("/health").json()["ok"] is True
    monkeypatch.setattr(deps, "_factory", None)


def test_demo_with_zoho_loads_the_mapping_and_turns_the_switch_on(tmp_path, monkeypatch):
    from sqlalchemy import select

    from planner import models as m
    from planner.control import switches
    from planner.db import make_session_factory

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'demo.db').as_posix()}")
    monkeypatch.setenv("ZOHO_API_ENABLED", "true")
    demo.bootstrap()
    with make_session_factory()() as s:
        assert switches.is_enabled(s, switches.ZOHO_API)
        audit = s.scalars(select(m.SwitchAudit).where(m.SwitchAudit.changed_by == "demo-bootstrap")).one()
        assert audit.new_value is True and "Zoho Books connected" in audit.reason
        suppliers = s.scalars(select(m.Supplier)).all()
        assert len(suppliers) == 15 and all(x.zoho_vendor_id and x.zoho_item_id for x in suppliers)
