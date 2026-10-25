# c0mpiled

An MCP gateway that holds risky tool calls until a human approves them on the phone.
This branch adds the approval call: Twilio phones the approver, an AI voice agent
explains the action and answers questions, and the decision is recorded in Supabase.

## How a call works

1. The MCP proxy flags a tool call and calls `ApprovalService.request_approval()` (or
   `POST /v1/approvals`), passing what it verified and what the agent claims.
2. Twilio dials the approver. Answering-machine detection hangs up on voicemail, so no
   details are left in a recording.
3. Twilio fetches `/twilio/voice/{id}` and gets back `<Connect><ConversationRelay>`. Twilio
   handles speech-to-text and text-to-speech and connects to the `/twilio/relay` WebSocket.
4. If the approver has a PIN, they must enter it on the keypad before anything is said
   about the request.
5. The voice model (see [Voice model](#voice-model)) explains the action, answers
   questions, and ends the conversation with a `record_decision` or
   `end_call_without_decision` tool call. Approvals require a spoken read-back and
   confirmation.
6. The decision is written to Postgres with a conditional update (the first decision
   wins), and a trigger sends `NOTIFY approval_resolved`. That wakes whichever gateway
   instance is holding the request.
7. The closing line ("The request has been denied...") is spoken from the stored status,
   not from model output.

## Facts versus claims

Every request has two parts, and the voice prompt keeps them separate:

- `facts`: what the gateway established itself, such as the tool, arguments, target
  server, why it was flagged, and checks it ran (for example "Dry run matched 200 rows").
- `claims`: what the agent says about its own request, such as its stated objective and
  justification. The voice model always attributes these ("the agent says...") and
  points out when an answer rests only on a claim.

All of it is treated as untrusted data. Agent-supplied text is escaped so it can't break
out of its section, and the prompt tells the model to warn the approver if the request
contains instructions aimed at the reviewer.

## Voice model

Set `VOICE_PROVIDER` in `.env`:

| Provider | Default model | Key | Notes |
|---|---|---|---|
| `gemini` (default) | `gemini-3.5-flash-lite` | `GEMINI_API_KEY`, free from [AI Studio](https://aistudio.google.com/apikey) | Free tier lets Google use requests to improve its products: **test data only** |
| `anthropic` | `claude-sonnet-5-5` | `ANTHROPIC_API_KEY` (prepaid credits) | |

`VOICE_MODEL` overrides the model and `VOICE_EFFORT` sets how much it thinks before
replying (`minimal` for Gemini, `low` for Claude by default). Speed matters on a phone
call: in testing `gemini-3.5-flash-lite` at `minimal` started replying in 0.7 s, while
`gemini-3.8-flash` took 43 s, so check the first-reply time before changing either.

The rest of the gateway doesn't depend on the provider. Each one is a small adapter
([claude_agent.py](src/gateway/claude_agent.py) and
[openai_compat_agent.py](src/gateway/openai_compat_agent.py)) behind the same
interface. Before real customer data goes through it, compare the providers on the same
set of difficult practice calls.

## Fails closed

Only status `approved` lets the proxy forward the call. Every other outcome leaves the
action blocked and is recorded with a reason: `denied`, `expired` (deadline passed), and
`failed` (no answer, voicemail, hang-up, PIN lockout, model or Twilio errors).

## Setup

```sh
uv sync
cp .env.example .env            # fill in the values
supabase db push                # applies supabase/migrations
uv run uvicorn gateway.app:create_app --factory --port 8000
```

For local testing, expose port 8000 with `ngrok http 8000` and set `PUBLIC_BASE_URL` to
the ngrok URL. Add an approver row (PIN optional):

```sh
uv run python -c "from gateway.security import hash_pin; print(hash_pin('4821'))"
```

```sql
insert into approvers (tenant_id, name, phone_e164, pin_hash)
values ('acme', 'Dana', '+15551234567', '<hash from above>');
```

Then start an approval:

```sh
curl -X POST localhost:8000/v1/approvals \
  -H "Authorization: Bearer $INTERNAL_API_KEY" -H "Content-Type: application/json" \
  -d '{"tenant_id": "acme", "approver_id": "<approver id>", "risk_level": "critical",
       "summary": "Delete 200 rows from the production customers table",
       "facts": {"tool_name": "execute_sql", "mcp_server": "prod-postgres",
                 "arguments": {"sql": "DELETE FROM customers WHERE ..."},
                 "risk_reasons": ["DELETE on a production database"],
                 "observations": ["Dry run matched 200 rows in customers"]},
       "claims": {"stated_objective": "Remove duplicate customer entries"}}'

curl "localhost:8000/v1/approvals/<id>?wait=60" -H "Authorization: Bearer $INTERNAL_API_KEY"
```

## Tests

```sh
uv run pytest
```

Tests use in-memory fakes for Postgres, Twilio and both model providers. `PostgresStore`
and the live Twilio flow still need a test against a real Supabase project and phone
number.

To try the conversation itself against the real model, without Twilio or a database,
play the approver in the terminal. It needs only the voice model's key in `.env`
(`GEMINI_API_KEY` by default):

```sh
uv run python -m gateway.simulate
```

Try asking "Why is it deleting those records?". It should say the agent claims the
records are duplicates and that this wasn't verified.
