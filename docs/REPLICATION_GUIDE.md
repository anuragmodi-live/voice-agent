# Munshi voice agent — architecture and replication guide

A Hindi/Hinglish voice assistant that answers business-performance questions
("How much did I sell this month?", "Which customers are overdue?") by running a
real-time calculation engine over raw transaction data, over **three channels**:
a browser (WebRTC), a **phone call** (SignalWire/Twilio Media Streams), and a plain
HTTP text endpoint. This guide lets you rebuild, deploy and operate the whole
stack from an empty account. Everything below was built and verified in this
repository; where something was *not* verified, it says so.

Contents: [1 Architecture](#1-architecture) · [2 Accounts and cost](#2-accounts-and-what-they-cost) ·
[3 Run locally](#3-run-it-locally) · [4 Deploy backend](#4-deploy-the-backend-render) ·
[5 Deploy web client](#5-deploy-the-web-client-vercel) · [6 Phone calls](#6-phone-calls-signalwire) ·
[7 Design decisions](#7-design-decisions-and-why) · [8 Config reference](#8-configuration-reference) ·
[9 Verify](#9-verification-checklist) · [10 Troubleshooting](#10-troubleshooting-real-incidents) ·
[11 Cost model](#11-cost-model-per-minute) · [12 Security](#12-security-model) ·
[13 Your own data](#13-adapting-it-to-your-own-business-data) · [14 Limitations](#14-known-limitations)

---

## 1. Architecture

```
 Browser (Vite client) ──WebRTC (SmallWebRTC, Cloudflare TURN)──┐
 Phone ──PSTN── SignalWire/Twilio number ──wss Media Streams────┤
                                                                ▼
                                  FastAPI + Pipecat pipeline  (one container on Render)
   audio in → Sarvam STT (saaras:v3, hi-IN) → user aggregator (Silero VAD + adaptive turn-end)
            → DeepSeek LLM (tool-calling) ⇄ ask_calculation_engine (pandas, deterministic)
            → HindiSpeechNormalizer (digits/loanwords → Devanagari) → Sarvam TTS (bulbul:v3 "ritu")
            → audio out
   side effects per turn: grounding check (numbers vs engine trace) → query_log (Postgres)
                          → judge LLM (async) → admin dashboard
   Supabase Postgres: query_log, voice_transcripts          Vercel: static client + /admin.html
```

**One turn, in order:** caller speaks → VAD detects a pause → Sarvam returns one
final transcript for the segment (it never sends partials) → the turn-end timer
(0.05–2.0 s, see §7.3) elapses → DeepSeek decides whether to call the single tool
`ask_calculation_engine` → the engine computes the number from raw CSV rows and
returns `{result, trace}` → the LLM narrates in Devanagari → the normalizer fixes
any digits/English words → TTS streams audio sentence by sentence (the first
sentence plays while later ones are still being generated).

### Repository map
| Path | Role |
|---|---|
| `server/server.py` | FastAPI app: `/health`, WebRTC (`/start-session`, `/ice-servers`, `/api/offer`), phone (`/twilio/voice`, `/twilio/ws`, `/twilio/call`), `/ask`, `/admin/*` |
| `server/bot.py` | Builds the Pipecat pipeline (web and phone transports), greeting, idle check-ins |
| `server/engine/` | The calculation engine: `data_loader.py` (hard allow-list of 9 files), `compute/*.py` (one module per metric category), `formulas.py`, `trace.py`, `schema.py` (the metric catalogue) |
| `server/tools/ask_calculation_engine.py` | The one LLM tool: schema, handler, session cache, `_inr_words` Devanagari amounts |
| `server/prompts/` | `system_prompt_hi.py` (persona + rules), `greeting_hi.py` (cached opening line) |
| `server/hindi_numbers.py`, `hindi_text.py`, `speech_normalizer.py` | Devanagari-only enforcement (number words both ways, loanwords, streaming processor) |
| `server/adaptive_turn.py` | Turn-end wait that adapts to the question asked |
| `server/telephony.py` | Twilio/SignalWire: TwiML, signature + stream-token auth, outbound call |
| `server/grounding.py`, `hallucination_check.py`, `judge.py` | Automatic number grounding + independent judge LLM |
| `server/query_log.py`, `transcripts.py`, `db.py` | Postgres persistence |
| `server/turn_credentials.py`, `aioice_turn_patch.py` | Cloudflare TURN credentials for WebRTC |
| `client/` | Vite web client (`index.html` voice UI, `admin.html` eval dashboard) |
| `scripts/sync_data.py`, `eval/` | Dataset split (engine-visible vs answer-key) and scoring |
| `render.yaml`, `server/Dockerfile` | Deployment |
| `server/tests/` | Offline tests (numbers, normalizer, turn logic, telephony) |

---

## 2. Accounts and what they cost

| Service | Used for | Needed for | Notes |
|---|---|---|---|
| GitHub | source; Render deploys from it | all | A **separate repo is fine**; the guide used a copy named `voice-agent` |
| Render | hosts the backend container | deploy | **Starter plan, Singapore region** (see §10 for why) ≈ $7/month |
| Supabase | Postgres for logs/transcripts | all | Free tier works but **pauses when idle** |
| Vercel | hosts the web client | web UI | Free tier |
| Sarvam AI | STT + TTS | voice | STT ₹30/hour of audio, TTS ₹30 per 10,000 characters (bulbul:v3) |
| DeepSeek (or Anthropic/OpenAI) | the LLM | all | Pick via `LLM_PROVIDER` |
| Cloudflare (Realtime TURN) | WebRTC relay | **web client from other networks only** | Not used by phone calls |
| SignalWire | phone number + media stream | phone | Account must be **funded (≥ $5) to leave trial**; see §6 |

Optional: a **CostGuard** gateway (this author's cost-tracking proxy) can sit in
front of DeepSeek via `DEEPSEEK_BASE_URL` — skip it and call DeepSeek directly.

---

## 3. Run it locally

Prerequisites: Python 3.12 (3.11+ works), Node 20+, a Postgres connection string.

```bash
git clone <your-repo> munshi && cd munshi/server
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt          # includes the Pipecat voice extras
cp .env.example .env                     # then edit (below)
```

Minimum `server/.env` to run the text endpoint:
`DATABASE_URL` (Supabase *transaction pooler* URI, port 6543 — the direct
connection is IPv6-only on most projects), `DEEPSEEK_API_KEY`, `ADMIN_KEY`
(any strong value). The two tables are created automatically on first start.
The demo dataset is **already committed** at `server/data/baseline_seed42/`
(≈30k sales rows); `DATA_DIR` defaults to it.

```bash
uvicorn server:app --port 8010 --reload
curl -X POST localhost:8010/ask -H "Content-Type: application/json" \
     -d '{"question": "iss mahine kitni sale hui?"}'
```

You should get a Devanagari `narration` with the revenue spelled in words and a
`log_ids` entry. For the **voice client**, add `SARVAM_API_KEY`, then:

```bash
cd ../client && npm install
cp env.example .env       # VITE_SERVER_URL=http://localhost:8010
npm run dev               # http://localhost:5173 -> click the connect button
```

On localhost the browser and server share a network, so TURN is not needed.
Admin dashboard: `http://localhost:5173/admin.html` (enter `ADMIN_KEY`).

**Tests** (offline, no keys needed) — run from `server/`:
`python tests/test_hindi_numbers.py`, `tests/test_hindi_text.py`,
`tests/test_speech_normalizer.py`, `tests/test_adaptive_turn.py`,
`tests/test_telephony.py` (each prints `ok <test>` lines).

---

## 4. Deploy the backend (Render)

1. Push the repo to GitHub. In `render.yaml`, check `name`, `region: singapore`
   and `plan: starter`. **The Blueprint page never asks for a region** — if the
   file has no `region:`, Render silently picks Oregon, and a service's region
   cannot be changed later.
2. Render → **New + → Blueprint** → pick the repo/branch → it reads `render.yaml`.
3. Fill the env vars Render prompts for (they are `sync: false`; secrets are never
   stored in the repo): `DATABASE_URL`, `DEEPSEEK_API_KEY`, `SARVAM_API_KEY`,
   `ADMIN_KEY`, `CF_TURN_TOKEN_ID`, `CF_TURN_API_TOKEN`, plus (if used)
   `DEEPSEEK_BASE_URL`, `DEEPSEEK_GATEWAY_PROVIDER_ID`, `ANTHROPIC_*`, and the
   telephony variables in §6. Non-secret values (`DATA_DIR`, `LLM_PROVIDER`,
   `JUDGE_AUTO_RUN`, `SARVAM_VOICE_ID`, `TELEPHONY_SAMPLE_RATE`) are set in the file.
   **A value written in `render.yaml` overrides the dashboard on every sync.**
4. Wait for the deploy, then check `https://<service>.onrender.com/health` →
   `{"status":"ok","data_dir":"data/baseline_seed42"}`.
5. Pushing to the branch auto-deploys (~1–2 min). Secret values are **not copied
   to a new service** — re-enter them if you create a second one.

If startup fails with `DATABASE_URL is not set` or `tenant/user ... not found`,
see §10 (empty env var / paused Supabase project).

---

## 5. Deploy the web client (Vercel)

1. Vercel → New Project → import the repo → **Root Directory = `client`**.
   (`client/vercel.json` already sets `npm run build` / `dist`.)
2. Settings → Environment Variables: `VITE_SERVER_URL` =
   `https://<service>.onrender.com` (no trailing slash; Production + Preview).
3. **Redeploy without the build cache.** Vite bakes the variable into the bundle at
   build time, so adding it without a rebuild changes nothing. If it is missing, the
   client falls back to `http://localhost:8010` and the connect button silently fails.
4. Verify: fetch the live JS bundle and confirm it contains your Render URL and no
   `localhost:8010`.

---

## 6. Phone calls (SignalWire)

**Why SignalWire, not Twilio:** a Twilio *trial* account cannot run this agent. The
console's "Try out Voice" dialog rewrites your TwiML and blocks `<Stream>`
("The Stream verb is not available on trial accounts"), and the trial Calls API
only accepts a destination plus Twilio's own sample instructions. A paid Twilio
account removes those limits (not tested here). SignalWire's cXML is
Twilio-compatible (`<Connect><Stream>`, same `connected/start/media/stop`
messages, REST under `/api/laml/2010-04-01`), so **the same code serves both**;
`TELEPHONY_PROVIDER` selects credentials.

**How a call flows:** caller dials the number → SignalWire `POST`s
`/twilio/voice` (signed) → we reply with `<Connect><Stream url="wss://…/twilio/ws">`
and a per-call token as a `<Parameter>` → SignalWire opens the websocket →
`bot()` verifies the token, builds the pipeline at 8 kHz µ-law → greeting plays.

Setup:
1. Create a SignalWire space. Buy a number (**Phone Numbers**). Add a card and
   fund **≥ $5** — a trial space only handles calls to/from verified numbers and
   blocks international calling.
2. **API Credentials** page: note the **Project ID**, create an **API token**, and
   click **Show** on the **Signing Key** (separate from the token).
3. Number settings → *Handle calls using → a cXML webhook* → *When a call comes
   in* = `POST https://<service>.onrender.com/twilio/voice`.
4. Render env: `TELEPHONY_PROVIDER=signalwire`, `SIGNALWIRE_SPACE=<space>.signalwire.com`,
   `SIGNALWIRE_PROJECT_ID`, `SIGNALWIRE_API_TOKEN`, `SIGNALWIRE_SIGNING_KEY`,
   `SIGNALWIRE_FROM_NUMBER` (E.164). Redeploy. **Leave `TWILIO_VALIDATE_SIGNATURE`
   unset** (see §12).
5. Dial the number. Healthy Render log sequence:
   `POST /twilio/voice … 200 OK` → `WebSocket /twilio/ws [accepted]` →
   `Detected transport: twilio` → STT/LLM/TTS TTFB lines, and no warnings.

Outbound test call (`POST /twilio/call`, needs `X-Admin-Key` and the destination in
`TELEPHONY_ALLOWED_TO`) additionally requires **International Dialing** enabled
on the SignalWire space for non-US numbers — a support request. Inbound calls do
not. Caller identity is not checked on inbound calls: anyone with the number can
start a (billable) session.

---

## 7. Design decisions and why

### 7.1 The engine, not the LLM, produces every number
The LLM has one tool, `ask_calculation_engine`. It turns a question into a
structured intent (`metric_category`, `sub_metric`, `period`, optional entity) from
a **fixed catalogue** (`engine/schema.py`: sales, profitability, cash_flow,
receivables, suppliers, inventory, customers, diagnostic; periods such as
`this_month`, `last_quarter`, `all_time`, `custom`). Deterministic pandas code
computes the answer from raw transactions and records a step-by-step **trace**.
`data_loader.py` allow-lists exactly nine files, so the answer key can never be
read by the engine or LLM. A per-session cache is keyed on the *structured intent*,
so rephrasings hit the cache.

### 7.2 Devanagari-only speech
Sarvam's TTS mispronounces Latin words and digit strings inside Hindi. Layers:
1. The system prompt states the script rule at the top **and** the end, with every
   spoken example in Devanagari (instructions stay in compact Roman Hinglish —
   Devanagari costs ~2× tokens per character and the prompt is re-sent each call).
2. The engine returns `<field>_inr_words` already spelled in Devanagari
   ("पाँच दशमलव पाँच करोड़ रुपये"); the model reads it verbatim instead of
   converting lakh/crore itself.
3. `HindiSpeechNormalizer` (between LLM and TTS) rewrites digits, `₹`, `%`, known
   English business terms and months to Devanagari on the stream, holding back at
   most one partial word. Latin words it cannot fix (names, product codes) are
   logged per response so drift is visible. The assistant context stores the
   *spoken* text, so history stays Devanagari-only.
4. `hallucination_check.py` parses Devanagari number words back into values
   (`hindi_numbers.extract_spoken_numbers`), so grounding still works — e.g. "पचपन
   लाख" for a real 5.5 crore result is flagged.

### 7.3 Turn-taking (the latency work)
- **Sarvam STT returns only final, per-segment transcripts** — no partials — so the
  LLM cannot start before a pause. The earliest possible dispatch is "segment
  transcribed".
- Pipecat's default stop strategy also waits an "STT p99 safety timer"
  (1.17 s − VAD stop 0.2 s ≈ **0.97 s**) after every pause because Sarvam
  transcripts are never marked finalized. `adaptive_turn.py` zeroes that timer
  (a turn can't end before a transcript exists anyway). Without this, the other
  wait settings have no effect.
- The remaining wait adapts to what the agent just asked: **0.05 s** for yes/no or
  option answers, **2.0 s** for dictated digits/emails/names, **0.2 s** otherwise.
  The model declares this with a `<<ANS:SHORT|NORMAL|LONG>>` marker (stripped
  before TTS); if it forgets — DeepSeek often does — `infer_expected_answer()`
  reads the closing question conservatively (ambiguous → normal).
- Smart-Turn (Pipecat's ONNX end-of-turn model) is deliberately **off**: on the
  old 0.15-CPU free plan it stuttered the audio. On 0.5 CPU it might be viable;
  untested.

### 7.4 Cached greeting
The opening line is a constant (`prompts/greeting_hi.py`) sent straight to TTS — no
LLM round trip before the caller hears anything. It is appended to the context so
the model knows it already greeted. Override with `GREETING_TEXT`.

### 7.5 Infrastructure choices that mattered
- **Region:** Singapore. Oregon added ~250 ms per hop (audio, Sarvam, LLM) for
  India callers. Measured in Singapore on a phone call: STT 0.31–0.41 s, LLM
  0.12–0.38 s, TTS 0.28–0.50 s time-to-first-output.
- **Plan:** Starter (0.5 CPU). The free plan's 0.15 CPU was pinned at 100 % during
  one web call → choppy audio and slow turns. One phone call peaked at 0.15 CPU
  (30 % of 0.5), ≈180 MB RAM.
- **WebRTC needs TURN** for browsers on other networks (Cloudflare Realtime TURN;
  credentials minted at startup). The phone path has no WebRTC/TURN at all.

---

## 8. Configuration reference

**Core**
| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | — (required) | Postgres URI; Supabase transaction pooler recommended |
| `DATA_DIR` | `data/baseline_seed42` | Dataset folder under `server/` |
| `ADMIN_KEY` | — | Required for `/admin/*` and `/twilio/call`; fails closed if unset |
| `LLM_PROVIDER` / `LLM_MODEL` | `deepseek` / provider default | `deepseek` \| `anthropic` \| `openai` (DeepSeek default model `deepseek-chat`) |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_GATEWAY_PROVIDER_ID` | — | Base URL/ID only for a gateway such as CostGuard |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `OPENAI_API_KEY` | — | Alternative providers |

**Judge** — `JUDGE_LLM_PROVIDER` (`deepseek`), `JUDGE_LLM_MODEL` (default
`deepseek-reasoner`), `JUDGE_DEEPSEEK_API_KEY`/`JUDGE_ANTHROPIC_API_KEY`,
`JUDGE_AUTO_RUN` = `off` | `flagged` (code default) | `all` (`render.yaml` uses
`all`; highest coverage and cost — one reasoning-model call per answered question).

**Voice** — `SARVAM_API_KEY`, `SARVAM_MODEL` (default `saaras:v3`), `SARVAM_VOICE_ID`
(`ritu`), `SARVAM_TTS_MODEL` (`bulbul:v3`), `SARVAM_TTS_PACE` (`0.85`),
`GREETING_TEXT`, `USER_IDLE_TIMEOUT_SECS` (`12`), `CF_TURN_TOKEN_ID`, `CF_TURN_API_TOKEN`.

**Turn-end** — `USER_SPEECH_TIMEOUT_SECS` (`0.2`, normal), `TURN_WAIT_SHORT_SECS`
(`0.05`), `TURN_WAIT_LONG_SECS` (`2.0`).

**Telephony** — `TELEPHONY_PROVIDER` (`twilio` default | `signalwire`),
`TELEPHONY_SAMPLE_RATE` (`8000`; `16000` upsamples the pipeline — no new detail,
the line is narrowband), `TELEPHONY_ALLOWED_TO` (falls back to `TWILIO_ALLOWED_TO`),
`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`/`TWILIO_FROM_NUMBER`,
`SIGNALWIRE_SPACE`/`PROJECT_ID`/`API_TOKEN`/`SIGNING_KEY`/`FROM_NUMBER`,
`TWILIO_WEBHOOK_BASE_URL` (only if the public URL is auto-detected wrongly),
`TWILIO_VALIDATE_SIGNATURE` (test-only; keep unset).

**Client** — `VITE_SERVER_URL` (build-time).

---

## 9. Verification checklist

| Step | Command / action | Expected |
|---|---|---|
| Health | `GET /health` | `200 {"status":"ok",...}` |
| Text path | `POST /ask {"question":"iss mahine kitni sale hui?"}` | Devanagari answer, amounts as words, `log_ids` non-empty |
| Admin | open `/admin.html`, enter `ADMIN_KEY` | entries with status `answered`; judge verdicts if enabled |
| Web call | connect from the deployed client | greeting within ~1 s, smooth audio; logs show `TTFB` lines |
| Phone call | dial the number | logs: `POST /twilio/voice 200` → `WebSocket /twilio/ws [accepted]`, no warnings |
| Guards | unsigned `POST /twilio/voice` | `403`; `POST /twilio/call` without key → `401` |
| Offline tests | the five test scripts in §3 | all `ok` |

Log lines worth grepping in Render: `TTFB` (per-stage latency), `usage characters` /
`usage audio seconds` / `prompt tokens` (billing inputs), `expecting a … answer`
(turn-end adaptation), `Latin-script word(s) reached TTS` (script drift).

---

## 10. Troubleshooting (real incidents)

| Symptom | Cause | Fix |
|---|---|---|
| Startup crash: `PoolTimeout … tenant/user … not found` | Supabase free project **paused** | Restore it in the Supabase dashboard, then redeploy |
| Startup crash: `DATABASE_URL is not set` | Secret not entered on a new Render service | Fill every `sync: false` var again |
| Voice choppy, replies slow | 0.15-CPU free plan pinned; Oregon region | `plan: starter`, `region: singapore` (new service) |
| "Connect" button does nothing on Vercel | `VITE_SERVER_URL` missing → client calls `localhost:8010` | Set it and **redeploy without cache** |
| Browser voice drops; logs show `TURN auth … 401/400/438` | Cloudflare TURN credentials/relay trouble | Re-check `CF_TURN_*`; re-test after fixing CPU |
| Every reply has ~1 s dead air | Pipecat's STT p99 safety timer | Already handled in `adaptive_turn.py` — don't remove it |
| Bot speaks English words/digits badly | Model drifted to Roman script | Check `Latin-script word(s)` log; extend the loanword table in `hindi_text.py` |
| `403` on `/twilio/voice`, log "invalid signature", `signature header present: False` | Request came from a Twilio console dialog (sends **unsigned** requests) | Use a real call, not the dialog |
| `403`, `signature header present: True` | Wrong secret, or URL typed differently (scheme/port) | SignalWire: use the **Signing Key**; check `SIGNALWIRE_SIGNING_KEY` |
| Twilio: call connects, hears "test message", never reaches the agent | Trial blocks `<Stream>` | Use SignalWire, or a paid Twilio account |
| Twilio API error "trial accounts have limited parameter access" | Trial restricts Calls API params | Same as above |
| SignalWire: calls refused / no international | Space still in **Trial** | Fund ≥ $5; outbound international also needs support to enable |
| Twilio/SignalWire request accepted but call ends | `TWILIO_VALIDATE_SIGNATURE=false` left set with no allow-list | Delete that variable |
| `render.yaml` change to `region` ignored on existing service | Region is fixed at creation | Rename the service; sync a new Blueprint |
| Telephony `503 … not set` | Provider env vars incomplete | Fill the provider's variables (§6) |

---

## 11. Cost model per minute

Measured on one real inbound call in Singapore (99 s, 5 caller turns, small talk, no
engine calls; Render usage logs). ₹ at an assumed ₹88/$.

| Step | Basis | Per minute |
|---|---|---|
| Phone line | SignalWire $0.0066 inbound + $0.003 stream per min | ₹0.85 |
| STT | ₹30/h × 71.5 s of audio billed | ₹0.36 |
| TTS | ₹30 / 10k chars × 856 chars (≈ 519 chars/min) | ₹1.56 |
| LLM | 35.7k prompt tokens (97.9 % cache hits), 476 output tokens | ₹0.07–0.17 |
| **Variable total** | | **≈ ₹2.8–2.9** |
| Fixed | Render Starter ≈ $7/month; number $0.50/month | — |

TTS dominates (~54 %). Real business questions cost more: each adds a second LLM
call, a longer spoken answer (earlier web calls spoke ~780 chars/min ⇒ TTS ≈ ₹2.3/min)
and a judge run (~₹0.3–1.2, estimated, unmeasured) — plan ≈ ₹3.7–4.5/min.
DeepSeek rates were not confirmed from an official page (third-party sources gave
two different price sets), and DeepSeek's `deepseek-chat`/`deepseek-reasoner`
model names were reported to be retiring — verify before relying on either. Not
included: taxes, SignalWire per-minute rounding, the caller's own ISD charges.

---

## 12. Security model
- **`/twilio/voice`** is public, so every request must carry a valid signature
  (HMAC-SHA1 over URL + sorted form params; SignalWire's Signing Key / Twilio's
  auth token). URL scheme/port variants are tried because the provider signs the URL
  exactly as typed in its console.
- **`/twilio/ws`** cannot carry a query string, so the signed webhook mints a per-call
  token (HMAC of the call SID) passed as a `<Parameter>`; `bot()` closes the socket
  (code 4401) without it.
- **`/twilio/call`** needs the admin key **and** a destination in
  `TELEPHONY_ALLOWED_TO`.
- `TWILIO_VALIDATE_SIGNATURE=false` exists only for the Twilio console dialog; even
  then an unsigned request must carry our AccountSid and an allow-listed number.
  Keep it unset in any real deployment.
- Secrets live only in Render's dashboard; `render.yaml` marks them `sync: false`.
  The repo may be public — keep phone numbers and keys out of it.
- Open risk: inbound callers are not restricted, so anyone with the number can
  start a billable session. Add a caller allow-list if that matters.

---

## 13. Adapting it to your own business data
1. Produce the nine files the engine reads (it refuses any other name):
   `sales_transactions.csv`, `purchase_transactions.csv`, `bank_transactions.csv`,
   `inventory_transactions.csv`, `products.csv`, `suppliers.csv`, `customers.csv`,
   `business_events.csv`, `business.json`. Match the column names in
   `server/data/baseline_seed42/`.
2. Put them in `server/data/<name>/` and set `DATA_DIR=data/<name>`.
   (`scripts/sync_data.py` splits a generator output into `server/data/<name>/` and
   the answer-key files that stay in `eval/<name>/`, outside the engine's reach.)
3. To add a metric: add its name to `METRIC_CATEGORIES` in `engine/schema.py`,
   implement it in the matching `engine/compute/*.py` (return `(result, trace)`),
   and describe it in the tool description in `tools/ask_calculation_engine.py` so
   the LLM can select it.
4. Change persona/tone/rules in `prompts/system_prompt_hi.py`; keep the Devanagari
   script block at the top and the reminder at the end.

---

## 14. Known limitations
- No word-by-word recognition: Sarvam STT gives final transcripts per segment only.
- DeepSeek does not reliably emit the `<<ANS:…>>` marker; the keyword fallback does
  most of the work, and misses yes/no questions that also contain a question word.
- Latin-script names the model fails to transliterate are only *logged*, not fixed.
- Phone audio is narrowband (8 kHz); Hindi recognition is likely worse than on the web
  client. Not benchmarked.
- Concurrency is untested: one 0.5-CPU instance, roughly 3 simultaneous calls by a
  straight-line CPU extrapolation.
- Supabase free projects pause when idle; sessions are in memory and lost on restart.
- The SignalWire path was verified by a local simulated handshake and one real
  inbound call; outbound `/twilio/call` on SignalWire was not exercised.
