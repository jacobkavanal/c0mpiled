# Handoff: phone-call approval feature

Context for an LLM (or person) picking this work up later. Read this first, then `README.md`.

## Goal

A centralized MCP gateway (runs on our servers, not the customer's) intercepts agent tool calls,
classifies risk, and either allows, blocks, or **phones a human for approval**. This branch
(`phone-call`) builds the phone-approval piece: Twilio calls the approver, a realtime-style voice
model explains the action and answers questions, and the decision is stored. The MCP proxy,
risk classification and dashboard are **not built yet**; only the approval-call service exists.

Key product requirement: the voice model must **separate verified facts (from the gateway) from
claims made by the agent**, e.g. "The agent says these are duplicates, but nothing in the request
verifies that." Example: agent wants to delete 200 customer rows; approver asks "why?"; model
answers with the agent's stated objective, labelled as unverified.

## State (as of 2026-10-24)

- Branch `phone-call`, from `main` (one initial commit). **Nothing is committed yet.**
- Python 3.11, `uv`, FastAPI, asyncpg, Twilio, `anthropic` SDK, `openai` SDK.
- 52 tests passed at last full run, **before the Groq provider was added**. Tests, lint
  (`uvx ruff check src tests`) and README/`.env.example` have not been updated or re-run since.
- Never exercised against real Twilio or a real Supabase database. Only the voice model
  conversation has been tried live (terminal simulator).

## Architecture

Voice: **Twilio ConversationRelay**. Twilio does speech-to-text and text-to-speech and streams
text over a WebSocket (`/twilio/relay`); we do not handle raw audio. (Considered and rejected for
now: OpenAI Realtime over Twilio Media Streams: lower latency but more code and a second vendor.)

Call flow:
1. Caller (the MCP proxy) uses `ApprovalService.request_approval()` or `POST /v1/approvals`.
2. `TwilioTelephony.place_call` dials the approver with answering-machine detection; voicemail
   or no answer resolves the request as `failed` and says nothing about the request.
3. Twilio hits `/twilio/voice/{id}`; we return `<Connect><ConversationRelay>` with a signed
   per-call token as a custom parameter.
4. If the approver has a PIN, nothing about the request is spoken until it is entered on the
   keypad (max 3 attempts, then `failed`).
5. A voice agent streams a reply per approver turn and ends the call with a tool call:
   `record_decision(approve|deny, reason)` or `end_call_without_decision(reason)`. Approval needs
   a spoken read-back and clear confirmation (enforced by prompt only).
6. Decision is stored with a conditional update (first decision wins); a Postgres trigger sends
   `NOTIFY approval_resolved` to wake waiting gateway instances.
7. Closing line is spoken from the **stored status**, not model output.

**Fails closed:** only status `approved` lets the proxy forward the call. `denied`, `expired`,
`failed` (no answer, voicemail, hang-up, PIN lockout, model/Twilio errors) all block.

## Files (`src/gateway/`)

| File | Role |
|---|---|
| `config.py` | `VoiceSettings` (provider/model/effort/keys) and `Settings` (everything else) |
| `models.py` | Pydantic models; `VerifiedFacts` vs `AgentClaims` is the core split |
| `prompts.py` | System prompt and `render_approval_context` (escapes agent-controlled text) |
| `voice_agent.py` | Provider-neutral: `VoiceAgent` protocol, tool defs, `parse_tool_calls`, `AgentOutcome`, `VoiceModelError` |
| `claude_agent.py` | Anthropic implementation (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`) |
| `openai_compat_agent.py` | Gemini and Groq via OpenAI-style Chat Completions (streamed tool calls, echoes back Gemini `extra_content` thought signatures) |
| `voice_providers.py` | Picks client and agent by `VOICE_PROVIDER`; `missing_credentials` messages |
| `relay.py` | WebSocket session: setup/token check, PIN, prompts, interrupts, outcome recording |
| `telephony.py` | Twilio calls and TwiML |
| `approvals.py` | `ApprovalService`: start, wait, expire overdue (sweeper every 15 s) |
| `store.py` | `ApprovalStore` protocol, `PostgresStore` (asyncpg, LISTEN/NOTIFY) |
| `app.py` | FastAPI app, `/v1/approvals` (bearer auth), `/twilio/*` webhooks (signature-checked) |
| `security.py` | HMAC call tokens, scrypt PIN hashes, Twilio signature check |
| `simulate.py` | Terminal simulator: you play the approver against the real model |

Database: `supabase/migrations/20261024000000_approval_calls.sql` (approvers, approval_requests,
call_events, trigger, RLS enabled with no policies; the gateway connects as owner).
DB must be a **session-mode** connection; the transaction pooler breaks LISTEN/NOTIFY.

## Voice model: what was measured

Providers: `gemini` (OpenAI-compat endpoint), `groq`, `anthropic`. Set in `.env`.

| Model | First words | Notes |
|---|---|---|
| `gemini-3.8-flash`, effort low | ~43 s | Unusable. Rejects `minimal`. |
| `gemini-3.5-flash-lite`, minimal | 2.7-3.7 s | Behaved correctly; coded default for `gemini` |
| Groq `openai/gpt-oss-20b`, low | ~0.5 s | **Called deny unprompted** after a plain question. Do not use. |
| Groq `openai/gpt-oss-120b`, low | 0.2-0.4 s | Correct on one 4-turn run. **Current `.env` choice.** |
| Anthropic `claude-sonnet-5-5`, low | not measured | Needs prepaid API credits; Claude Pro does not include API access |

- The Groq key available here does **not** have `llama-3.3-70b-versatile`; it lists
  `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b` among chat models.
- Only ONE 4-turn conversation was run per model. Approve path and read-back never tested live.
- Free tiers (Gemini confirmed on its pricing page; Groq **not checked**) may use data for
  training. **Test data only** until verified or moved to a paid/Claude provider.
- `reasoning_effort`: Gemini accepts minimal/low/medium/high (varies by model); Claude uses
  low..max. Defaults are per provider in `config.py` (`DEFAULT_VOICE_EFFORTS`).

## Environment notes

- Project lives in OneDrive. That causes: slow imports (~7 s), uv hardlink errors (use
  `$env:UV_LINK_MODE = "copy"`), and `.venv` file locks on `uv sync`. Moving it out of OneDrive
  (e.g. `C:\dev\c0mpiled`) is recommended.
- `uv run --no-sync` avoids the reinstall step when the editable install is already present.
- `.env` (git-ignored) holds API keys; `.env.example` is the committed template and must never
  contain real keys. An Anthropic key was once pasted into chat and into `.env.example`
  (never committed); the user was told to revoke it.
- Windows PowerShell 5.1: no `&&`; inline Python with quotes breaks, use script files.

## Known gaps / next steps

1. Re-run `uv run pytest` and `uvx ruff check src tests`; fix tests broken by the Groq change;
   add Groq tests (write tests first; the user prefers that order when asked).
2. Update `README.md` and `.env.example` for Groq (they mention only gemini/anthropic).
3. **Real call test** (nothing done yet): Twilio account + number (trial can only call verified
   numbers), Supabase project + migration, ngrok, full `.env` (`DATABASE_URL`,
   `PUBLIC_BASE_URL`, `TWILIO_*`, `INTERNAL_API_KEY`, `CALL_TOKEN_SECRET`), an `approvers` row.
   Steps are in README "Setup".
4. Unverified assumption: Twilio signs the ConversationRelay WebSocket upgrade like its HTTP
   webhooks. If real calls fail to connect, try `TWILIO_VALIDATE_WEBSOCKET_SIGNATURE=false`
   (the signed per-call token still authenticates the session).
5. Latency on a real call: consider a filler phrase while the model starts, and test the
   welcome-greeting overlap. Replies of 1-2 sentences only.
6. Safety evaluation before real data: run the same tricky calls (prompt injection in the
   request, pressure to approve, ambiguous "yes", unverified claims) against each model.
   Approval confirmation relies on the prompt; consider a code-level gate (e.g. require a
   confirmation turn before `record_decision(approve)` is accepted).
7. Not built: the MCP proxy, risk classification, dashboard, tenant RLS policies, private-MCP
   connector, retry/escalation to a second approver.

## Conventions the user has shown

- Prefers tests written first and run to confirm failure when changing behavior (except when
  they explicitly ask for minimal or no testing because of time pressure).
- Wants short, direct answers. Do not implement when they say "don't implement".
- Do not commit unless asked. Commit messages end with the attribution line the harness gives.
