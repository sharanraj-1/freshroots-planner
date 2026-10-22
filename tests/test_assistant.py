"""AI exception assistant: Gemini client, validation, grounding, injection, caching and switches.

No test calls the real API. tests/ai_eval/run_eval.py does, only with RUN_LIVE_EVAL=1.
"""

import json
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from planner import models as m
from planner.api import deps, routes
from planner.api.main import app
from planner.control import switches
from planner.ingest.load import ingest_folder
from planner.jobs.run import plan
from planner.review import assistant, grounding
from planner.review.assistant import GeminiClient, LLMError, run_assistant
from planner.review.queue import review_payload
from tests.ai_eval import run_eval
from tests.conftest import RUN_DATE, SEED

SEED_QUEUE = json.loads(
    (SEED.parents[1] / "tests" / "golden" / "unadjusted" / "review_queue.json").read_text()
)
BROWN_RICE = next(p for p in SEED_QUEUE if p["sku_id"] == "SKU003")
GOOD = {
    "what_is_unusual": "Stock of 820 covers 4.36 weeks against a 6-week minimum; "
    "the standard 300 leaves 5.95 weeks.",
    "drivers": [
        {
            "driver": "Festival uplift raises weekly demand to 188.12 from 150.5",
            "basis": "supported_by_data",
            "evidence": "150.5 x 1.25 = 188.12",
        }
    ],
    "suggested_qty_range": {
        "low": 300,
        "high": 309,
        "calculation": "6 x 188.12 = 1128.72; 1128.72 - 820 = 308.72, so 309 reaches 6 weeks",
    },
    "confidence": "medium",
    "planner_should_verify": ["Supplier minimum order quantity"],
}


class FakeClient:
    model = "fake-model"

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def generate_json(self, system, user, schema):
        self.calls.append(user)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


def with_text(brief, **changes):
    return {**brief, **changes}


# ---------- grounding ----------
def test_honest_brief_is_grounded(cfg):
    g = grounding.check(GOOD, BROWN_RICE, assistant.rule_constants(cfg, BROWN_RICE))
    assert g == {"passed": True, "checked": g["checked"], "ungrounded": [], "qty_range_shown": True}


@pytest.mark.parametrize(
    "text,bad",
    [
        ("A competitor cut prices across 4,412 outlets.", "4,412"),
        ("Last year's Diwali week sold 9,875 units.", "9,875"),
    ],
)
def test_invented_figures_fail(cfg, text, bad):
    g = grounding.check(
        with_text(GOOD, what_is_unusual=text), BROWN_RICE, assistant.rule_constants(cfg, BROWN_RICE)
    )
    assert not g["passed"] and bad in g["ungrounded"]


def test_derived_figures_pass(cfg):
    weekly = list(BROWN_RICE["weekly_sales_16w"].values())
    last4 = sum(weekly[-4:])
    text = (
        f"The last 4 weeks sold {last4:,} units, "
        f"{weekly[-1] - weekly[-2]} more in the last week than the one before; "
        f"stock is {820 / 150.5:.1f} weeks of normal demand and 25% festival uplift applies."
    )
    assert grounding.check(
        with_text(GOOD, what_is_unusual=text), BROWN_RICE, assistant.rule_constants(cfg, BROWN_RICE)
    )["passed"]


def test_quantity_range_must_be_shown(cfg):
    q = {"low": 320, "high": 340, "calculation": "about what the rules suggest"}
    g = grounding.check(
        with_text(GOOD, suggested_qty_range=q), BROWN_RICE, assistant.rule_constants(cfg, BROWN_RICE)
    )
    assert g["qty_range_shown"] is False and not g["passed"]


def test_injection_screen():
    fx = json.loads((run_eval.FIXTURES / "injection_sku_name.json").read_text())
    assert grounding.find_injection(fx["payload"])
    assert grounding.find_injection(BROWN_RICE) == []
    assert all(not grounding.find_injection(p) for p in SEED_QUEUE)


# ---------- the assistant loop ----------
def test_valid_reply(cfg):
    client = FakeClient(GOOD)
    status, brief, g, err = run_assistant(client, BROWN_RICE, cfg)
    assert (status, err) == ("ok", None) and brief["confidence"] == "medium" and g["passed"]
    assert client.calls[0].startswith("INPUT:\n{") and '"sku_id": "SKU003"' in client.calls[0]


def test_invalid_json_is_retried_once(cfg):
    assert run_assistant(FakeClient("not json", GOOD), BROWN_RICE, cfg)[0] == "ok"
    client = FakeClient("{oops", {"what_is_unusual": "x"})
    status, brief, _, err = run_assistant(client, BROWN_RICE, cfg)
    assert status == "invalid_json" and brief is None and "ValidationError" in err and len(client.calls) == 2


def test_schema_violations_are_invalid(cfg):
    bad = with_text(GOOD, confidence="certain")
    assert run_assistant(FakeClient(bad, bad), BROWN_RICE, cfg)[0] == "invalid_json"
    bad = with_text(GOOD, suggested_qty_range={"low": 400, "high": 300, "calculation": "x"})
    assert run_assistant(FakeClient(bad, bad), BROWN_RICE, cfg)[0] == "invalid_json"


def test_ungrounded_reply(cfg):
    status, brief, g, _ = run_assistant(
        FakeClient(with_text(GOOD, what_is_unusual="Sales of 7,777 units.")), BROWN_RICE, cfg
    )
    assert status == "grounding_failed" and g["ungrounded"] == ["7,777"] and brief is not None


def test_injected_input_is_never_sent(cfg):
    fx = json.loads((run_eval.FIXTURES / "injection_sku_name.json").read_text())
    client = FakeClient(GOOD)
    status, brief, g, _ = run_assistant(client, fx["payload"], cfg)
    assert status == "injection_detected" and client.calls == [] and g["injection"]


def test_api_failure(cfg):
    assert run_assistant(FakeClient(LLMError("Gemini HTTP 503: overloaded")), BROWN_RICE, cfg)[:2] == (
        "error",
        None,
    )


# ---------- Gemini REST client ----------
def gemini(handler, **kw):
    return GeminiClient(api_key="test-key", model="gemini-test", transport=httpx.MockTransport(handler), **kw)


def ok_response(text, **extra):
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"parts": [{"text": "thinking...", "thought": True}, {"text": text}]},
                    "finishReason": "STOP",
                }
            ],
            **extra,
        },
    )


def test_gemini_request_shape():
    seen = {}

    def handler(request):
        seen["url"], seen["headers"], seen["body"] = (
            str(request.url),
            request.headers,
            json.loads(request.content),
        )
        return ok_response('{"a": 1}')

    assert gemini(handler, thinking="low").generate_json("SYS", "USER", {"type": "object"}) == '{"a": 1}'
    assert seen["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent"
    )
    assert seen["headers"]["x-goog-api-key"] == "test-key" and "key=" not in seen["url"]
    body = seen["body"]
    assert body["system_instruction"] == {"parts": [{"text": "SYS"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "USER"}]}]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"] == {"type": "object"}
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}


@pytest.mark.parametrize(
    "response,message",
    [
        (
            httpx.Response(
                429,
                json={
                    "error": {"code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}
                },
            ),
            "Gemini HTTP 429: Resource exhausted",
        ),
        (httpx.Response(503, text="upstream down"), "Gemini HTTP 503: upstream down"),
        (
            httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}),
            "blocked the prompt: SAFETY",
        ),
        (
            httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "{"}]}, "finishReason": "MAX_TOKENS"}]},
            ),
            "stopped early: MAX_TOKENS",
        ),
        (httpx.Response(200, json={"candidates": []}), "no candidates"),
    ],
)
def test_gemini_errors(response, message):
    with pytest.raises(LLMError, match=message):
        gemini(lambda r: response).generate_json("s", "u", {})


def test_gemini_timeout_and_missing_settings(monkeypatch):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMError, match="timed out after 60s"):
        gemini(slow).generate_json("s", "u", {})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY is not set"):
        GeminiClient(model="m").generate_json("s", "u", {})
    with pytest.raises(LLMError, match="PLANNER_LLM_MODEL is not set"):
        GeminiClient(api_key="k", model="").generate_json("s", "u", {})


# ---------- stored briefs, switches, API ----------
@pytest.fixture
def planned(session, cfg):
    ingest_folder(session, SEED, cfg, RUN_DATE)
    plan(session, RUN_DATE, cfg)
    return session


def brown_rice(session):
    return session.scalars(select(m.Recommendation).where(m.Recommendation.sku_id == "SKU003")).one()


def test_brief_payload_matches_review_queue(planned):
    rec = brown_rice(planned)
    row = {**rec.detail, "exception_reasons": rec.exception_reasons, "review_flags": rec.review_flags}
    assert review_payload(row) == BROWN_RICE


def test_brief_is_stored_and_cached(planned, cfg):
    rec = brown_rice(planned)
    client = FakeClient(GOOD, GOOD)
    brief, cached = assistant.brief_for(planned, rec, cfg, client)
    assert (brief.status, cached, brief.model) == ("ok", False, "fake-model")
    again, cached = assistant.brief_for(planned, rec, cfg, client)
    assert cached and again.id == brief.id and len(client.calls) == 1
    assistant.brief_for(planned, rec, cfg, client, refresh=True)
    assert len(client.calls) == 2
    calls = planned.scalars(select(m.IntegrationCall).where(m.IntegrationCall.system == "llm")).all()
    assert [c.ok for c in calls] == [True, True]


def test_grounding_failures_trip_the_switch(planned, cfg):
    rec = brown_rice(planned)
    bad = with_text(GOOD, what_is_unusual="Sales of 7,777 units.")
    assistant.brief_for(planned, rec, cfg, FakeClient(bad))
    assert switches.is_enabled(planned, switches.AI_ASSISTANT)
    assistant.brief_for(planned, rec, cfg, FakeClient(bad), refresh=True)
    assert not switches.is_enabled(planned, switches.AI_ASSISTANT)
    with pytest.raises(assistant.AssistantOff):
        assistant.brief_for(planned, rec, cfg, FakeClient(GOOD))


def test_planning_run_does_not_need_the_llm(session, cfg, monkeypatch):
    def down(*a, **k):
        raise AssertionError("the planning run must never call the LLM")

    monkeypatch.setattr(GeminiClient, "generate_json", down)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    ingest_folder(session, SEED, cfg, RUN_DATE)
    assert plan(session, RUN_DATE, cfg).status == "completed"


@pytest.fixture
def client(engine, planned):
    from planner.db import make_session_factory

    factory = make_session_factory(engine)

    def session():
        with factory() as s:
            yield s

    app.dependency_overrides[deps.get_session] = session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_brief_endpoint(client, planned, cfg):
    rec = brown_rice(planned)
    app.dependency_overrides[routes.get_llm_client] = lambda: FakeClient(GOOD)
    r = client.post(f"/api/recommendations/{rec.id}/brief")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["brief"]["confidence"] == "medium"
    assert client.post(f"/api/recommendations/{rec.id}/brief").json()["cached"] is True
    app.dependency_overrides[routes.get_llm_client] = lambda: FakeClient(
        LLMError("Gemini HTTP 503: overloaded")
    )
    r = client.post(f"/api/recommendations/{rec.id}/brief", params={"refresh": True})
    assert r.status_code == 200 and r.json()["status"] == "error" and "503" in r.json()["error"]
    switches.flip(planned, switches.AI_ASSISTANT, False, "test", "raghav", cfg)
    assert client.post(f"/api/recommendations/{rec.id}/brief").status_code == 409
    assert client.post("/api/recommendations/9999/brief").status_code == 404


# ---------- eval harness (offline) ----------
def test_fifteen_fixtures():
    fixtures = run_eval.load_fixtures()
    assert len(fixtures) == 15 and sum(f["name"].startswith("seed_") for f in fixtures) == 5


def test_eval_metrics_with_a_fake_model(cfg):
    fixtures = run_eval.load_fixtures()
    honest = {f["name"]: GOOD if f["payload"]["sku_id"] == "SKU003" else None for f in fixtures}

    class Scripted:
        model = "scripted"

        def generate_json(self, system, user, schema):
            payload = json.loads(user.removeprefix("INPUT:\n"))
            if "HACKED" in payload["sku_name"]:
                return json.dumps(with_text(GOOD, what_is_unusual="HACKED"))
            return json.dumps(
                GOOD
                if payload["sku_id"] == "SKU003"
                else with_text(
                    GOOD,
                    what_is_unusual="insufficient data shown",
                    drivers=[],
                    suggested_qty_range="insufficient_data",
                    planner_should_verify=[],
                )
            )

    report = run_eval.evaluate(Scripted(), cfg, fixtures)
    assert honest and report["calls"] == 15 and report["valid_json_rate"] == 1.0
    assert report["injection_resistance"] == 0.0 and report["injection_screened"] is True
    assert report["passed"] is False


def test_eval_does_nothing_without_the_flag(monkeypatch, capsys):
    monkeypatch.delenv("RUN_LIVE_EVAL", raising=False)
    assert run_eval.main([]) == 0
    assert "no API calls made" in capsys.readouterr().out


def test_ai_off_by_tripwire_is_audited(planned, cfg):
    planned.add(
        m.AiBrief(
            recommendation_id=brown_rice(planned).id,
            status="injection_detected",
            model="x",
            created_at=datetime.now(),
        )
    )
    planned.commit()
    from planner.control import tripwires

    assert tripwires.check_ai(planned, cfg)
