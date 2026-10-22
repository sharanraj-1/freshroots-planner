"""The site-wide password gate used on hosted deployments."""

import base64

import pytest
from fastapi.testclient import TestClient

from planner.api.main import app


def basic(password, user="raghav"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture
def client():
    return TestClient(app)


def test_open_when_no_password_is_set(client, monkeypatch):
    monkeypatch.delenv("PLANNER_ACCESS_PASSWORD", raising=False)
    assert client.get("/api/meta").status_code == 200


@pytest.mark.parametrize("path", ["/", "/app.js", "/api/meta", "/health"])
def test_everything_needs_the_password(client, monkeypatch, path):
    monkeypatch.setenv("PLANNER_ACCESS_PASSWORD", "s3cret-pass")
    r = client.get(path)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert client.get(path, headers=basic("wrong")).status_code == 401
    assert client.get(path, headers={"Authorization": "Basic !!!notbase64"}).status_code == 401
    assert client.get(path, headers=basic("s3cret-pass")).status_code in (200, 503)
