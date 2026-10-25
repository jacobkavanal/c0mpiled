"""Voice agent over an OpenAI-compatible Chat Completions API. Used for Gemini, through
Google's OpenAI compatibility endpoint."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import openai
from openai import AsyncOpenAI

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


def _openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    # Tool input is validated locally, so the schema sent to the provider keeps to the
    # widely supported subset of JSON Schema.
    parameters = {k: v for k, v in tool["input_schema"].items() if k != "additionalProperties"}
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": parameters,
        },
    }


OPENAI_TOOLS = [_openai_tool(tool) for tool in TOOLS]


@dataclass
class _PendingCall:
    """A tool call assembled from streamed fragments."""

    id: str | None = None
    name: str = ""
    arguments: str = ""
    # Provider-specific fields on the call, such as Gemini's thought signature. They are
    # sent back unchanged with the call.
    extra: dict[str, Any] = field(default_factory=dict)

    def add(self, delta: Any) -> None:
        if delta.id:
            self.id = delta.id
        if delta.function is not None:
            if delta.function.name:
                self.name = delta.function.name
            self.arguments += delta.function.arguments or ""
        self.extra.update(getattr(delta, "model_extra", None) or {})


class OpenAICompatibleVoiceAgent:
    """One phone conversation about one approval request, through Chat Completions."""

    def __init__(
        self,
        client: AsyncOpenAI,
        approval: ApprovalRequest,
        *,
        model: str,
        reasoning_effort: str,
        max_invalid_tool_calls: int = 2,
    ) -> None:
        self._client = client
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_invalid_tool_calls = max_invalid_tool_calls
        self._messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._add_user(render_approval_context(approval))

    def add_user_message(self, text: str) -> None:
        self._add_user(text)

    def add_call_event(self, text: str) -> None:
        self._add_user(call_event(text))

    def _add_user(self, text: str) -> None:
        # Merge consecutive user turns; not every compatible API accepts them separately.
        last = self._messages[-1]
        if last["role"] == "user":
            last["content"] = f"{last['content']}\n\n{text}"
        else:
            self._messages.append({"role": "user", "content": text})

    async def run_turn(self, on_text: Callable[[str], Awaitable[None]]) -> AgentOutcome | None:
        invalid_tool_calls = 0
        while True:
            try:
                text, pending, finish_reason = await self._stream(on_text)
            except openai.APIError as exc:
                raise VoiceModelError("voice model request failed") from exc

            if finish_reason == "content_filter":
                return AgentOutcome("refusal", "voice model declined to continue")
            if finish_reason == "length":
                return AgentOutcome("error", "voice model reply was cut off")
            # Decided by the calls themselves: some compatible APIs report "stop" even
            # when the reply is a tool call.
            if not pending:
                self._messages.append({"role": "assistant", "content": text})
                return None

            calls = [
                ToolCall(call.id or f"call_{i}", call.name, _parse_arguments(call.arguments))
                for i, call in enumerate(pending)
            ]
            outcome = parse_tool_calls(calls)
            self._messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": raw.arguments},
                            **raw.extra,
                        }
                        for call, raw in zip(calls, pending, strict=True)
                    ],
                }
            )
            if outcome is not None:
                return outcome
            self._messages.extend(
                {"role": "tool", "tool_call_id": call.id, "content": invalid_tool_call_result(call)}
                for call in calls
            )
            invalid_tool_calls += 1
            if invalid_tool_calls > self._max_invalid_tool_calls:
                return AgentOutcome("error", "voice model kept producing invalid tool input")

    async def _stream(
        self, on_text: Callable[[str], Awaitable[None]]
    ) -> tuple[str, list[_PendingCall], str | None]:
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=self._messages,
            tools=OPENAI_TOOLS,
            tool_choice="auto",
            reasoning_effort=self._reasoning_effort,
            stream=True,
        )
        text: list[str] = []
        calls: dict[int, _PendingCall] = {}
        finish_reason = None
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.delta.content:
                text.append(choice.delta.content)
                await on_text(choice.delta.content)
            for position, delta in enumerate(choice.delta.tool_calls or []):
                index = delta.index if delta.index is not None else position
                calls.setdefault(index, _PendingCall()).add(delta)
            if choice.finish_reason:
                finish_reason = choice.finish_reason
        return "".join(text), [calls[i] for i in sorted(calls)], finish_reason


def _parse_arguments(arguments: str) -> Any:
    try:
        return json.loads(arguments or "{}")
    except ValueError:
        return arguments
