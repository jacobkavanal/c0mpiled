"""CLI entrypoint: print proposals only; it never executes purchases."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from datetime import UTC, datetime

from ctrl.models import PolicyConfig
from integration.controller_adapter import audit_record, decide
from .planner import build_replenishment_proposals
from .supabase_repository import SupabaseRepository
from .trusted_state import load_trusted_state


def main() -> int:
    parser = ArgumentParser(description="Generate, evaluate, and optionally audit CTRL proposals.")
    parser.add_argument("--record-audit", action="store_true", help="Append decisions to purchase_attempts.")
    args = parser.parse_args()
    repository = SupabaseRepository.from_environment()
    now = datetime.now(UTC)
    agent_run_id = f"run-{now.strftime('%Y%m%dT%H%M%SZ')}"
    proposals = build_replenishment_proposals(
        agent_run_id=agent_run_id,
        needs=repository.low_stock_needs(),
        suppliers=repository.suppliers(),
        offers=repository.offers(),
    )
    state = load_trusted_state(repository, now=now)
    decisions = [
        decide(proposal=proposal, snapshot=state, policy=PolicyConfig(), now=now)
        for proposal in proposals
    ]
    if args.record_audit:
        for proposal, decision in zip(proposals, decisions, strict=True):
            repository.record_purchase_attempt(
                audit_record(proposal=proposal, decision=decision, policy_version="demo-v1")
            )
    print(
        json.dumps(
            {
                "trusted_state": state.to_controller_payload(),
                "proposals": [proposal.to_dict() for proposal in proposals],
                "decisions": [decision.to_dict() for decision in decisions],
                "audit_recorded": args.record_audit,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
