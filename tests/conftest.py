import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from twilio.request_validator import RequestValidator

from gateway.config import Settings
from gateway.models import (
    AgentClaims,
    ApprovalRequest,
    ApprovalStatus,
    Approver,
    NewApprovalRequest,
    RiskLevel,
    VerifiedFacts,
)
from gateway.security import hash_pin
from gateway.voice_agent import AgentOutcome

AUTH_TOKEN = "twilio-test-token"
TENANT = "acme"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql://unused",
        public_base_url="https://gw.test",
        internal_api_key="internal-key",
        call_token_secret="call-token-secret",
        twilio_account_sid="AC00000000000000000000000000000000",
        twilio_auth_token=AUTH_TOKEN,
        twilio_from_number="+15550000000",
    )


def twilio_signature(url: str, params: dict[str, str]) -> str:
    return RequestValidator(AUTH_TOKEN).compute_signature(url, params)


def delete_customers_request(approver_id: UUID) -> NewApprovalRequest:
    return NewApprovalRequest(
        tenant_id=TENANT,
        approver_id=approver_id,
        risk_level=RiskLevel.CRITICAL,
        summary="Delete 200 rows from the production customers table",
        facts=VerifiedFacts(
            tool_name="execute_sql",
            mcp_server="prod-postgres",
            arguments={
                "sql": "DELETE FROM customers WHERE id IN (...)",
                "row_ids": list(range(200)),
            },
            risk_reasons=[
                "DELETE on a production database",
                "Not reversible without a backup restore",
            ],
            observations=["Dry run matched 200 rows in customers"],
        ),
        claims=AgentClaims(
            agent_name="billing-cleanup-agent",
            stated_objective="Remove duplicate customer entries",
        ),
    )


class InMemoryStore:
    def __init__(self) -> None:
        self.approvers: dict[UUID, Approver] = {}
        self.approvals: dict[UUID, ApprovalRequest] = {}
        self.events: list[tuple[UUID, str, dict[str, Any]]] = []

    def add_approver(self, *, pin: str | None = None) -> Approver:
        approver = Approver(
            id=uuid4(),
            tenant_id=TENANT,
            name="Dana",
            phone_e164="+15551234567",
            pin_hash=hash_pin(pin) if pin else None,
        )
        self.approvers[approver.id] = approver
        return approver

    async def get_approver(self, approver_id: UUID) -> Approver | None:
        return self.approvers.get(approver_id)

    async def create_approval(
        self, req: NewApprovalRequest, expires_at: datetime
    ) -> ApprovalRequest:
        approval = ApprovalRequest(
            id=uuid4(),
            tenant_id=req.tenant_id,
            approver_id=req.approver_id,
            status=ApprovalStatus.PENDING,
            risk_level=req.risk_level,
            summary=req.summary,
            facts=req.facts,
            claims=req.claims,
            expires_at=expires_at,
            created_at=datetime.now(UTC),
        )
        self.approvals[approval.id] = approval
        return approval

    async def get_approval(self, approval_id: UUID) -> ApprovalRequest | None:
        return self.approvals.get(approval_id)

    async def mark_calling(self, approval_id: UUID, call_sid: str) -> None:
        approval = self.approvals[approval_id]
        if approval.status == ApprovalStatus.PENDING:
            self.approvals[approval_id] = approval.model_copy(
                update={"status": ApprovalStatus.CALLING, "call_sid": call_sid}
            )

    async def resolve(
        self, approval_id: UUID, status: ApprovalStatus, reason: str | None
    ) -> ApprovalRequest | None:
        approval = self.approvals.get(approval_id)
        if approval is None or not approval.is_open:
            return None
        resolved = approval.model_copy(
            update={"status": status, "decision_reason": reason, "decided_at": datetime.now(UTC)}
        )
        self.approvals[approval_id] = resolved
        return resolved

    async def expire_overdue(self) -> list[ApprovalRequest]:
        now = datetime.now(UTC)
        overdue = [a.id for a in self.approvals.values() if a.is_open and a.expires_at < now]
        expired = [
            await self.resolve(i, ApprovalStatus.EXPIRED, "no decision before the deadline")
            for i in overdue
        ]
        return [a for a in expired if a is not None]

    async def log_event(self, approval_id: UUID, kind: str, content: dict[str, Any]) -> None:
        self.events.append((approval_id, kind, content))

    async def wait_for_resolution(self, approval_id: UUID, timeout: float) -> ApprovalRequest:
        deadline = time.monotonic() + timeout
        while True:
            approval = self.approvals[approval_id]
            if not approval.is_open or time.monotonic() >= deadline:
                return approval
            await asyncio.sleep(0.01)

    def kinds(self) -> list[str]:
        return [kind for _, kind, _ in self.events]


class FakeTelephony:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[UUID, str]] = []
        self.hung_up: list[str] = []

    async def place_call(self, approval: ApprovalRequest, approver: Approver) -> str:
        if self.fail:
            raise RuntimeError("Twilio is down")
        sid = f"CA{len(self.calls):032d}"
        self.calls.append((approval.id, approver.phone_e164))
        return sid

    async def hang_up(self, call_sid: str) -> None:
        self.hung_up.append(call_sid)


class FakeAgent:
    """Stands in for a voice agent: each run_turn speaks the next scripted reply.

    A scripted outcome that is an exception is raised after the tokens are spoken.
    """

    def __init__(self, script: list[tuple[list[str], AgentOutcome | Exception | None]]) -> None:
        self.script = list(script)
        self.user_messages: list[str] = []
        self.call_events: list[str] = []
        self.turns = 0

    def add_user_message(self, text: str) -> None:
        self.user_messages.append(text)

    def add_call_event(self, text: str) -> None:
        self.call_events.append(text)

    async def run_turn(self, on_text: Callable[[str], Awaitable[None]]) -> AgentOutcome | None:
        self.turns += 1
        tokens, outcome = self.script.pop(0)
        for token in tokens:
            await on_text(token)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def approval() -> ApprovalRequest:
    req = delete_customers_request(uuid4())
    return ApprovalRequest(
        id=uuid4(),
        status=ApprovalStatus.CALLING,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        created_at=datetime.now(UTC),
        **req.model_dump(exclude={"timeout_seconds"}),
    )


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def telephony() -> FakeTelephony:
    return FakeTelephony()
