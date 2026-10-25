import asyncio
import contextlib
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .approvals import ApprovalService, ApproverNotFound
from .config import Settings, get_settings
from .models import ApprovalRequest, ApprovalStatus, NewApprovalRequest
from .relay import RelaySession
from .security import sign_call_token, twilio_signature_valid
from .store import ApprovalStore, PostgresStore
from .telephony import (
    NOT_A_PERSON,
    Telephony,
    TwilioTelephony,
    closing_twiml,
    hangup_twiml,
    relay_twiml,
)
from .voice_agent import AgentFactory
from .voice_providers import make_agent_factory

log = logging.getLogger(__name__)

TWIML = "application/xml"
# Twilio call statuses after which no conversation can happen.
CALL_ENDED = {"completed", "busy", "no-answer", "failed", "canceled"}


@dataclass
class Deps:
    settings: Settings
    store: ApprovalStore
    telephony: Telephony
    service: ApprovalService
    agent_factory: AgentFactory


def create_app(
    *,
    settings: Settings | None = None,
    store: ApprovalStore | None = None,
    telephony: Telephony | None = None,
    agent_factory: AgentFactory | None = None,
    run_sweeper: bool = True,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Built first so a missing voice model key stops startup with a clear message.
        active_agent_factory = agent_factory or make_agent_factory(settings)
        owned_store = None
        active_store = store
        if active_store is None:
            owned_store = await PostgresStore.connect(settings.database_url.get_secret_value())
            active_store = owned_store
        active_telephony = telephony or TwilioTelephony(settings)
        service = ApprovalService(active_store, active_telephony, settings)
        app.state.deps = Deps(
            settings=settings,
            store=active_store,
            telephony=active_telephony,
            service=service,
            agent_factory=active_agent_factory,
        )
        sweeper = asyncio.create_task(service.sweep_forever()) if run_sweeper else None
        try:
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper
            if owned_store is not None:
                await owned_store.close()

    app = FastAPI(title="c0mpiled approval gateway", lifespan=lifespan)
    _add_api_routes(app)
    _add_twilio_routes(app)
    return app


def _deps(request: Request) -> Deps:
    return request.app.state.deps


DepsDep = Annotated[Deps, Depends(_deps)]
_bearer = HTTPBearer(auto_error=False)


def _require_internal_key(
    deps: DepsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    expected = deps.settings.internal_api_key.get_secret_value()
    if credentials is None or not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED)


def _add_api_routes(app: FastAPI) -> None:
    auth = [Depends(_require_internal_key)]

    @app.post("/v1/approvals", status_code=201, dependencies=auth)
    async def create_approval(req: NewApprovalRequest, deps: DepsDep) -> ApprovalRequest:
        try:
            return await deps.service.start(req)
        except ApproverNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    @app.get("/v1/approvals/{approval_id}", dependencies=auth)
    async def get_approval(
        approval_id: UUID,
        deps: DepsDep,
        wait: Annotated[int, Query(ge=0, le=60, description="Long-poll seconds")] = 0,
    ) -> ApprovalRequest:
        try:
            if wait:
                return await deps.service.wait(approval_id, timeout=wait)
            approval = await deps.store.get_approval(approval_id)
        except LookupError:
            approval = None
        if approval is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return approval


async def _twilio_form(request: Request, deps: DepsDep) -> dict[str, str]:
    form = {key: str(value) for key, value in (await request.form()).items()}
    if deps.settings.twilio_validate_signatures and not twilio_signature_valid(
        deps.settings.twilio_auth_token.get_secret_value(),
        _public_url(deps.settings, request.url.path, request.url.query),
        form,
        request.headers.get("X-Twilio-Signature"),
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    return form


TwilioForm = Annotated[dict[str, str], Depends(_twilio_form)]


def _public_url(settings: Settings, path: str, query: str, *, websocket: bool = False) -> str:
    # Twilio signs the URL it requested, which is the public one, not what a proxy forwards.
    base = settings.public_ws_base_url if websocket else settings.public_base_url.rstrip("/")
    return f"{base}{path}" + (f"?{query}" if query else "")


def _add_twilio_routes(app: FastAPI) -> None:
    @app.post("/twilio/voice/{approval_id}")
    async def voice(approval_id: UUID, form: TwilioForm, deps: DepsDep) -> Response:
        approval = await deps.store.get_approval(approval_id)
        if approval is None or not approval.is_open:
            return Response(hangup_twiml(), media_type=TWIML)

        answered_by = form.get("AnsweredBy", "")
        if answered_by in NOT_A_PERSON:
            await deps.store.log_event(approval_id, "call_status", {"answered_by": answered_by})
            await deps.store.resolve(
                approval_id, ApprovalStatus.FAILED, f"call reached {answered_by}"
            )
            return Response(hangup_twiml(), media_type=TWIML)

        approver = await deps.store.get_approver(approval.approver_id)
        if approver is None:
            return Response(hangup_twiml(), media_type=TWIML)
        token = sign_call_token(
            deps.settings.call_token_secret.get_secret_value(),
            approval.id,
            ttl_seconds=deps.settings.max_call_seconds,
        )
        return Response(relay_twiml(deps.settings, approval, approver, token), media_type=TWIML)

    @app.post("/twilio/relay-ended/{approval_id}")
    async def relay_ended(approval_id: UUID, _form: TwilioForm, deps: DepsDep) -> Response:
        # The closing line comes from the stored outcome, not from anything the model said.
        approval = await deps.store.get_approval(approval_id)
        return Response(closing_twiml(approval), media_type=TWIML)

    @app.post("/twilio/status/{approval_id}", status_code=204)
    async def status_callback(approval_id: UUID, form: TwilioForm, deps: DepsDep) -> Response:
        call_status = form.get("CallStatus", "")
        if await deps.store.get_approval(approval_id) is None:
            return Response(status_code=204)
        await deps.store.log_event(
            approval_id,
            "call_status",
            {
                "status": call_status,
                "call_sid": form.get("CallSid"),
                "duration": form.get("CallDuration"),
            },
        )
        if call_status in CALL_ENDED:
            reason = (
                "call ended without a decision"
                if call_status == "completed"
                else f"call {call_status}"
            )
            await deps.store.resolve(approval_id, ApprovalStatus.FAILED, reason)
        return Response(status_code=204)

    @app.websocket("/twilio/relay")
    async def relay(websocket: WebSocket) -> None:
        deps: Deps = websocket.app.state.deps
        if deps.settings.twilio_validate_websocket_signature and not twilio_signature_valid(
            deps.settings.twilio_auth_token.get_secret_value(),
            _public_url(deps.settings, websocket.url.path, websocket.url.query, websocket=True),
            {},
            websocket.headers.get("X-Twilio-Signature"),
        ):
            await websocket.close(code=1008)
            return
        session = RelaySession(
            websocket, settings=deps.settings, store=deps.store, agent_factory=deps.agent_factory
        )
        await session.run()
