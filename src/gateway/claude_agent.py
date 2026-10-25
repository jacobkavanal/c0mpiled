from collections.abc import Awaitable, Callable
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from .models import ApprovalRequest
from .prompts import SYSTEM_PROMPT, call_event, render_approval_context
from .voice_agent import (
    TOOLS,
    AgentOutcome,
    ToolCall,
    VoiceModelError,
    invalid_tool_call_result,
    parse_tool_calls,
)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000


class ClaudeVoiceAgent:
    """One phone conversation with Claude about one approval request.

    The history is append-only: assistant turns go back exactly as the API returned
    them, so thinking and fallback blocks stay valid across turns.
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        approval: ApprovalRequest,
        *,
        model: str,
        effort: str,
        max_invalid_tool_calls: int = 2,
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._max_invalid_tool_calls = max_invalid_tool_calls
        self._messages: list[dict[str, Any]] = [
            {"role": "user", "content": render_approval_context(approval)}
        ]

    def add_user_message(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def add_call_event(self, text: str) -> None:
        self._messages.append({"role": "user", "content": call_event(text)})

    async def run_turn(self, on_text: Callable[[str], Awaitable[None]]) -> AgentOutcome | None:
        invalid_tool_calls = 0
        while True:
            try:
                async with self._client.beta.messages.stream(**self._request()) as stream:
                    async for event in stream:
                        if event.type == "text":
                            await on_text(event.text)
                    message = await stream.get_final_message()
            except anthropic.APIError as exc:
                raise VoiceModelError("Claude request failed") from exc
            except ValueError:
                # Tool input the SDK could not parse at all; there is no tool_use_id to
                # answer, so re-issue the turn.
                invalid_tool_calls += 1
                if invalid_tool_calls > self._max_invalid_tool_calls:
                    return AgentOutcome("error", "voice model produced unparseable tool input")
                continue

            if message.stop_reason == "refusal":
                return AgentOutcome("refusal", "voice model declined to continue")
            if message.stop_reason == "max_tokens":
                return AgentOutcome("error", "voice model reply was cut off")

            tool_uses = [block for block in message.content if block.type == "tool_use"]
            if not tool_uses:
                self._messages.append({"role": "assistant", "content": message.content})
                return None

            calls = [ToolCall(block.id, block.name, block.input) for block in tool_uses]
            outcome = parse_tool_calls(calls)
            # Appending the assistant turn and its tool results together keeps the
            # history valid even if this task is cancelled at the next await.
            self._messages.append({"role": "assistant", "content": message.content})
            if outcome is not None:
                return outcome
            self._messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "is_error": True,
                            "content": invalid_tool_call_result(call),
                        }
                        for call in calls
                    ],
                }
            )
            invalid_tool_calls += 1
            if invalid_tool_calls > self._max_invalid_tool_calls:
                return AgentOutcome("error", "voice model kept producing invalid tool input")

    def _request(self) -> dict[str, Any]:
        return {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
            "output_config": {"effort": self._effort},
            "cache_control": {"type": "ephemeral"},
            "system": SYSTEM_PROMPT,
            "tools": TOOLS,
            "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            "messages": self._messages,
        }
