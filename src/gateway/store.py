import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import asyncpg

from .models import ApprovalRequest, ApprovalStatus, Approver, NewApprovalRequest

log = logging.getLogger(__name__)

# Fired by the approval_requests trigger whenever a request reaches a terminal status.
RESOLVED_CHANNEL = "approval_resolved"
# Re-read the row at least this often while waiting, in case a NOTIFY was missed.
_POLL_SECONDS = 5.0


class ApprovalStore(Protocol):
    async def get_approver(self, approver_id: UUID) -> Approver | None: ...

    async def create_approval(
        self, req: NewApprovalRequest, expires_at: datetime
    ) -> ApprovalRequest: ...

    async def get_approval(self, approval_id: UUID) -> ApprovalRequest | None: ...

    async def mark_calling(self, approval_id: UUID, call_sid: str) -> None: ...

    async def resolve(
        self, approval_id: UUID, status: ApprovalStatus, reason: str | None
    ) -> ApprovalRequest | None:
        """Move an open request to a terminal status.

        Returns None if the request was already resolved; the first decision wins.
        """
        ...

    async def expire_overdue(self) -> list[ApprovalRequest]: ...

    async def log_event(self, approval_id: UUID, kind: str, content: dict[str, Any]) -> None: ...

    async def wait_for_resolution(self, approval_id: UUID, timeout: float) -> ApprovalRequest:
        """Block until the request is resolved or the timeout passes; return its latest state."""
        ...


class PostgresStore:
    def __init__(self, pool: asyncpg.Pool, listener: asyncpg.Connection) -> None:
        self._pool = pool
        self._listener = listener
        self._waiters: dict[UUID, set[asyncio.Event]] = {}

    @classmethod
    async def connect(cls, dsn: str) -> "PostgresStore":
        pool = await asyncpg.create_pool(dsn, init=_init_connection, min_size=1, max_size=10)
        listener = await asyncpg.connect(dsn)
        store = cls(pool, listener)
        await listener.add_listener(RESOLVED_CHANNEL, store._on_resolved)
        return store

    async def close(self) -> None:
        await self._listener.close()
        await self._pool.close()

    def _on_resolved(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            approval_id = UUID(payload)
        except ValueError:
            return
        for event in self._waiters.get(approval_id, ()):
            event.set()

    async def get_approver(self, approver_id: UUID) -> Approver | None:
        row = await self._pool.fetchrow("select * from approvers where id = $1", approver_id)
        return Approver.model_validate(dict(row)) if row else None

    async def create_approval(
        self, req: NewApprovalRequest, expires_at: datetime
    ) -> ApprovalRequest:
        row = await self._pool.fetchrow(
            """
            insert into approval_requests
                (tenant_id, approver_id, risk_level, summary, facts, claims, expires_at)
            values ($1, $2, $3, $4, $5, $6, $7)
            returning *
            """,
            req.tenant_id,
            req.approver_id,
            req.risk_level.value,
            req.summary,
            req.facts.model_dump(mode="json"),
            req.claims.model_dump(mode="json"),
            expires_at,
        )
        return _approval(row)

    async def get_approval(self, approval_id: UUID) -> ApprovalRequest | None:
        row = await self._pool.fetchrow(
            "select * from approval_requests where id = $1", approval_id
        )
        return _approval(row) if row else None

    async def mark_calling(self, approval_id: UUID, call_sid: str) -> None:
        await self._pool.execute(
            """
            update approval_requests set status = 'calling', call_sid = $2
            where id = $1 and status = 'pending'
            """,
            approval_id,
            call_sid,
        )

    async def resolve(
        self, approval_id: UUID, status: ApprovalStatus, reason: str | None
    ) -> ApprovalRequest | None:
        row = await self._pool.fetchrow(
            """
            update approval_requests
            set status = $2, decision_reason = $3, decided_at = now()
            where id = $1 and status in ('pending', 'calling')
            returning *
            """,
            approval_id,
            status.value,
            reason,
        )
        return _approval(row) if row else None

    async def expire_overdue(self) -> list[ApprovalRequest]:
        rows = await self._pool.fetch(
            """
            update approval_requests
            set status = 'expired', decision_reason = 'no decision before the deadline',
                decided_at = now()
            where status in ('pending', 'calling') and expires_at < now()
            returning *
            """
        )
        return [_approval(row) for row in rows]

    async def log_event(self, approval_id: UUID, kind: str, content: dict[str, Any]) -> None:
        await self._pool.execute(
            "insert into call_events (approval_id, kind, content) values ($1, $2, $3)",
            approval_id,
            kind,
            content,
        )

    async def wait_for_resolution(self, approval_id: UUID, timeout: float) -> ApprovalRequest:
        event = asyncio.Event()
        self._waiters.setdefault(approval_id, set()).add(event)
        deadline = time.monotonic() + timeout
        try:
            while True:
                approval = await self.get_approval(approval_id)
                if approval is None:
                    raise LookupError(f"approval {approval_id} not found")
                remaining = deadline - time.monotonic()
                if not approval.is_open or remaining <= 0:
                    return approval
                event.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(event.wait(), min(remaining, _POLL_SECONDS))
        finally:
            waiters = self._waiters.get(approval_id)
            if waiters is not None:
                waiters.discard(event)
                if not waiters:
                    del self._waiters[approval_id]


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


def _approval(row: asyncpg.Record) -> ApprovalRequest:
    return ApprovalRequest.model_validate(dict(row))
