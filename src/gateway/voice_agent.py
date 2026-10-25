"""What every voice agent shares, whichever model provider runs it: the tools the
model ends a call with, how their input is checked, and the outcome the relay acts on."""

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from .models import ApprovalRequest

log = logging.getLogger(__name__)

RECORD_DECISION = "record_decision"
END_WITHOUT_DECISION = "end_call_without_decision"

# Tool definitions in Anthropic's format; other providers convert from these.
TOOLS: list[dict[str, Any]] = [
    {
        "name": RECORD_DECISION,
        "description": (
            "Record the approver's decision on the held action. This ends the call. Call it "
            "only with a decision the approver gave. For approve, only after they clearly "
            "confirmed your read-back of the action."
        ),
        "strict": True,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["approve", "deny"]},
                "reason": {
                    "type": "string",
                    "description": "The approver's reason in their own words, or an empty "
                    "string if they gave none.",
                },
            },
            "required": ["decision", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": END_WITHOUT_DECISION,
        "description": (
            "End the call without a decision, when the approver wants to decide later, can't "
            "decide, or asks to hang up. The action stays blocked."
        ),
        "strict": True,
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why the call is ending."},
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
]

INVALID_TOOL_CALL = (
    f"Invalid tool call. Call exactly one tool, named exactly {RECORD_DECISION} or "
    f"{END_WITHOUT_DECISION}, with input matching its schema."
)


@dataclass(frozen=True)
class AgentOutcome:
    kind: Literal["approve", "deny", "no_decision", "refusal", "error"]
    reason: str = ""


class VoiceModelError(Exception):
    """The model provider could not be reached or rejected the request."""


class VoiceAgent(Protocol):
    """One phone conversation about one approval request."""

    def add_user_message(self, text: str) -> None: ...

    def add_call_event(self, text: str) -> None: ...

    async def run_turn(self, on_text: Callable[[str], Awaitable[None]]) -> AgentOutcome | None:
        """Generate one spoken reply, streaming its text through on_text.

        Returns an outcome when the model ended the conversation with a tool call, or
        None when it is waiting for the approver to speak.
        """
        ...


AgentFactory = Callable[[ApprovalRequest], VoiceAgent]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    # The parsed JSON input, or the raw text when it was not valid JSON.
    arguments: Any


class _DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "deny"]
    reason: str


class _EndInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


def parse_tool_calls(calls: Sequence[ToolCall]) -> AgentOutcome | None:
    """The outcome of a turn's tool calls, or None if they must be rejected and retried."""
    if len(calls) != 1:
        return None
    call = calls[0]
    # Models occasionally vary the letter case of a tool name; the match is
    # unambiguous, so accept it rather than spend a round trip.
    name = call.name.lower()
    try:
        if name == RECORD_DECISION:
            parsed = _DecisionInput.model_validate(call.arguments)
            return AgentOutcome(parsed.decision, parsed.reason)
        if name == END_WITHOUT_DECISION:
            parsed_end = _EndInput.model_validate(call.arguments)
            return AgentOutcome("no_decision", parsed_end.reason)
    except ValidationError:
        log.warning("invalid %s input: %r", call.name, call.arguments)
    return None


def invalid_tool_call_result(call: ToolCall) -> str:
    """Error content sent back so the model can correct the call."""
    return json.dumps(
        {"INVALID_JSON": json.dumps(call.arguments, default=str), "error": INVALID_TOOL_CALL}
    )
