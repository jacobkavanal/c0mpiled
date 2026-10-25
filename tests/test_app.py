import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.approvals import ApprovalService
from gateway.models import ApprovalStatus

from .conftest import FakeTelephony, delete_customers_request, twilio_signature

AUTH = {"Authorization": "Bearer internal-key"}


@pytest.fixture
def client(settings, store, telephony):
    app = create_app(
        settings=settings,
        store=store,
        telephony=telephony,
        agent_factory=lambda a: None,
        run_sweeper=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def calling_approval(store, *, pin=None):
    approver = store.add_approver(pin=pin)
    approval = asyncio.run(
        store.create_approval(
            delete_customers_request(approver.id), datetime.now(UTC) + timedelta(minutes=5)
        )
    )
    asyncio.run(store.mark_calling(approval.id, "CA123"))
    return approval


def post_twilio(client, path, params):
    signature = twilio_signature(f"https://gw.test{path}", params)
    return client.post(path, data=params, headers={"X-Twilio-Signature": signature})


def test_create_approval_requires_internal_key(client, store):
    approver = store.add_approver()
    body = delete_customers_request(approver.id).model_dump(mode="json")
    assert client.post("/v1/approvals", json=body).status_code == 401
    assert (
        client.post(
            "/v1/approvals", json=body, headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )


def test_create_approval_places_call(client, store, telephony):
    approver = store.add_approver()
    response = client.post(
        "/v1/approvals",
        json=delete_customers_request(approver.id).model_dump(mode="json"),
        headers=AUTH,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "calling"
    assert telephony.calls == [(store.approvals[next(iter(store.approvals))].id, "+15551234567")]
    assert client.get(f"/v1/approvals/{body['id']}", headers=AUTH).json()["status"] == "calling"


def test_create_approval_rejects_approver_from_other_tenant(client, store):
    approver = store.add_approver()
    req = delete_customers_request(approver.id).model_copy(update={"tenant_id": "other-tenant"})
    assert (
        client.post("/v1/approvals", json=req.model_dump(mode="json"), headers=AUTH).status_code
        == 404
    )


def test_voice_webhook_rejects_bad_signature(client, store):
    approval = calling_approval(store)
    response = client.post(
        f"/twilio/voice/{approval.id}",
        data={"CallSid": "CA123"},
        headers={"X-Twilio-Signature": "nope"},
    )
    assert response.status_code == 403


def test_voice_webhook_connects_conversation_relay(client, store):
    approval = calling_approval(store, pin="4821")
    response = post_twilio(
        client, f"/twilio/voice/{approval.id}", {"CallSid": "CA123", "AnsweredBy": "human"}
    )

    assert response.status_code == 200
    twiml = response.text
    assert "<ConversationRelay" in twiml
    assert 'url="wss://gw.test/twilio/relay"' in twiml
    assert 'dtmfDetection="true"' in twiml
    assert '<Parameter name="token" value="' in twiml
    assert f'action="https://gw.test/twilio/relay-ended/{approval.id}"' in twiml
    # No request details are put into TwiML, so nothing leaks before the PIN.
    assert "customers" not in twiml


def test_voicemail_hangs_up_and_fails_closed(client, store):
    approval = calling_approval(store)
    response = post_twilio(
        client,
        f"/twilio/voice/{approval.id}",
        {"CallSid": "CA123", "AnsweredBy": "machine_end_beep"},
    )

    assert "<Hangup />" in response.text
    assert "ConversationRelay" not in response.text
    assert store.approvals[approval.id].status == ApprovalStatus.FAILED


def test_unanswered_call_fails_closed(client, store):
    approval = calling_approval(store)
    response = post_twilio(
        client, f"/twilio/status/{approval.id}", {"CallSid": "CA123", "CallStatus": "no-answer"}
    )

    assert response.status_code == 204
    assert store.approvals[approval.id].status == ApprovalStatus.FAILED
    assert store.approvals[approval.id].decision_reason == "call no-answer"


def test_closing_line_comes_from_stored_status(client, store):
    approval = calling_approval(store)
    asyncio.run(store.resolve(approval.id, ApprovalStatus.APPROVED, None))
    response = post_twilio(client, f"/twilio/relay-ended/{approval.id}", {"CallSid": "CA123"})
    assert "Your approval has been recorded" in response.text


async def test_failed_call_placement_fails_closed(settings, store):
    approver = store.add_approver()
    service = ApprovalService(store, FakeTelephony(fail=True), settings)
    approval = await service.request_approval(delete_customers_request(approver.id))
    assert approval.status == ApprovalStatus.FAILED
    assert not approval.is_approved


async def test_request_approval_waits_for_decision(settings, store, telephony):
    approver = store.add_approver()
    service = ApprovalService(store, telephony, settings)

    async def approve_soon():
        await asyncio.sleep(0.05)
        approval_id = next(iter(store.approvals))
        await store.resolve(approval_id, ApprovalStatus.APPROVED, None)

    task = asyncio.create_task(approve_soon())
    approval = await service.request_approval(delete_customers_request(approver.id))
    await task
    assert approval.is_approved


async def test_overdue_requests_expire_and_hang_up(settings, store, telephony):
    approver = store.add_approver()
    service = ApprovalService(store, telephony, settings)
    approval = await service.start(delete_customers_request(approver.id))
    store.approvals[approval.id] = store.approvals[approval.id].model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )

    assert await service.expire_overdue() == 1
    assert store.approvals[approval.id].status == ApprovalStatus.EXPIRED
    assert telephony.hung_up == [approval.call_sid or store.approvals[approval.id].call_sid]
