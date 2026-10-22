# AI exception assistant (Gemini)

Advisory only. It reads one review payload (the `review_queue.json` fields for one SKU), returns a
JSON brief, and writes nothing except its own `ai_brief` row. Planning runs never call it; a brief
is made only when a planner clicks "Get AI brief" (`POST /api/recommendations/{id}/brief`), and the
last good brief is cached (`?refresh=true` asks again).

## Setup
```
GEMINI_API_KEY=...                  # from Google AI Studio; never commit it
PLANNER_LLM_MODEL=gemini-3.5-flash-lite
PLANNER_LLM_THINKING=               # optional: minimal | low | medium | high
```
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

**Status:** not run live yet. No Gemini key has been provided.
