import asyncio
from typing import Protocol

from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from .config import Settings
from .models import ApprovalRequest, ApprovalStatus, Approver

# Twilio AnsweredBy values that mean nobody is on the line to hear request details.
NOT_A_PERSON = (
    "machine_start",
    "machine_end_beep",
    "machine_end_silence",
    "machine_end_other",
    "fax",
)

_CLOSING_LINES = {
    ApprovalStatus.APPROVED: (
        "Your approval has been recorded. The action will now proceed. Goodbye."
    ),
    ApprovalStatus.DENIED: "The request has been denied, and the action will not run. Goodbye.",
    ApprovalStatus.EXPIRED: (
        "This request expired before a decision was recorded, so the action will not run. Goodbye."
    ),
}
_NO_DECISION_LINE = "No decision was recorded, so the action will not run. Goodbye."


class Telephony(Protocol):
    async def place_call(self, approval: ApprovalRequest, approver: Approver) -> str: ...

    async def hang_up(self, call_sid: str) -> None: ...


class TwilioTelephony:
    def __init__(self, settings: Settings, client: Client | None = None) -> None:
        self._settings = settings
        self._client = client or Client(
            settings.twilio_account_sid, settings.twilio_auth_token.get_secret_value()
        )

    async def place_call(self, approval: ApprovalRequest, approver: Approver) -> str:
        base = self._settings.public_base_url.rstrip("/")
        # Synchronous SDK; keep it off the event loop.
        call = await asyncio.to_thread(
            self._client.calls.create,
            to=approver.phone_e164,
            from_=self._settings.twilio_from_number,
            url=f"{base}/twilio/voice/{approval.id}",
            method="POST",
            status_callback=f"{base}/twilio/status/{approval.id}",
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            # Synchronous answering-machine detection: the voice webhook receives
            # AnsweredBy, so details are never left on a voicemail.
            machine_detection="Enable",
            timeout=self._settings.ring_timeout_seconds,
            time_limit=self._settings.max_call_seconds,
        )
        return call.sid

    async def hang_up(self, call_sid: str) -> None:
        await asyncio.to_thread(self._client.calls(call_sid).update, status="completed")


def relay_twiml(
    settings: Settings, approval: ApprovalRequest, approver: Approver, call_token: str
) -> str:
    base = settings.public_base_url.rstrip("/")
    response = VoiceResponse()
    # When the relay session ends, Twilio fetches the action URL for the closing line.
    connect = Connect(action=f"{base}/twilio/relay-ended/{approval.id}", method="POST")
    relay_options: dict[str, str | bool] = {
        "url": f"{settings.public_ws_base_url}/twilio/relay",
        "welcome_greeting": settings.welcome_greeting,
        "language": settings.call_language,
    }
    if approver.pin_hash:
        relay_options["dtmf_detection"] = True
    if settings.tts_voice:
        relay_options["voice"] = settings.tts_voice
    relay = connect.conversation_relay(**relay_options)
    # Custom parameters reach our WebSocket in the setup message, never the caller.
    relay.parameter(name="token", value=call_token)
    response.append(connect)
    response.hangup()
    return str(response)


def closing_twiml(approval: ApprovalRequest | None) -> str:
    line = _CLOSING_LINES.get(approval.status, _NO_DECISION_LINE) if approval else _NO_DECISION_LINE
    response = VoiceResponse()
    response.say(line)
    response.hangup()
    return str(response)


def hangup_twiml() -> str:
    response = VoiceResponse()
    response.hangup()
    return str(response)
