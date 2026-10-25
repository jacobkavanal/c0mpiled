"""Pure, deterministic replenishment proposal planner.

This module deliberately has no database, controller, or purchase-execution
dependency. It proposes an order; CTRL's policy engine remains the only
component that may allow an execution adapter to purchase anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from .models import (
    ProposedAction,
    ProposedItem,
    ReplenishmentNeed,
    Supplier,
    SupplierOffer,
)


@dataclass(frozen=True)
class PlannerConfig:
    tool_name: str = "square.make_api_request"
    operation: str = "create_order"
    currency: str = "USD"
    delivery_address_id: str = "warehouse-main"
    shipping_cost: Decimal = Decimal("0.00")
    tax_amount: Decimal = Decimal("0.00")


def build_replenishment_proposals(
    *,
    agent_run_id: str,
    needs: list[ReplenishmentNeed],
    suppliers: list[Supplier],
    offers: list[SupplierOffer],
    config: PlannerConfig = PlannerConfig(),
) -> list[ProposedAction]:
    """Return at most one normalized proposal per low-stock product.

    Selection is stable: prefer the product's preferred supplier, then a
    preferred offer, then the lower price, faster delivery, and a stable ID.
    Blocked and suspended suppliers are excluded from *planning*; the
    controller still independently validates all submitted actions.
    """
    supplier_by_id = {supplier.supplier_id: supplier for supplier in suppliers}
    proposals: list[ProposedAction] = []

    for need in sorted(needs, key=lambda item: item.product_id):
        if need.proposed_replenishment_quantity <= 0:
            continue

        offer = _select_offer(
            need=need,
            offers=offers,
            supplier_by_id=supplier_by_id,
            currency=config.currency,
        )
        if offer is None:
            continue

        quantity = _adjust_quantity(
            requested=need.proposed_replenishment_quantity,
            minimum=offer.minimum_order_quantity,
            multiple=offer.order_multiple,
        )
        if offer.available_quantity is not None and quantity > offer.available_quantity:
            continue

        item = ProposedItem(
            product_id=need.product_id,
            supplier_product_id=offer.supplier_product_id,
            quantity=quantity,
            unit_price=offer.unit_price,
        )
        total = (Decimal(quantity) * offer.unit_price) + config.shipping_cost + config.tax_amount
        stable_key = ":".join(
            (agent_run_id, need.product_id, offer.supplier_product_id, str(quantity))
        )
        action_id = str(uuid5(NAMESPACE_URL, f"ctrl-action:{stable_key}"))
        proposals.append(
            ProposedAction(
                action_id=action_id,
                agent_run_id=agent_run_id,
                tool_name=config.tool_name,
                operation=config.operation,
                supplier_id=offer.supplier_id,
                items=(item,),
                currency=config.currency,
                shipping_cost=config.shipping_cost,
                tax_amount=config.tax_amount,
                stated_total=total,
                delivery_address_id=config.delivery_address_id,
                idempotency_key=f"ctrl:{action_id}",
            )
        )
    return proposals


def _select_offer(
    *,
    need: ReplenishmentNeed,
    offers: list[SupplierOffer],
    supplier_by_id: dict[str, Supplier],
    currency: str,
) -> SupplierOffer | None:
    candidates = [
        offer
        for offer in offers
        if offer.product_id == need.product_id
        and offer.currency == currency
        and supplier_by_id.get(offer.supplier_id, Supplier("", "blocked")).approval_status
        not in {"blocked", "suspended"}
    ]
    if not candidates:
        return None

    return min(
        candidates,
        key=lambda offer: (
            offer.supplier_id != need.preferred_supplier_id,
            not offer.is_preferred,
            offer.unit_price,
            offer.lead_time_days,
            offer.supplier_product_id,
        ),
    )


def _adjust_quantity(*, requested: int, minimum: int, multiple: int) -> int:
    """Meet supplier MOQ/multiples, leaving excess for controller policy to judge."""
    quantity = max(requested, minimum)
    return ((quantity + multiple - 1) // multiple) * multiple
