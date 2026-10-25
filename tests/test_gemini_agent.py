import json

import openai
import pytest

from gateway.openai_compat_agent import OpenAICompatibleVoiceAgent
from gateway.prompts import SYSTEM_PROMPT, render_approval_context
from gateway.voice_agent import AgentOutcome, VoiceModelError

from .openai_fakes import fake_openai, finish_chunk, text_chunk, tool_chunk


def make_agent(client, approval):
    return OpenAICompatibleVoiceAgent(
        client, approval, model="gemini-3.8-flash", reasoning_effort="low"
    )


async def collect(agent):
    spoken = []

    async def on_text(token):
        spoken.append(token)

    outcome = await agent.run_turn(on_text)
    return "".join(spoken), outcome


async def test_spoken_turn_streams_text_and_sends_expected_request(approval):
    client, completions = fake_openai(
        [
            text_chunk("Your agent wants "),
            text_chunk("to delete 200 records."),
            finish_chunk("stop"),
        ],
        [text_chunk("It says they are duplicates."), finish_chunk("stop")],
    )
    agent = make_agent(client, approval)
    agent.add_call_event("The call is connected.")

    spoken, outcome = await collect(agent)

    assert spoken == "Your agent wants to delete 200 records."
    assert outcome is None
    request = completions.requests[0]
    assert request["model"] == "gemini-3.8-flash"
    assert request["stream"] is True
    assert request["reasoning_effort"] == "low"
    assert request["tool_choice"] == "auto"
    assert [t["function"]["name"] for t in request["tools"]] == [
        "record_decision",
        "end_call_without_decision",
    ]
    assert request["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    # Consecutive user turns are merged, so the context and the call event are one message.
    assert len(request["messages"]) == 2
    assert request["messages"][1]["role"] == "user"
    assert request["messages"][1]["content"].startswith(render_approval_context(approval))
    assert "<call_event>The call is connected.</call_event>" in request["messages"][1]["content"]

    agent.add_user_message("Why is it deleting them?")
    await collect(agent)
    assert completions.requests[1]["messages"][2] == {
        "role": "assistant",
        "content": "Your agent wants to delete 200 records.",
    }
    assert completions.requests[1]["messages"][3] == {
        "role": "user",
        "content": "Why is it deleting them?",
    }


async def test_tool_call_streamed_in_fragments_returns_outcome(approval):
    client, _ = fake_openai(
        [
            text_chunk("Okay."),
            tool_chunk(0, call_id="call_1", name="record_decision", arguments='{"decision": "de'),
            tool_chunk(0, arguments='ny", "reason": "no proof of duplicates"}'),
            finish_chunk("tool_calls"),
        ]
    )

    spoken, outcome = await collect(make_agent(client, approval))

    assert spoken == "Okay."
    assert outcome == AgentOutcome("deny", "no proof of duplicates")


async def test_tool_call_is_used_even_when_finish_reason_says_stop(approval):
    client, _ = fake_openai(
        [
            tool_chunk(
                0, call_id="c", name="end_call_without_decision", arguments='{"reason": "later"}'
            ),
            finish_chunk("stop"),
        ]
    )

    _, outcome = await collect(make_agent(client, approval))

    assert outcome == AgentOutcome("no_decision", "later")


async def test_invalid_tool_call_is_retried_and_signature_echoed_back(approval):
    signature = {"extra_content": {"google": {"thought_signature": "sig-123"}}}
    client, completions = fake_openai(
        [
            tool_chunk(
                0, call_id="call_1", name="record_decision", arguments="{not json", extra=signature
            )
        ],
        [
            tool_chunk(
                0,
                call_id="call_2",
                name="record_decision",
                arguments='{"decision": "deny", "reason": ""}',
            )
        ],
    )

    _, outcome = await collect(make_agent(client, approval))

    assert outcome == AgentOutcome("deny", "")
    retry = completions.requests[1]["messages"]
    assistant, tool_result = retry[-2], retry[-1]
    assert assistant["role"] == "assistant"
    call = assistant["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["function"] == {"name": "record_decision", "arguments": "{not json"}
    assert call["extra_content"] == signature["extra_content"]
    assert tool_result["role"] == "tool"
    assert tool_result["tool_call_id"] == "call_1"
    error = json.loads(tool_result["content"])["error"]
    assert "record_decision" in error and "end_call_without_decision" in error


async def test_gives_up_after_repeated_invalid_tool_calls(approval):
    bad = [
        tool_chunk(
            0, call_id="c", name="record_decision", arguments='{"decision": "maybe", "reason": ""}'
        )
    ]
    client, completions = fake_openai(bad, bad, bad)

    _, outcome = await collect(make_agent(client, approval))

    assert outcome.kind == "error"
    assert len(completions.requests) == 3


@pytest.mark.parametrize(
    ("finish_reason", "kind"), [("length", "error"), ("content_filter", "refusal")]
)
async def test_truncation_and_safety_stop_end_the_call(approval, finish_reason, kind):
    client, _ = fake_openai([text_chunk("Your agent"), finish_chunk(finish_reason)])

    _, outcome = await collect(make_agent(client, approval))

    assert outcome.kind == kind


async def test_api_errors_become_voice_model_errors(approval):
    client, _ = fake_openai(openai.APIError.__new__(openai.APIError))

    with pytest.raises(VoiceModelError):
        await collect(make_agent(client, approval))
