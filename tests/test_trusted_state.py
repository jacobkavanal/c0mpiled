from datetime import UTC, datetime
from decimal import Decimal

import pytest

from purchasing_agent.models import InventoryRecord, Supplier, SupplierOffer
from purchasing_agent.trusted_state import load_trusted_state


class FakeSource:
    def suppliers(self):
        return [Supplier("SUP-2", "pending"), Supplier("SUP-1", "approved")]

    def offers(self):
        return [
            SupplierOffer(
                supplier_product_id="OFFER-1",
                product_id="PRD-1",
                supplier_id="SUP-1",
                unit_price=Decimal("4.25"),
                currency="USD",
                minimum_order_quantity=1,
                order_multiple=1,
                lead_time_days=2,
                available_quantity=30,
                is_preferred=True,
                price_updated_at="2026-10-24T00:00:00+00:00",
            )
        ]

    def inventory_records(self):
        return [
            InventoryRecord("PRD-2", False, 1, 0, 5),
            InventoryRecord("PRD-1", True, 10, 2, 30),
        ]

    def incoming_quantities(self):
        return {"PRD-1": 7}

    def spent_today(self, now):
        assert now.tzinfo is UTC
        return Decimal("88.10")

    def existing_idempotency_keys(self):
        return {"key-b", "key-a"}

    def denied_action_fingerprints(self):
        return {"fingerprint-b", "fingerprint-a"}


def test_loads_sorted_authoritative_state_in_controller_shape() -> None:
    state = load_trusted_state(
        FakeSource(),
        now=datetime(2026, 10, 24, 9, tzinfo=UTC),
        allowed_delivery_address_ids=frozenset({"warehouse-b", "warehouse-a"}),
    )
    payload = state.to_controller_payload()

    assert list(payload["suppliers"]) == ["SUP-1", "SUP-2"]
    assert payload["products"]["PRD-1"]["incoming_quantity"] == 7
    assert payload["products"]["PRD-2"]["active"] is False
    assert payload["spent_today"] == "88.10"
    assert payload["existing_idempotency_keys"] == ["key-a", "key-b"]
    assert payload["denied_action_fingerprints"] == ["fingerprint-a", "fingerprint-b"]


def test_rejects_naive_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        load_trusted_state(FakeSource(), now=datetime(2026, 10, 24))
