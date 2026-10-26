# biz-performance-voice-agent

A voice AI that answers business-performance questions ("How much did I
sell this month?", "Which customers are overdue?", "Why is my profit down
even though sales are up?") by running a **real-time calculation engine**
against raw transaction data — never a precomputed lookup table, and never
with access to the hidden answer key used to evaluate it.

Built to work against the synthetic electronics-SME dataset from
[`costguard/synthetic_business/`](../costguard/synthetic_business/), but the
engine only assumes the same 9-file schema (sales/purchase/bank/inventory
transactions + products/suppliers/customers/business_events + business.json)
— point `DATA_DIR` at any dataset with that shape.

## Phase 1 (this repo, today) vs. Phase 2 (planned)

**Phase 1** — everything except live voice hardware: the calculation engine,
the LLM tool contract, session-scoped answer caching, full step-by-step
tracing, structured per-ask logging, hallucination detection, and an admin
API. Exercised over plain HTTP via `POST /ask` — no Sarvam/voice API keys
needed, just one LLM provider key.

**Phase 2** (not built yet) — swaps `dev_llm_client.py`'s HTTP-request loop
for the real Pipecat cascade pipeline (`bot.py`) with Sarvam STT/TTS in
Hindi/Hinglish and a WebRTC client, replicating
[`kyc-voice-agent`](../kyc-voice-agent)'s voice/model/infra exactly. Every
other file (`engine/`, `tools/`, `session_store.py`, `query_log.py`,
`admin.py`) is unchanged by that swap — only the transport layer around them
changes.

## Why real-time computation, not lookup

The calculation engine (`server/engine/`) reads *only* line-item transaction
tables (`sales_transactions.csv`, `purchase_transactions.csv`, etc.) and
computes every answer fresh with pandas, every time. It has no access to any
precomputed daily/weekly/monthly rollup — those files are simply never
copied into `server/data/` in the first place. Every compute function
returns a `(result, trace)` pair; the trace records each load/filter/
aggregate/formula step in order, so any number the engine reports can be
manually reconstructed from the raw CSVs.

## Ground-truth isolation

`ground_truth.json` and `business_questions.json` (the hidden answer key and
test-question suite) are **never reachable by the LLM or the engine, in any
form**:

1. `scripts/sync_data.py` is the only thing that ever touches a full
   `synthetic_business/output/<dataset>/` folder. It routes those two files
   into `eval/<dataset>/` — a top-level folder the engine's code never
   imports or points `DATA_DIR` at — and everything else into
   `server/data/<dataset>/`.
2. `server/engine/data_loader.py` additionally **hard-allowlists** the
   exact 9 filenames it will ever open, and raises if asked for anything
   else. Two independent layers, so a single misconfiguration can't leak it.
3. The only code that ever reads `eval/` is `admin.py`'s `/admin/score`
   endpoint — gated behind `X-Admin-Key`, and its JSON report is for a human
   reviewer only. It is never passed into any LLM context or tool response.

## Session memory & caching

Each session (one call) gets an in-memory `query_cache` keyed by the
*structured* intent the LLM extracted from the question (not the raw text —
so "how much did I sell in March" and "what were my March sales" hit the
same cache entry once both resolve to the same `QueryIntent`). A repeated
ask within the same session returns the cached `{result, trace}` instantly
without re-running the engine, and is still logged (`cache_hit: true`).

## Logging & pass/fail evaluation

Every single ask — cache hit or miss, successful or not — appends one
`QueryLogEntry` (`server/query_log.py`) with the question, resolved intent,
full trace, result, narration, latency, and an automatically-assigned
status:

- `answered` — the engine succeeded and the LLM's narration only stated
  numbers traceable back to the trace (`hallucination_check.py`).
- `possible_hallucination` — the narration contains a number that doesn't
  match anything in the trace within tolerance.
- `unfulfilled` — the tool-call arguments failed validation, or the engine
  raised (unsupported question / insufficient data).

Plus an admin manual confirm/dispute override
(`POST /admin/query-log/{log_id}/review`) for judgment-call questions
(mainly the `diagnostic` category) the automatic check can't fully resolve —
same review pattern as `kyc-voice-agent`'s session confirm/dispute.

`eval/score_report.py` pulls the admin API and prints a report; `/admin/score/{dataset}`
cross-references logged answers against that dataset's `ground_truth.json`
server-side, for datasets that have one.

## Voice output: Devanagari-only speech, cached greeting, early LLM hand-off

**Devanagari-only speech.** Sarvam's TTS mispronounces Latin-script words and
digit strings inside Hindi sentences, so everything the agent *speaks* is pure
Devanagari — English loanwords ("रेवेन्यू", "मार्जिन"), entity names, and
numbers spelled as Hindi words ("पाँच दशमलव पाँच करोड़ रुपये"). Tool arguments
(e.g. `entity_name`) stay in the data's own Latin spelling. Enforced in layers,
because a prompt alone drifts back to Roman script on long answers:

1. `prompts/system_prompt_hi.py` states the rule at the top *and* the very end
   of the prompt, and every spoken example in it is Devanagari.
2. `tools/ask_calculation_engine.py` emits each `<field>_inr_words` phrase
   already spelled in Devanagari (built by `hindi_numbers.py`), which the model
   reads verbatim instead of converting rupee amounts itself.
3. `speech_normalizer.py` (a pipecat processor between the LLM and TTS) runs
   `hindi_text.normalize_for_speech()` on the streamed text: digits, `₹`/`Rs`
   and `%` become number words, and a table of common business loanwords is
   rewritten to Devanagari. Latin words that survive (names, product codes) are
   logged per response, so drift is visible. The assistant context records the
   *spoken* text, so later turns see a Devanagari-only history too. The `/ask`
   text path applies the same normalization.
4. `hallucination_check.py` now also parses Devanagari number words back into
   values (`hindi_numbers.extract_spoken_numbers`), so narrated figures are
   still grounded against the engine trace — e.g. "पचपन लाख" for a real 5.5
   crore result is still flagged.

**Cached greeting.** The opening line is a constant
(`prompts/greeting_hi.py`, override with `GREETING_TEXT`) sent straight to TTS
as a `TTSSpeakFrame` — no LLM round trip before the caller hears anything. It
is appended to the context, so the model knows it already greeted.

**Early LLM hand-off.** `bot.py` lowers the turn-end wait
(`USER_SPEECH_TIMEOUT_SECS`, default `0.2`; pipecat's default is `0.6`) so each
transcribed speech segment goes to the LLM immediately. If the caller keeps
talking or changes the request mid-sentence, pipecat interrupts the in-flight
reply and the later segment is appended to the context; the prompt tells the
model to answer the *latest* intent. Limit: Sarvam's streaming STT returns only
final per-segment transcripts (no partials, and only after its VAD sees a
pause), so true character-by-character hand-off isn't possible without
switching STT provider.

**Adaptive turn-end wait** (`adaptive_turn.py`). How long to wait for the
caller to resume depends on what was just asked. A yes/no or one-word answer
→ barge in almost at once (`TURN_WAIT_SHORT_SECS`, default `0.05`); dictated
digits, emails or full names → wait much longer (`TURN_WAIT_LONG_SECS`,
default `2.0`); everything else → `USER_SPEECH_TIMEOUT_SECS` (`0.2`). The wait
applies to the caller's *next* turn. The expected answer length comes from:

1. a `<<ANS:SHORT|NORMAL|LONG>>` marker the model is prompted to put at the
   start of any reply that ends in a question (stripped before TTS and logging
   by `speech_normalizer.py`), else
2. `hindi_text.infer_expected_answer()`, a conservative read of the reply's
   closing question (dictation keywords → long; a `क्या …?` question with no
   other question word → short; anything unclear → normal). In a live check
   deepseek-chat emitted no marker at all, so this fallback does most of the
   work today.

Note this agent rarely asks for emails, card/Aadhaar/PAN numbers or names; the
machinery is generic and is mostly exercised by its yes/no follow-ups ("want
the full list?").

Tests (plain asserts, also pytest-compatible), from `server/`:
`python tests/test_hindi_numbers.py`, `tests/test_hindi_text.py`,
`tests/test_speech_normalizer.py`, `tests/test_adaptive_turn.py`.

## Telephony (Twilio)

The same agent answers phone calls over Twilio Media Streams. A call is
`phone -> Twilio -> POST /twilio/voice` (returns TwiML with a `<Stream>` to
`wss://.../twilio/ws`), then the call's 8 kHz mu-law audio flows over that
websocket into the existing STT -> LLM -> TTS pipeline (`bot.py` /
pipecat's `TwilioFrameSerializer`). Phone calls skip WebRTC and TURN entirely.

- `telephony.py` — TwiML, request/stream authentication, outbound call helper.
- `POST /twilio/voice` — public, but rejects anything without a valid
  `X-Twilio-Signature`; it mints a per-call token passed to the stream as a
  `<Parameter>`.
- `WS /twilio/ws` — refused (close 4401) unless the stream carries that call's
  token, so knowing the URL is not enough to start a billable session.
- `POST /twilio/call` `{"to": "+91..."}` (needs `X-Admin-Key`) — rings a number
  from `TWILIO_FROM_NUMBER`; only numbers in `TWILIO_ALLOWED_TO` are accepted.
- Env: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`,
  `TWILIO_ALLOWED_TO`, `TELEPHONY_SAMPLE_RATE` (`8000` default, or `16000` to feed
  STT upsampled audio — the phone line itself is always narrowband).
- The greeting is spoken when the stream connects (there is no RTVI "client
  ready" on a phone line); typing pings and reconnect/resume are web-only.

Step-by-step test instructions: DEPLOYMENT.md, "Phone calls (Twilio)".
Tests: `tests/test_telephony.py`.

## Setup

```bash
cd server
uv sync   # or: pip install -e .
cp .env.example .env   # fill in an LLM provider key + ADMIN_KEY

# Populate server/data/ (engine) + eval/ (admin-only) from a generated dataset:
cd ..
python scripts/sync_data.py \
  --source ../costguard/synthetic_business/output/baseline_seed42 \
  --name baseline_seed42
# then set DATA_DIR=data/baseline_seed42 in server/.env (already the default)

cd server
uvicorn server:app --port 8010 --reload
```

Try it:

```bash
curl -X POST http://localhost:8010/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "iss mahine kitni sale hui?"}'
```

Admin (needs `X-Admin-Key: <ADMIN_KEY>`):

```bash
curl http://localhost:8010/admin/query-log -H "X-Admin-Key: <key>"
curl http://localhost:8010/admin/score/baseline_seed42 -H "X-Admin-Key: <key>"
```

## Known limitations

- **`hallucination_check.py` is a heuristic, not a proof.** It parses numbers
  out of the narration (handling Indian lakh/crore phrasing, ignoring product
  SKU codes and dates) and checks each against the trace/result with a 5%
  tolerance for casual rounding. It was iterated against real model output
  during Phase 1 testing and fixed several real false-positive classes
  (scale mismatches between a trace step and the final result, product codes
  parsed as bare numbers, dates parsed as financial figures, negative
  percentage changes narrated as positive magnitudes) — but genuinely
  ambiguous phrasing like a spoken range ("21-22 lakh" for a sum the engine
  didn't compute a total for) can still slip through as a false positive.
  This is exactly what `admin_verdict` manual review exists for.
- **Enforcing "always call the tool" is real but not airtight.** If the LLM
  answers without ever calling `ask_calculation_engine` in an exchange, it's
  nudged and retried (`dev_llm_client.py`); if it still refuses after
  `MAX_TOOL_ROUNDS`, the response is discarded in favor of a fixed fallback
  line and logged as `answered_without_engine` — a hard, always-fail status.
  This also means a genuinely out-of-scope question (weather, jokes) can get
  forced through a failed tool-call attempt rather than being cleanly
  declined — acceptable for Phase 1's business-question test suite, worth
  revisiting (e.g. a dedicated "decline" tool) before a real deployment.
- Same in-memory, single-process caveats as `kyc-voice-agent`: `session_store.py`
  and `query_log.py` are cleared on restart — fine for evaluation, not for
  production.

## Project structure

```
biz-performance-voice-agent/
├── server/
│   ├── server.py                # FastAPI: POST /ask, /admin/*
│   ├── dev_llm_client.py        # Phase 1: raw-SDK chat+tool-call loop
│   ├── llm_factory.py           # Phase 2: Pipecat LLM service factory (unused by Phase 1)
│   ├── session_store.py         # in-memory SessionData incl. query_cache
│   ├── query_log.py             # QueryLogEntry + pass/fail status logic
│   ├── hallucination_check.py   # narration-vs-trace number grounding
│   ├── admin.py                 # X-Admin-Key auth + query-log + scoring
│   ├── prompts/system_prompt_hi.py
│   ├── tools/ask_calculation_engine.py   # the one LLM tool
│   ├── engine/
│   │   ├── data_loader.py       # allowlisted CSV/JSON loading only
│   │   ├── schema.py            # QueryIntent + period resolution
│   │   ├── trace.py             # StepTrace
│   │   ├── formulas.py          # shared, step-traced building blocks
│   │   ├── engine.py            # (metric_category, sub_metric) -> compute fn
│   │   └── compute/             # sales/profitability/cashflow/receivables/suppliers/inventory/customers/diagnostic
│   └── data/<dataset>/          # gitignored, populated by sync_data.py
├── scripts/sync_data.py
└── eval/<dataset>/              # gitignored, ground_truth.json + business_questions.json only
```
