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
    price_updated_at: str = ""


@dataclass(frozen=True)
class InventoryRecord:
    product_id: str
    active: bool
    quantity_on_hand: int
    quantity_reserved: int
    target_stock: int


@dataclass(frozen=True)
class ProductState:
    product_id: str
    active: bool
    quantity_on_hand: int
    quantity_reserved: int
    incoming_quantity: int
    target_stock: int

    def to_dict(self) -> dict[str, object]:
        return {
            "product_id": self.product_id,
            "active": self.active,
            "quantity_on_hand": self.quantity_on_hand,
            "quantity_reserved": self.quantity_reserved,
            "incoming_quantity": self.incoming_quantity,
            "target_stock": self.target_stock,
        }


@dataclass(frozen=True)
class TrustedStateSnapshot:
    """Serialized bridge from Supabase rows to Person 1's TrustedState model."""

    suppliers: tuple[Supplier, ...]
    products: tuple[ProductState, ...]
    offers: tuple[SupplierOffer, ...]
    spent_today: Decimal
    existing_idempotency_keys: frozenset[str]
    allowed_delivery_address_ids: frozenset[str]
    denied_action_fingerprints: frozenset[str]

    def to_controller_payload(self) -> dict[str, object]:
        return {
            "suppliers": {
                supplier.supplier_id: {
                    "supplier_id": supplier.supplier_id,
                    "approval_status": supplier.approval_status,
                }
                for supplier in self.suppliers
            },
            "products": {
                product.product_id: product.to_dict() for product in self.products
            },
            "offers": {
                offer.supplier_product_id: {
                    "supplier_product_id": offer.supplier_product_id,
                    "product_id": offer.product_id,
                    "supplier_id": offer.supplier_id,
                    "unit_price": money_string(offer.unit_price),
                    "currency": offer.currency,
                    "minimum_order_quantity": offer.minimum_order_quantity,
                    "order_multiple": offer.order_multiple,
                    "available_quantity": offer.available_quantity,
                    "price_updated_at": offer.price_updated_at,
                }
                for offer in self.offers
            },
            "spent_today": money_string(self.spent_today),
            "existing_idempotency_keys": sorted(self.existing_idempotency_keys),
            "allowed_delivery_address_ids": sorted(self.allowed_delivery_address_ids),
            "denied_action_fingerprints": sorted(self.denied_action_fingerprints),
        }


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
