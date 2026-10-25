import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gateway.app import create_app
from gateway.models import ApprovalStatus
from gateway.relay import MODEL_UNAVAILABLE, PIN_LOCKED, PIN_PROMPT, PIN_RETRY
from gateway.security import sign_call_token
from gateway.voice_agent import AgentOutcome, VoiceModelError

from .conftest import FakeAgent, delete_customers_request, twilio_signature

RELAY_URL = "wss://gw.test/twilio/relay"
OPENING = ["Your agent wants to delete 200 customer records. ", "Do you approve or deny?"]


@pytest.fixture
def make_client(settings, store, telephony):
    def make(agent):
        app = create_app(
            settings=settings,
            store=store,
            telephony=telephony,
            agent_factory=lambda a: agent,
            run_sweeper=False,
        )
        return TestClient(app)

    return make


def calling_approval(store, *, pin=None):
    approver = store.add_approver(pin=pin)
    approval = asyncio.run(
        store.create_approval(
            delete_customers_request(approver.id), datetime.now(UTC) + timedelta(minutes=5)
        )
    )
    asyncio.run(store.mark_calling(approval.id, "CA123"))
    return approval


def connect(client):
    return client.websocket_connect(
        "/twilio/relay", headers={"X-Twilio-Signature": twilio_signature(RELAY_URL, {})}
    )


def setup_message(settings, approval_id):
    token = sign_call_token(settings.call_token_secret.get_secret_value(), approval_id, 60)
    return {"type": "setup", "callSid": "CA123", "customParameters": {"token": token}}


def receive_turn(ws):
    """Collect streamed tokens up to the end-of-turn marker."""
    tokens = []
    while True:
        message = ws.receive_json()
        assert message["type"] == "text"
        if message["last"]:
            return "".join(tokens) + message["token"]
        tokens.append(message["token"])


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met")
        time.sleep(0.01)


def test_conversation_to_denial(settings, store, make_client):
    approval = calling_approval(store)
    agent = FakeAgent(
        [
            (OPENING, None),
            (["The agent says they are duplicates, but that was not verified."], None),
            (["Okay."], AgentOutcome("deny", "no proof of duplicates")),
        ]
    )
    with make_client(agent) as client, connect(client) as ws:
        ws.send_json(setup_message(settings, approval.id))
        assert receive_turn(ws) == "".join(OPENING)

        ws.send_json(
            {"type": "prompt", "voicePrompt": "Why is it deleting those records?", "last": True}
        )
        assert "not verified" in receive_turn(ws)

        ws.send_json({"type": "prompt", "voicePrompt": "Deny it.", "last": True})
        assert receive_turn(ws) == "Okay."
        assert ws.receive_json() == {"type": "end"}

    resolved = store.approvals[approval.id]
    assert resolved.status == ApprovalStatus.DENIED
    assert resolved.decision_reason == "no proof of duplicates"
    assert agent.user_messages == ["Why is it deleting those records?", "Deny it."]
    assert "approver" in store.kinds() and "assistant" in store.kinds()


def test_voice_model_failure_ends_the_call_and_fails_closed(settings, store, make_client):
    approval = calling_approval(store)
    agent = FakeAgent([([], VoiceModelError("429 rate limited"))])
    with make_client(agent) as client, connect(client) as ws:
        ws.send_json(setup_message(settings, approval.id))
        assert receive_turn(ws) == MODEL_UNAVAILABLE
        assert ws.receive_json() == {"type": "end"}

    resolved = store.approvals[approval.id]
    assert resolved.status == ApprovalStatus.FAILED
    assert resolved.decision_reason == "voice model unavailable"


def test_pin_required_before_any_details(settings, store, make_client):
    approval = calling_approval(store, pin="4821")
    agent = FakeAgent([(OPENING, None)])
    with make_client(agent) as client, connect(client) as ws:
        ws.send_json(setup_message(settings, approval.id))
        assert receive_turn(ws) == PIN_PROMPT

        ws.send_json({"type": "prompt", "voicePrompt": "What is this about?", "last": True})
        assert receive_turn(ws) == PIN_PROMPT
        assert agent.turns == 0 and agent.user_messages == []

        for digit in "1111#":
            ws.send_json({"type": "dtmf", "digit": digit})
        assert receive_turn(ws) == PIN_RETRY

        for digit in "4821#":
            ws.send_json({"type": "dtmf", "digit": digit})
        assert receive_turn(ws) == "".join(OPENING)

    pin_events = [content for _, kind, content in store.events if kind == "pin"]
    assert pin_events == [{"result": "incorrect", "attempt": 1}, {"result": "verified"}]
    assert "4821" not in str(store.events)


def test_pin_lockout_fails_closed(settings, store, make_client):
    approval = calling_approval(store, pin="4821")
    with make_client(FakeAgent([])) as client, connect(client) as ws:
        ws.send_json(setup_message(settings, approval.id))
        receive_turn(ws)
        for attempt in range(settings.pin_max_attempts):
            for digit in "0000#":
                ws.send_json({"type": "dtmf", "digit": digit})
            expected = PIN_LOCKED if attempt == settings.pin_max_attempts - 1 else PIN_RETRY
            assert receive_turn(ws) == expected
        assert ws.receive_json() == {"type": "end"}

    assert store.approvals[approval.id].status == ApprovalStatus.FAILED


def test_hanging_up_without_a_decision_fails_closed(settings, store, make_client):
    approval = calling_approval(store)
    with make_client(FakeAgent([(OPENING, None)])) as client:
        with connect(client) as ws:
            ws.send_json(setup_message(settings, approval.id))
            receive_turn(ws)
        wait_for(lambda: store.approvals[approval.id].status == ApprovalStatus.FAILED)


def test_interrupt_tells_agent_what_was_heard(settings, store, make_client):
    approval = calling_approval(store)
    agent = FakeAgent([(OPENING, None), (["Sure."], None)])
    with make_client(agent) as client, connect(client) as ws:
        ws.send_json(setup_message(settings, approval.id))
        receive_turn(ws)
        ws.send_json({"type": "interrupt", "utteranceUntilInterrupt": "Your agent wants to"})
        ws.send_json({"type": "prompt", "voicePrompt": "Wait, which database?", "last": True})
        receive_turn(ws)

    assert (
        agent.call_events[-1]
        == 'The approver interrupted you. They heard only: "Your agent wants to"'
    )


@pytest.mark.parametrize("token", ["forged", None])
def test_invalid_call_token_is_rejected(settings, store, make_client, token):
    approval = calling_approval(store)
    agent = FakeAgent([])
    with make_client(agent) as client, connect(client) as ws:
        setup = setup_message(settings, approval.id)
        if token is None:
            setup["customParameters"] = {}
        else:
            setup["customParameters"]["token"] = token
        ws.send_json(setup)
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 1008
    assert agent.turns == 0
    assert store.approvals[approval.id].status == ApprovalStatus.CALLING


def test_unsigned_websocket_is_rejected(settings, store, make_client):
    with (
        make_client(FakeAgent([])) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/twilio/relay") as ws,
    ):
        ws.receive_json()
