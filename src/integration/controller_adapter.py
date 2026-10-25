"""Bridge agent data to CTRL and optionally persist its audit decision."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from ctrl.controller import evaluate
from ctrl.models import (
    Approval,
    OfferState,
    PolicyConfig,
    ProductState,
    ProposedAction,
    ProposedItem,
    SupplierState,
    TrustedState,
)
from ctrl.reason_codes import Outcome
from purchasing_agent.models import ProposedAction as AgentProposedAction
from purchasing_agent.models import TrustedStateSnapshot


def to_controller_action(action: AgentProposedAction) -> ProposedAction:
    """Convert the agent proposal; the agent cannot supply trusted state."""
    return ProposedAction(
        action_id=action.action_id,
        agent_run_id=action.agent_run_id,
        tool_name=action.tool_name,
        operation=action.operation,
        supplier_id=action.supplier_id,
        items=tuple(
            ProposedItem(
                product_id=item.product_id,
                supplier_product_id=item.supplier_product_id,
                quantity=item.quantity,
                unit_price=item.unit_price,
            )
            for item in action.items
        ),
        currency=action.currency,
        shipping_cost=action.shipping_cost,
        tax_amount=action.tax_amount,
        stated_total=action.stated_total,
        delivery_address_id=action.delivery_address_id,
        idempotency_key=action.idempotency_key,
        is_subscription=action.is_subscription,
        retry_number=action.retry_number,
        replaces_product_id=action.replaces_product_id,
        original_approved_action_hash=action.original_approved_action_hash,
    )


def to_controller_state(snapshot: TrustedStateSnapshot) -> TrustedState:
    return TrustedState(
        suppliers={
            supplier.supplier_id: SupplierState(
                supplier_id=supplier.supplier_id,
                approval_status=supplier.approval_status,
            )
            for supplier in snapshot.suppliers
        },
        products={
            product.product_id: ProductState(
                product_id=product.product_id,
                active=product.active,
                quantity_on_hand=product.quantity_on_hand,
                quantity_reserved=product.quantity_reserved,
                incoming_quantity=product.incoming_quantity,
                target_stock=product.target_stock,
            )
            for product in snapshot.products
        },
        offers={
            offer.supplier_product_id: OfferState(
                supplier_product_id=offer.supplier_product_id,
                product_id=offer.product_id,
                supplier_id=offer.supplier_id,
                unit_price=offer.unit_price,
                currency=offer.currency,
                minimum_order_quantity=offer.minimum_order_quantity,
                order_multiple=offer.order_multiple,
                available_quantity=offer.available_quantity,
                price_updated_at=datetime.fromisoformat(offer.price_updated_at),
            )
            for offer in snapshot.offers
        },
        spent_today=snapshot.spent_today,
        existing_idempotency_keys=snapshot.existing_idempotency_keys,
        allowed_delivery_address_ids=snapshot.allowed_delivery_address_ids,
        denied_action_fingerprints=snapshot.denied_action_fingerprints,
    )


def decide(
    *,
    proposal: AgentProposedAction,
    snapshot: TrustedStateSnapshot,
    policy: PolicyConfig,
    now: datetime,
    approval: Approval | None = None,
):
    """Call the controller after all trusted state has been freshly loaded."""
    return evaluate(
        action=to_controller_action(proposal),
        state=to_controller_state(snapshot),
        policy=policy,
        approval=approval,
        now=now,
    )


def audit_record(*, proposal: AgentProposedAction, decision, policy_version: str) -> dict[str, object]:
    """Return a schema-compatible, append-only purchase_attempts row."""
    database_decision = {
        Outcome.ALLOW: "allow",
        Outcome.ESCALATE: "require_approval",
        Outcome.BLOCK: "block",
        Outcome.STOP: "block",
    }[decision.outcome]
    return {
        "attempt_id": f"ATT-{uuid4().hex.upper()}",
        "agent_run_id": proposal.agent_run_id,
        "mcp_server": "ctrl-controller",
        "tool_name": proposal.tool_name,
        "tool_arguments": proposal.to_dict(),
        "decision": database_decision,
        "violated_rules": [code.value for code in decision.reason_codes],
        "decision_reason": decision.explanation,
        "execution_status": "not_executed",
        "policy_version": policy_version,
    }
