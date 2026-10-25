"""Twilio ConversationRelay session: Twilio does speech-to-text and text-to-speech,
and this WebSocket carries the text between the call and the voice agent."""

import asyncio
import contextlib
import enum
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from .config import Settings
from .models import ApprovalRequest, ApprovalStatus, Approver
from .security import verify_call_token, verify_pin
from .store import ApprovalStore
from .voice_agent import AgentFactory, AgentOutcome, VoiceAgent, VoiceModelError

log = logging.getLogger(__name__)

SETUP_TIMEOUT_SECONDS = 10
MAX_PIN_DIGITS = 12

PIN_PROMPT = "Before I share any details, please enter your PIN on the keypad, then press pound."
PIN_RETRY = "That PIN was not correct. Please try again, then press pound."
PIN_LOCKED = "That PIN was not correct. For security, this call will now end."
MODEL_UNAVAILABLE = "Sorry, I'm having technical trouble, so I can't take a decision right now."

_OUTCOME_STATUS = {
    "approve": ApprovalStatus.APPROVED,
    "deny": ApprovalStatus.DENIED,
}


class _Phase(enum.Enum):
    AWAITING_PIN = enum.auto()
    TALKING = enum.auto()
    CLOSING = enum.auto()


class RelaySession:
    def __init__(
        self,
        ws: WebSocket,
        *,
        settings: Settings,
        store: ApprovalStore,
        agent_factory: AgentFactory,
    ) -> None:
        self._ws = ws
        self._settings = settings
        self._store = store
        self._agent_factory = agent_factory
        self._phase = _Phase.TALKING
        self._turn: asyncio.Task[None] | None = None
        self._pin_digits = ""
        self._pin_attempts = 0
        self._approval: ApprovalRequest
        self._approver: Approver
        self._agent: VoiceAgent

    async def run(self) -> None:
        await self._ws.accept()
        if not await self._handle_setup():
            await self._ws.close(code=1008)
            return
        try:
            while True:
                await self._dispatch(await self._ws.receive_json())
        except WebSocketDisconnect:
            pass
        finally:
            await self._cleanup()

    async def _handle_setup(self) -> bool:
        try:
            setup = await asyncio.wait_for(self._ws.receive_json(), SETUP_TIMEOUT_SECONDS)
        except (TimeoutError, WebSocketDisconnect, ValueError):
            return False
        if setup.get("type") != "setup":
            return False
        token = (setup.get("customParameters") or {}).get("token", "")
        approval_id = verify_call_token(self._settings.call_token_secret.get_secret_value(), token)
        if approval_id is None:
            log.warning("relay setup with invalid call token")
            return False
        approval = await self._store.get_approval(approval_id)
        if approval is None or not approval.is_open:
            return False
        approver = await self._store.get_approver(approval.approver_id)
        if approver is None:
            return False

        self._approval, self._approver = approval, approver
        self._agent = self._agent_factory(approval)
        await self._log("call_connected", {"call_sid": setup.get("callSid")})
        if approver.pin_hash:
            self._phase = _Phase.AWAITING_PIN
            await self._say(PIN_PROMPT)
        else:
            self._start_opening()
        return True

    async def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "prompt":
            if message.get("last", True):
                await self._on_prompt(message.get("voicePrompt", ""))
        elif kind == "interrupt":
            await self._on_interrupt(message.get("utteranceUntilInterrupt", ""))
        elif kind == "dtmf":
            await self._on_dtmf(message.get("digit", ""))
        elif kind == "error":
            log.error("ConversationRelay error: %s", message.get("description"))
            await self._log("relay_error", {"description": message.get("description")})

    async def _on_prompt(self, text: str) -> None:
        if self._phase is _Phase.CLOSING or not text.strip():
            return
        if self._phase is _Phase.AWAITING_PIN:
            # Speech goes nowhere until the approver has proven who they are.
            await self._say(PIN_PROMPT)
            return
        await self._log("approver", {"text": text})
        await self._cancel_turn()
        self._agent.add_user_message(text)
        self._turn = asyncio.create_task(self._run_turn())

    async def _on_interrupt(self, heard: str) -> None:
        if self._phase is not _Phase.TALKING:
            return
        await self._cancel_turn()
        await self._log("interrupt", {"heard": heard})
        self._agent.add_call_event(
            f'The approver interrupted you. They heard only: "{heard}"'
            if heard
            else "The approver interrupted you before hearing your reply."
        )

    async def _on_dtmf(self, digit: str) -> None:
        if self._phase is not _Phase.AWAITING_PIN:
            return
        if digit == "*":
            self._pin_digits = ""
        elif digit.isdigit() and len(self._pin_digits) < MAX_PIN_DIGITS:
            self._pin_digits += digit
        elif digit == "#":
            await self._check_pin()

    async def _check_pin(self) -> None:
        entered, self._pin_digits = self._pin_digits, ""
        assert self._approver.pin_hash is not None
        if verify_pin(entered, self._approver.pin_hash):
            await self._log("pin", {"result": "verified"})
            self._phase = _Phase.TALKING
            self._start_opening()
            return
        self._pin_attempts += 1
        await self._log("pin", {"result": "incorrect", "attempt": self._pin_attempts})
        if self._pin_attempts < self._settings.pin_max_attempts:
            await self._say(PIN_RETRY)
            return
        self._phase = _Phase.CLOSING
        await self._say(PIN_LOCKED)
        await self._store.resolve(
            self._approval.id, ApprovalStatus.FAILED, "PIN verification failed"
        )
        await self._end()

    def _start_opening(self) -> None:
        self._agent.add_call_event(
            "The call is connected and the approver is listening. Give your opening explanation."
        )
        self._turn = asyncio.create_task(self._run_turn())

    async def _run_turn(self) -> None:
        spoken: list[str] = []

        async def on_text(token: str) -> None:
            spoken.append(token)
            await self._send({"type": "text", "token": token, "last": False})

        try:
            outcome = await self._agent.run_turn(on_text)
        except VoiceModelError:
            log.exception("voice model request failed")
            outcome = AgentOutcome("error", "voice model unavailable")
            spoken.append(MODEL_UNAVAILABLE)
            await self._send({"type": "text", "token": MODEL_UNAVAILABLE, "last": False})

        if outcome is not None:
            # From here on, approver speech can't cancel the decision being recorded.
            self._phase = _Phase.CLOSING
        await asyncio.shield(self._after_turn("".join(spoken), outcome))

    async def _after_turn(self, text: str, outcome: AgentOutcome | None) -> None:
        if text:
            await self._send({"type": "text", "token": "", "last": True})
            await self._log("assistant", {"text": text})
        if outcome is None:
            return
        await self._log("outcome", {"kind": outcome.kind, "reason": outcome.reason})
        status = _OUTCOME_STATUS.get(outcome.kind, ApprovalStatus.FAILED)
        reason = outcome.reason or None
        if outcome.kind == "no_decision":
            reason = f"approver ended the call without deciding: {outcome.reason}"
        if await self._store.resolve(self._approval.id, status, reason) is None:
            log.info("approval %s was already resolved; %s not recorded", self._approval.id, status)
        await self._end()

    async def _cancel_turn(self) -> None:
        if self._turn is None or self._turn.done():
            return
        self._turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._turn

    async def _cleanup(self) -> None:
        if self._phase is not _Phase.CLOSING:
            await self._cancel_turn()
        elif self._turn is not None:
            # Let the decision finish recording even though the socket is gone.
            with contextlib.suppress(Exception):
                await self._turn
        if hasattr(self, "_approval"):
            await self._store.resolve(
                self._approval.id, ApprovalStatus.FAILED, "call ended without a decision"
            )

    async def _say(self, text: str) -> None:
        await self._send({"type": "text", "token": text, "last": True})

    async def _end(self) -> None:
        await self._send({"type": "end"})

    async def _send(self, message: dict[str, Any]) -> None:
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await self._ws.send_json(message)

    async def _log(self, kind: str, content: dict[str, Any]) -> None:
        try:
            await self._store.log_event(self._approval.id, kind, content)
        except Exception:
            log.exception("failed to log %s event", kind)
