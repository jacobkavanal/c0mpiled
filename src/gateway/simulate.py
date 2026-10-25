"""Talk to the voice agent in the terminal, playing the approver.

Runs the real voice agent with the duplicate-customers example request; no Twilio or
database needed. Reads VOICE_PROVIDER (gemini by default), its API key (GEMINI_API_KEY
or ANTHROPIC_API_KEY), and optionally VOICE_MODEL / VOICE_EFFORT, from the
environment or from .env.

    uv run python -m gateway.simulate
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .config import VoiceSettings
from .models import (
    AgentClaims,
    ApprovalRequest,
    ApprovalStatus,
    RiskLevel,
    VerifiedFacts,
)
from .voice_providers import make_agent_factory, missing_credentials

# Only the voice settings, so .env needs nothing else filled in.
SimulatorSettings = VoiceSettings


def require_credentials(settings: VoiceSettings) -> None:
    problem = missing_credentials(settings)
    if problem:
        print(problem, file=sys.stderr)
        raise SystemExit(1)


def example_approval() -> ApprovalRequest:
    now = datetime.now(UTC)
    return ApprovalRequest(
        id=uuid4(),
        tenant_id="demo",
        approver_id=uuid4(),
        status=ApprovalStatus.CALLING,
        risk_level=RiskLevel.CRITICAL,
        summary="Delete 200 rows from the production customers table",
        facts=VerifiedFacts(
            tool_name="execute_sql",
            mcp_server="prod-postgres",
            arguments={"sql": "DELETE FROM customers WHERE id = ANY($1)", "row_count": 200},
            risk_reasons=[
                "DELETE on a production database",
                "Not reversible without a backup restore",
            ],
            observations=["Dry run matched 200 rows in customers"],
        ),
        claims=AgentClaims(
            agent_name="billing-cleanup-agent",
            stated_objective="Remove duplicate customer entries",
        ),
        expires_at=now + timedelta(minutes=5),
        created_at=now,
    )


async def main() -> None:
    settings = SimulatorSettings()
    require_credentials(settings)
    agent = make_agent_factory(settings)(example_approval())
    print(
        f"[{settings.voice_provider}: {settings.resolved_voice_model}, effort "
        f"{settings.resolved_voice_effort}] You are the approver. Ctrl+C to hang up."
    )
    if settings.voice_provider == "gemini":
        print("[Gemini's free tier may use this conversation to improve Google's products.]")
    print()

    async def speak(token: str) -> None:
        sys.stdout.write(token)
        sys.stdout.flush()

    agent.add_call_event(
        "The call is connected and the approver is listening. Give your opening explanation."
    )
    while True:
        sys.stdout.write("Agent: ")
        outcome = await agent.run_turn(speak)
        print()
        if outcome is not None:
            print(
                f"\n[call ended: {outcome.kind}"
                + (f", reason: {outcome.reason}]" if outcome.reason else "]")
            )
            return
        text = await asyncio.to_thread(input, "You: ")
        agent.add_user_message(text)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print("\n[hung up]")
