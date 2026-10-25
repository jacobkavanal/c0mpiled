from datetime import UTC, datetime
from decimal import Decimal

from ctrl.models import PolicyConfig
from ctrl.reason_codes import Outcome
from integration.controller_adapter import audit_record, decide, to_controller_state
from purchasing_agent.models import (
    ProductState,
    ProposedAction,
    ProposedItem,
    Supplier,
    SupplierOffer,
    TrustedStateSnapshot,
)

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


def snapshot() -> TrustedStateSnapshot:
    return TrustedStateSnapshot(
        suppliers=(Supplier("SUP-1", "approved"),),
        products=(ProductState("PRD-1", True, 0, 0, 0, 10),),
        offers=(
            SupplierOffer(
                supplier_product_id="OFFER-1", product_id="PRD-1", supplier_id="SUP-1",
                unit_price=Decimal("5.00"), currency="USD", minimum_order_quantity=1,
                order_multiple=1, lead_time_days=1, available_quantity=20, is_preferred=True,
                price_updated_at=NOW.isoformat(),
            ),
        ),
        spent_today=Decimal("0.00"), existing_idempotency_keys=frozenset(),
        allowed_delivery_address_ids=frozenset({"warehouse-main"}),
        denied_action_fingerprints=frozenset(),
    )


def proposal() -> ProposedAction:
    return ProposedAction(
        action_id="ACT-1", agent_run_id="RUN-1", tool_name="square.make_api_request",
        operation="create_order", supplier_id="SUP-1",
        items=(ProposedItem("PRD-1", "OFFER-1", 10, Decimal("5.00")),), currency="USD",
        shipping_cost=Decimal("0.00"), tax_amount=Decimal("0.00"), stated_total=Decimal("50.00"),
        delivery_address_id="warehouse-main", idempotency_key="IDEMP-1",
    )


def test_adapter_evaluates_fresh_trusted_state() -> None:
    decision = decide(proposal=proposal(), snapshot=snapshot(), policy=PolicyConfig(), now=NOW)
    assert decision.outcome is Outcome.ALLOW
    assert to_controller_state(snapshot()).products["PRD-1"].target_stock == 10


def test_audit_record_is_schema_compatible_and_never_claims_execution() -> None:
    decision = decide(proposal=proposal(), snapshot=snapshot(), policy=PolicyConfig(), now=NOW)
    record = audit_record(proposal=proposal(), decision=decision, policy_version="demo-v1")
    assert record["decision"] == "allow"
    assert record["execution_status"] == "not_executed"
    assert record["tool_arguments"]["action_id"] == "ACT-1"
    assert record["attempt_id"].startswith("ATT-")
