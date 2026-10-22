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
