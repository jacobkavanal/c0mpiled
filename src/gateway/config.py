from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

VoiceProvider = Literal["gemini", "groq", "anthropic"]
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Phone replies must start within about a second. Measured: gemini-3.8-flash took 43 s
# to start replying (and rejects "minimal"); gemini-3.5-flash-lite at minimal took 0.7 s.
DEFAULT_VOICE_MODELS: dict[VoiceProvider, str] = {
    "gemini": "gemini-3.5-flash-lite",
    "groq": "openai/gpt-oss-20b",
    "anthropic": "claude-sonnet-5-5",
}
# Claude's effort has no "minimal"; Gemini's reasoning_effort does.
DEFAULT_VOICE_EFFORTS: dict[VoiceProvider, str] = {
    "gemini": "minimal",
    "groq": "low",
    "anthropic": "low",
}
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


class VoiceSettings(BaseSettings):
    """Which model runs the phone conversation. Enough on its own for the simulator."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Gemini's free tier lets Google use requests to improve its products: test data
    # only. Use "anthropic", or a paid Gemini tier, for real customer requests.
    voice_provider: VoiceProvider = "gemini"
    # Blank means the provider's default from DEFAULT_VOICE_MODELS.
    voice_model: str | None = None
    # Phone turns are latency-sensitive; raise this only if answers get worse. Sent as
    # Claude's effort or Gemini's reasoning_effort. Blank means the provider's default
    # from DEFAULT_VOICE_EFFORTS. Not every Gemini model accepts every level.
    voice_effort: str | None = None

    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = GEMINI_OPENAI_BASE_URL
    groq_api_key: SecretStr | None = None
    groq_base_url: str = GROQ_BASE_URL
    anthropic_api_key: SecretStr | None = None

    @property
    def resolved_voice_model(self) -> str:
        return self.voice_model or DEFAULT_VOICE_MODELS[self.voice_provider]

    @property
    def resolved_voice_effort(self) -> str:
        return self.voice_effort or DEFAULT_VOICE_EFFORTS[self.voice_provider]


class Settings(VoiceSettings):
    # Supabase Postgres. Must be a session-mode connection (direct, or the
    # session pooler on port 5432): LISTEN/NOTIFY does not work through the
    # transaction pooler.
    database_url: SecretStr

    # Public HTTPS origin Twilio uses to reach this service, e.g.
    # https://gateway.example.com. Used for webhook URLs and signature checks.
    public_base_url: str

    # Bearer token the MCP proxy uses for the /v1/approvals API.
    internal_api_key: SecretStr
    # HMAC key for the per-call token handed to Twilio ConversationRelay.
    call_token_secret: SecretStr

    twilio_account_sid: str
    twilio_auth_token: SecretStr
    twilio_from_number: str
    twilio_validate_signatures: bool = True
    twilio_validate_websocket_signature: bool = True

    welcome_greeting: str = (
        "Hello, this is an automated approval call about an action your AI agent wants to take."
    )
    tts_voice: str | None = None
    call_language: str = "en-US"

    approval_timeout_seconds: int = 300
    ring_timeout_seconds: int = 30
    max_call_seconds: int = 600
    pin_max_attempts: int = 3

    @property
    def public_ws_base_url(self) -> str:
        base = self.public_base_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base.removeprefix("https://")
        return "ws://" + base.removeprefix("http://")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
