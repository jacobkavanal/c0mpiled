from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    CALLING = "calling"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    FAILED = "failed"


OPEN_STATUSES = frozenset({ApprovalStatus.PENDING, ApprovalStatus.CALLING})
TERMINAL_STATUSES = frozenset(ApprovalStatus) - OPEN_STATUSES


class VerifiedFacts(BaseModel):
    """What the gateway established itself, from the intercepted MCP request or its own checks."""

    tool_name: str
    mcp_server: str
    arguments: dict[str, Any]
    # Why the policy engine flagged the call, e.g. "DELETE on a production database".
    risk_reasons: list[str] = Field(default_factory=list)
    # Results of checks the gateway ran, e.g. "Dry run matched 200 rows in customers".
    observations: list[str] = Field(default_factory=list)


class AgentClaims(BaseModel):
    """What the agent says about its own request. Nothing here has been checked."""

    agent_name: str | None = None
    stated_objective: str | None = None
    justification: str | None = None


class NewApprovalRequest(BaseModel):
    tenant_id: str
    approver_id: UUID
    risk_level: RiskLevel
    # One line written by the gateway (not the agent), e.g.
    # "Delete 200 rows from the production customers table".
    summary: str
    facts: VerifiedFacts
    claims: AgentClaims
    timeout_seconds: int | None = Field(default=None, ge=30, le=3600)


class Approver(BaseModel):
    id: UUID
    tenant_id: str
    name: str
    phone_e164: str
    pin_hash: str | None = None


class ApprovalRequest(BaseModel):
    id: UUID
    tenant_id: str
    approver_id: UUID
    status: ApprovalStatus
    risk_level: RiskLevel
    summary: str
    facts: VerifiedFacts
    claims: AgentClaims
    call_sid: str | None = None
    decision_reason: str | None = None
    decided_at: datetime | None = None
    expires_at: datetime
    created_at: datetime

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_approved(self) -> bool:
        """The only state in which the held MCP request may be forwarded."""
        return self.status == ApprovalStatus.APPROVED
