"""Build controller-owned trusted state from authoritative Supabase records."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from .models import InventoryRecord, ProductState, Supplier, SupplierOffer, TrustedStateSnapshot


class TrustedStateSource(Protocol):
    def suppliers(self) -> list[Supplier]: ...

    def offers(self) -> list[SupplierOffer]: ...

    def inventory_records(self) -> list[InventoryRecord]: ...

    def incoming_quantities(self) -> dict[str, int]: ...

    def spent_today(self, now: datetime) -> Decimal: ...

    def existing_idempotency_keys(self) -> set[str]: ...


def load_trusted_state(
    source: TrustedStateSource,
    *,
    now: datetime,
    allowed_delivery_address_ids: frozenset[str] = frozenset({"warehouse-main"}),
) -> TrustedStateSnapshot:
    """Load state from the source; no action-controlled value is trusted here.

    `now` is supplied by the caller, matching the controller's deterministic
    clock contract. The database supplies product, offer, supplier, spending,
    and idempotency facts. Denied fingerprints remain empty until the audit
    writer persists a canonical fingerprint alongside each denied attempt.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    incoming_by_product = source.incoming_quantities()
    products = tuple(
        ProductState(
            product_id=record.product_id,
            active=record.active,
            quantity_on_hand=record.quantity_on_hand,
            quantity_reserved=record.quantity_reserved,
            incoming_quantity=incoming_by_product.get(record.product_id, 0),
            target_stock=record.target_stock,
        )
        for record in sorted(source.inventory_records(), key=lambda item: item.product_id)
    )
    return TrustedStateSnapshot(
        suppliers=tuple(sorted(source.suppliers(), key=lambda item: item.supplier_id)),
        products=products,
        offers=tuple(sorted(source.offers(), key=lambda item: item.supplier_product_id)),
        spent_today=source.spent_today(now.astimezone(UTC)),
        existing_idempotency_keys=frozenset(source.existing_idempotency_keys()),
        allowed_delivery_address_ids=allowed_delivery_address_ids,
        denied_action_fingerprints=frozenset(),
    )
