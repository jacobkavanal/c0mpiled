from typing import Any

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import SecretStr

from .claude_agent import ClaudeVoiceAgent
from .config import VoiceSettings
from .models import ApprovalRequest
from .openai_compat_agent import OpenAICompatibleVoiceAgent
from .voice_agent import AgentFactory, VoiceAgent


def missing_credentials(settings: VoiceSettings) -> str | None:
    """What to set up before the configured provider can be called, or None if ready."""
    if settings.voice_provider == "gemini":
        if _has(settings.gemini_api_key):
            return None
        return (
            "No Gemini API key found. Get a free one at https://aistudio.google.com/apikey and "
            "add GEMINI_API_KEY=... to .env in the project root (not .env.example)."
        )
    if settings.voice_provider == "groq":
        if _has(settings.groq_api_key):
            return None
        return (
            "No Groq API key found. Create one at https://console.groq.com/keys and add "
            "GROQ_API_KEY=... to .env in the project root (not .env.example)."
        )
    if _has(settings.anthropic_api_key):
        return None
    return (
        "No Anthropic API key found. Add ANTHROPIC_API_KEY=sk-ant-... to .env in the project "
        "root (not .env.example), or set VOICE_PROVIDER=gemini."
    )


def make_client(settings: VoiceSettings) -> Any:
    problem = missing_credentials(settings)
    if problem:
        raise RuntimeError(problem)
    if settings.voice_provider == "gemini":
        assert settings.gemini_api_key is not None
        return AsyncOpenAI(
            api_key=settings.gemini_api_key.get_secret_value(), base_url=settings.gemini_base_url
        )
    if settings.voice_provider == "groq":
        assert settings.groq_api_key is not None
        return AsyncOpenAI(
            api_key=settings.groq_api_key.get_secret_value(), base_url=settings.groq_base_url
        )
    assert settings.anthropic_api_key is not None
    return AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())


def make_agent_factory(settings: VoiceSettings, client: Any = None) -> AgentFactory:
    client = client if client is not None else make_client(settings)
    model, effort = settings.resolved_voice_model, settings.resolved_voice_effort

    if settings.voice_provider in ("gemini", "groq"):

        def gemini_agent(approval: ApprovalRequest) -> VoiceAgent:
            return OpenAICompatibleVoiceAgent(
                client, approval, model=model, reasoning_effort=effort
            )

        return gemini_agent

    def claude_agent(approval: ApprovalRequest) -> VoiceAgent:
        return ClaudeVoiceAgent(client, approval, model=model, effort=effort)

    return claude_agent


def _has(secret: SecretStr | None) -> bool:
    return secret is not None and bool(secret.get_secret_value().strip())
