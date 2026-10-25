from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from gateway.claude_agent import ClaudeVoiceAgent
from gateway.openai_compat_agent import OpenAICompatibleVoiceAgent
from gateway.voice_providers import make_agent_factory, make_client, missing_credentials

from .openai_fakes import fake_openai, finish_chunk, text_chunk


def with_voice(settings, **update):
    return settings.model_copy(update=update)


def test_default_provider_is_fast_gemini_with_minimal_thinking(settings):
    # gemini-3.8-flash took 43 s to start replying in testing; flash-lite at minimal, 0.7 s.
    assert settings.voice_provider == "gemini"
    assert settings.resolved_voice_model == "gemini-3.5-flash-lite"
    assert settings.resolved_voice_effort == "minimal"


def test_anthropic_provider_defaults_to_sonnet_at_low_effort(settings):
    anthropic = with_voice(settings, voice_provider="anthropic")
    assert anthropic.resolved_voice_model == "claude-sonnet-5-5"
    assert anthropic.resolved_voice_effort == "low"


def test_voice_model_and_effort_override_and_blank_values(settings):
    custom = with_voice(settings, voice_model="gemini-3.8-flash", voice_effort="medium")
    assert custom.resolved_voice_model == "gemini-3.8-flash"
    assert custom.resolved_voice_effort == "medium"
    blank = with_voice(settings, voice_model="", voice_effort="")
    assert blank.resolved_voice_model == "gemini-3.5-flash-lite"
    assert blank.resolved_voice_effort == "minimal"


async def test_gemini_factory_builds_openai_compatible_agent(settings, approval):
    client, completions = fake_openai([text_chunk("Hello."), finish_chunk("stop")])

    agent = make_agent_factory(settings, client=client)(approval)
    await agent.run_turn(_ignore)

    assert isinstance(agent, OpenAICompatibleVoiceAgent)
    assert completions.requests[0]["model"] == "gemini-3.5-flash-lite"
    assert completions.requests[0]["reasoning_effort"] == "minimal"


async def test_anthropic_factory_builds_claude_agent(settings, approval):
    messages = SimpleNamespace(requests=[])

    def stream(**kwargs):
        messages.requests.append(kwargs)
        raise RuntimeError("stop after capturing the request")

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    agent = make_agent_factory(with_voice(settings, voice_provider="anthropic"), client=client)(
        approval
    )

    assert isinstance(agent, ClaudeVoiceAgent)
    with pytest.raises(RuntimeError, match="stop after capturing"):
        await agent.run_turn(_ignore)
    assert messages.requests[0]["model"] == "claude-sonnet-5-5"
    assert messages.requests[0]["output_config"] == {"effort": "low"}


def test_gemini_client_uses_google_openai_endpoint(settings):
    client = make_client(with_voice(settings, gemini_api_key=SecretStr("gemini-test-key")))

    assert str(client.base_url).startswith(
        "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    assert client.api_key == "gemini-test-key"


def test_missing_credentials_names_the_key_for_the_provider(settings):
    assert "GEMINI_API_KEY" in missing_credentials(settings)
    assert missing_credentials(with_voice(settings, gemini_api_key=SecretStr("k"))) is None
    anthropic = with_voice(settings, voice_provider="anthropic")
    assert "ANTHROPIC_API_KEY" in missing_credentials(anthropic)
    assert missing_credentials(with_voice(anthropic, anthropic_api_key=SecretStr("k"))) is None


async def _ignore(_token):
    pass
