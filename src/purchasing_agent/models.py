"""Typed records shared by the Supabase adapter and deterministic planner."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ReplenishmentNeed:
    product_id: str
    sku: str
    name: str
    inventory_position: int
    reorder_point: int
    target_stock: int
    proposed_replenishment_quantity: int
    preferred_supplier_id: str | None = None


@dataclass(frozen=True)
class Supplier:
    supplier_id: str
    approval_status: str


@dataclass(frozen=True)
class SupplierOffer:
    supplier_product_id: str
    product_id: str
    supplier_id: str
    unit_price: Decimal
    currency: str
    minimum_order_quantity: int
    order_multiple: int
    lead_time_days: int
    available_quantity: int | None
    is_preferred: bool


@dataclass(frozen=True)
class ProposedItem:
    product_id: str
    supplier_product_id: str
    quantity: int
    unit_price: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "product_id": self.product_id,
            "supplier_product_id": self.supplier_product_id,
            "quantity": self.quantity,
            "unit_price": money_string(self.unit_price),
        }


@dataclass(frozen=True)
class ProposedAction:
    """Wire format consumed by Person 1's controller evaluate() contract."""

    action_id: str
    agent_run_id: str
    tool_name: str
    operation: str
    supplier_id: str
    items: tuple[ProposedItem, ...]
    currency: str
    shipping_cost: Decimal
    tax_amount: Decimal
    stated_total: Decimal
    delivery_address_id: str
    idempotency_key: str
    is_subscription: bool = False
    retry_number: int = 0
    replaces_product_id: str | None = None
    original_approved_action_hash: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "agent_run_id": self.agent_run_id,
            "tool_name": self.tool_name,
            "operation": self.operation,
            "supplier_id": self.supplier_id,
            "items": [item.to_dict() for item in self.items],
            "currency": self.currency,
            "shipping_cost": money_string(self.shipping_cost),
            "tax_amount": money_string(self.tax_amount),
            "stated_total": money_string(self.stated_total),
            "delivery_address_id": self.delivery_address_id,
            "idempotency_key": self.idempotency_key,
            "is_subscription": self.is_subscription,
            "retry_number": self.retry_number,
            "replaces_product_id": self.replaces_product_id,
            "original_approved_action_hash": self.original_approved_action_hash,
        }


def money_string(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"
