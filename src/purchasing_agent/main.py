"""CLI entrypoint: print proposals only; it never executes purchases."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

from .planner import build_replenishment_proposals
from .supabase_repository import SupabaseRepository
from .trusted_state import load_trusted_state


def main() -> int:
    repository = SupabaseRepository.from_environment()
    agent_run_id = f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    proposals = build_replenishment_proposals(
        agent_run_id=agent_run_id,
        needs=repository.low_stock_needs(),
        suppliers=repository.suppliers(),
        offers=repository.offers(),
    )
    state = load_trusted_state(repository, now=datetime.now(UTC))
    print(
        json.dumps(
            {
                "trusted_state": state.to_controller_payload(),
                "proposals": [proposal.to_dict() for proposal in proposals],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
