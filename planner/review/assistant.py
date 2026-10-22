"""AI exception assistant: one advisory brief for one recommendation, from Google Gemini.

Advisory and read-only: it has no tools and writes nothing except its own ai_brief row.
The planning run never calls it; briefs are made only when a planner asks.

Environment: GEMINI_API_KEY, PLANNER_LLM_MODEL, optional PLANNER_LLM_THINKING (e.g. "low").
API reference: https://ai.google.dev/api/generate-content (v1beta generateContent, JSON output).
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select

from planner import models as m
from planner.control import switches, tripwires
from planner.review import grounding
from planner.review.queue import review_payload

log = logging.getLogger("planner.assistant")
PROMPT = (Path(__file__).parent / "exception_agent_prompt.md").read_text(encoding="utf-8")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
TIMEOUT_S = 60


# ---------- the brief contract ----------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Driver(_Strict):
    driver: str = Field(min_length=1)
    basis: Literal["supported_by_data", "hypothesis"]
    evidence: str = ""


class QtyRange(_Strict):
    low: float = Field(ge=0)
    high: float = Field(ge=0)
    calculation: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self):
        if self.high < self.low:
            raise ValueError("high is below low")
        return self


class Brief(_Strict):
    what_is_unusual: str = Field(min_length=1)
    drivers: list[Driver]
    suggested_qty_range: QtyRange | Literal["insufficient_data"]
    confidence: Literal["low", "medium", "high"]
    planner_should_verify: list[str]


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "what_is_unusual": {"type": "string"},
        "drivers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "driver": {"type": "string"},
                    "basis": {"type": "string", "enum": ["supported_by_data", "hypothesis"]},
                    "evidence": {"type": "string"},
                },
                "required": ["driver", "basis", "evidence"],
            },
        },
        "suggested_qty_range": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "low": {"type": "number", "minimum": 0},
                        "high": {"type": "number", "minimum": 0},
                        "calculation": {"type": "string"},
                    },
                    "required": ["low", "high", "calculation"],
                },
                {"type": "string", "enum": ["insufficient_data"]},
            ]
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "planner_should_verify": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["what_is_unusual", "drivers", "suggested_qty_range", "confidence", "planner_should_verify"],
}


# ---------- Gemini over REST ----------
class LLMError(Exception):
    pass


class GeminiClient:
    def __init__(self, api_key=None, model=None, thinking=None, timeout=TIMEOUT_S, transport=None):
        self.api_key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
        self.model = model or os.environ.get("PLANNER_LLM_MODEL", "")
        self.thinking = thinking if thinking is not None else os.environ.get("PLANNER_LLM_THINKING", "")
        self.timeout = timeout
        self.transport = transport

    def generate_json(self, system, user, schema):
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set")
        if not self.model:
            raise LLMError("PLANNER_LLM_MODEL is not set")
        config = {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "maxOutputTokens": 8192,
        }
        if self.thinking:
            config["thinkingConfig"] = {"thinkingLevel": self.thinking}
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }
        url = f"{GEMINI_BASE}/models/{self.model}:generateContent"
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as http:
                r = http.post(url, json=body, headers={"x-goog-api-key": self.api_key})
        except httpx.TimeoutException as e:
            raise LLMError(f"Gemini timed out after {self.timeout}s") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Gemini unreachable: {type(e).__name__}") from e
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200:
            err = data.get("error") if isinstance(data, dict) else None
            msg = err.get("message") if isinstance(err, dict) else r.text[:200]
            raise LLMError(f"Gemini HTTP {r.status_code}: {msg}")
        block = (data.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise LLMError(f"Gemini blocked the prompt: {block}")
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError("Gemini returned no candidates")
        cand = candidates[0]
        finish = cand.get("finishReason")
        if finish not in (None, "STOP"):
            raise LLMError(f"Gemini stopped early: {finish}")
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise LLMError("Gemini returned an empty reply")
        return text


# ---------- making a brief ----------
def rule_constants(cfg, payload):
    """Rule settings a brief may quote ("12-week average", "1.5x"), plus the series length."""
    return [
        cfg["baseline_weeks"],
        cfg["trend_weeks"],
        cfg["festival_uplift"],
        1 + cfg["festival_uplift"],
        cfg["high_demand_threshold"],
        1 + cfg["high_demand_threshold"],
        cfg["high_demand_multiplier"],
        cfg["review_flags"]["min_history_weeks"],
        len(payload.get("weekly_sales_16w") or {}),
    ]


def parse_brief(text):
    try:
        return Brief.model_validate(json.loads(text)), None
    except (ValueError, ValidationError) as e:
        return None, f"{type(e).__name__}: {str(e)[:300]}"


def run_assistant(client, payload, cfg, screen_input=True):
    """The pure part: screen, call (retry once on invalid JSON), validate and ground.

    Returns (status, brief_dict | None, grounding | None, error | None).
    """
    hits = grounding.find_injection(payload)
    if screen_input and hits:
        return "injection_detected", None, {"injection": hits}, "instruction-like text in the input data"
    user = "INPUT:\n" + json.dumps(payload, ensure_ascii=False)
    error = None
    for _attempt in range(2):
        try:
            text = client.generate_json(PROMPT, user, RESPONSE_SCHEMA)
        except LLMError as e:
            return "error", None, None, str(e)
        brief, error = parse_brief(text)
        if brief:
            break
    else:
        return "invalid_json", None, None, error
    data = brief.model_dump()
    g = grounding.check(data, payload, rule_constants(cfg, payload))
    if hits:
        g["injection"] = hits
    return ("ok" if g["passed"] else "grounding_failed"), data, g, None


def latest_brief(session, rec_id):
    return session.scalar(
        select(m.AiBrief).where(m.AiBrief.recommendation_id == rec_id).order_by(m.AiBrief.id.desc()).limit(1)
    )


class AssistantOff(Exception):
    pass


def brief_for(session, rec, cfg, client=None, refresh=False):
    """Return the cached ok brief, or make (and store) a new one."""
    if not switches.is_enabled(session, switches.AI_ASSISTANT) or not switches.is_enabled(
        session, switches.PLANNER
    ):
        raise AssistantOff("the AI assistant is switched off")
    cached = latest_brief(session, rec.id)
    if cached and cached.status == "ok" and not refresh:
        return cached, True
    client = client or GeminiClient()
    row = {**rec.detail, "exception_reasons": rec.exception_reasons, "review_flags": rec.review_flags}
    payload = review_payload(row)
    status, data, g, error = run_assistant(client, payload, cfg)
    brief = m.AiBrief(
        recommendation_id=rec.id,
        status=status,
        model=client.model or "unset",
        brief=data,
        grounding=g,
        error=error,
        created_at=datetime.now(),
    )
    session.add(brief)
    if status != "injection_detected":
        session.add(
            m.IntegrationCall(system="llm", ok=status != "error", called_at=datetime.now(), detail=error)
        )
    session.commit()
    log.info("AI brief for recommendation %s: %s", rec.id, status)
    tripwires.check_ai(session, cfg)
    return brief, False
