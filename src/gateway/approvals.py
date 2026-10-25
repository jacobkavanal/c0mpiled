import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from .config import Settings
from .models import ApprovalRequest, ApprovalStatus, NewApprovalRequest
from .store import ApprovalStore
from .telephony import Telephony

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 15


class ApproverNotFound(LookupError):
    pass


class ApprovalService:
    """Entry point for the MCP proxy: hold a risky tool call until a human decides on the phone.

    Fails closed. Only ApprovalStatus.APPROVED means the call may be forwarded;
    expiry, unanswered calls, voicemail, and errors all leave the action blocked.
    """

    def __init__(self, store: ApprovalStore, telephony: Telephony, settings: Settings) -> None:
        self._store = store
        self._telephony = telephony
        self._settings = settings

    async def start(self, req: NewApprovalRequest) -> ApprovalRequest:
        """Create the request and phone the approver. Returns without waiting for a decision."""
        approver = await self._store.get_approver(req.approver_id)
        if approver is None or approver.tenant_id != req.tenant_id:
            raise ApproverNotFound(f"approver {req.approver_id} not found for this tenant")

        timeout = req.timeout_seconds or self._settings.approval_timeout_seconds
        expires_at = datetime.now(UTC) + timedelta(seconds=timeout)
        approval = await self._store.create_approval(req, expires_at)

        try:
            call_sid = await self._telephony.place_call(approval, approver)
        except Exception as exc:
            log.exception("could not place approval call for %s", approval.id)
            failed = await self._store.resolve(
                approval.id, ApprovalStatus.FAILED, f"could not place call: {exc}"
            )
            return failed or approval

        await self._store.mark_calling(approval.id, call_sid)
        await self._store.log_event(approval.id, "call_placed", {"call_sid": call_sid})
        return await self._store.get_approval(approval.id) or approval

    async def wait(self, approval_id: UUID, timeout: float | None = None) -> ApprovalRequest:
        """Wait for a decision, up to the request's deadline (or the given timeout, if sooner)."""
        approval = await self._store.get_approval(approval_id)
        if approval is None:
            raise LookupError(f"approval {approval_id} not found")
        until_deadline = (approval.expires_at - datetime.now(UTC)).total_seconds()
        limit = until_deadline if timeout is None else min(timeout, until_deadline)
        approval = await self._store.wait_for_resolution(approval_id, max(limit, 0))
        if approval.is_open and approval.expires_at <= datetime.now(UTC):
            await self.expire_overdue()
            approval = await self._store.get_approval(approval_id) or approval
        return approval

    async def request_approval(self, req: NewApprovalRequest) -> ApprovalRequest:
        """Start the call and block until the request is resolved or expires."""
        approval = await self.start(req)
        if not approval.is_open:
            return approval
        return await self.wait(approval.id)

    async def expire_overdue(self) -> int:
        expired = await self._store.expire_overdue()
        for approval in expired:
            await self._store.log_event(approval.id, "expired", {})
            if approval.call_sid:
                try:
                    await self._telephony.hang_up(approval.call_sid)
                except Exception:
                    log.exception("could not hang up call %s", approval.call_sid)
        return len(expired)

    async def sweep_forever(self) -> None:
        while True:
            try:
                await self.expire_overdue()
            except Exception:
                log.exception("expiry sweep failed")
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
