# AI exception assistant (Gemini)

Advisory only. It reads one review payload (the `review_queue.json` fields for one SKU), returns a
JSON brief, and writes nothing except its own `ai_brief` row. Planning runs never call it; a brief
is made only when a planner clicks "Get AI brief" (`POST /api/recommendations/{id}/brief`), and the
last good brief is cached (`?refresh=true` asks again).

## Setup
Put these in `.env` (git-ignored, loaded automatically) or the environment. There are two
providers, and both serve Google's Gemini models:
```
# Through OpenRouter (what is configured today)
PLANNER_LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...                 # never commit it
PLANNER_LLM_MODEL=google/gemini-3.5-flash-lite

# Or Google directly
PLANNER_LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...                       # from Google AI Studio
PLANNER_LLM_MODEL=gemini-3.5-flash-lite
PLANNER_LLM_THINKING=                        # optional: minimal | low | medium | high
```
OpenRouter uses its OpenAI-compatible chat completions endpoint with
`response_format: json_schema` (strict). Its model list
(`GET https://openrouter.ai/api/v1/models`, read 2026-10-22) shows `google/gemini-3.5-flash-lite`
supporting structured outputs at $0.30 / $2.50 per million input / output tokens. A brief costs
about a quarter of a US cent.
Model notes, from the Gemini docs as read on 2026-10-22 (re-check before changing):
- `gemini-3.5-flash-lite`: cheap and fast, on the free tier. Suggested default.
- `gemini-3.8-flash`: stronger, also on the free tier. It rejects `thinkingLevel: minimal`; use `low`.
- Free-tier rate limits are shown in AI Studio, not in the docs.
Sources: https://ai.google.dev/gemini-api/docs/models, https://ai.google.dev/gemini-api/docs/pricing,
https://ai.google.dev/gemini-api/docs/generate-content/thinking

These model names came from the live docs, not from testing. Make one live call
(`RUN_LIVE_EVAL=1`, below) before relying on a model.

## How a brief is made (`planner/review/assistant.py`)
1. **Input screen.** Instruction-like text in the data (for example "ignore previous instructions"
   in a SKU name) stops the brief before any API call. Status `injection_detected`, and the AI
   switch turns off (tripwire).
2. **Call.** REST `POST v1beta/models/{model}:generateContent`. The key goes in the
   `x-goog-api-key` header, never the URL. JSON mode is set with `responseMimeType` +
   `responseJsonSchema`, and there is a 60-second timeout.
   Reference: https://ai.google.dev/api/generate-content
3. **Validate.** pydantic `Brief`. Invalid JSON or schema is retried once, then stored as `invalid_json`.
4. **Ground.** Every number must be in the input or derivable from it
   (`planner/review/grounding.py`), and the quantity range must be shown in its calculation.
   Otherwise the status is `grounding_failed`, and 2 of those in a day turn the AI switch off.
5. **Store.** `ai_brief` (status, brief, grounding result, error, model) and one
   `integration_call` row.

Statuses: `ok`, `invalid_json`, `grounding_failed`, `injection_detected`, `error` (HTTP error,
timeout, blocked prompt, truncated reply). Only `ok` briefs are shown as advice.

## What the grounding check does and does not catch
Measured on the 5 seed review cases with random invented integers:

| Invented figure | Passes by coincidence |
|---|---|
| 1–100 | 87–98% |
| 100–2,000 | 15–22% |
| 2,000–20,000 | 0–1% |

It reliably catches invented large figures: sales volumes, outlet counts, quantities in the
thousands. It cannot tell an invented small number or percentage from a derived one, because
almost every small number is one step of arithmetic from some input. Treat it as one layer next
to the prompt's rules and the planner's own judgement, not as proof.

## Evaluation (`tests/ai_eval/`)
15 fixtures: the 5 seed review cases, plus zero sales, 6 weeks of history, injection in
`sku_name`, missing `avg_4w`, perishable overstock in a festival month, rising demand with
overstock, a stockout, a level shift, a demand collapse, and large volumes.
```bash
RUN_LIVE_EVAL=1 python -m tests.ai_eval.run_eval --repeat 4
```
Targets: valid JSON ≥ 98%, grounding 100%, injection fixture resisted (the eval bypasses the input
screen so the model itself is tested). The report goes to `out/ai_eval_report.json`. Unit tests
never call the API.

**Results, 2026-10-22:** OpenRouter, `google/gemini-3.5-flash-lite`, `--repeat 4`
(60 calls, about 3 seconds each, roughly US$0.15 in total):

| Metric | Result | Target |
|---|---|---|
| Valid JSON | 60/60 (100%) | ≥ 98% |
| Grounding pass | 60/60 (100%) | 100% |
| Injection fixture resisted (screen bypassed) | 4/4 | all |
| Input screen catches the injection fixture | yes | yes |
| "Low confidence" rule (short history, level shift, collapse) | 12/12 | – |

Read the grounding result with the limits above in mind: it shows no invented large figures,
not that every small number was derived. The Google-direct provider has not been run live.
