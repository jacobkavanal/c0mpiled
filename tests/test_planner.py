from decimal import Decimal

from purchasing_agent.models import ReplenishmentNeed, Supplier, SupplierOffer
from purchasing_agent.planner import build_replenishment_proposals


def offer(
    supplier_product_id: str,
    supplier_id: str,
    price: str,
    *,
    preferred: bool = False,
    minimum: int = 1,
    multiple: int = 1,
) -> SupplierOffer:
    return SupplierOffer(
        supplier_product_id=supplier_product_id,
        product_id="PRD-001",
        supplier_id=supplier_id,
        unit_price=Decimal(price),
        currency="USD",
        minimum_order_quantity=minimum,
        order_multiple=multiple,
        lead_time_days=3,
        available_quantity=100,
        is_preferred=preferred,
    )


def need(preferred_supplier_id: str | None = None) -> ReplenishmentNeed:
    return ReplenishmentNeed(
        product_id="PRD-001",
        sku="PAPER-A4",
        name="A4 Paper",
        inventory_position=5,
        reorder_point=10,
        target_stock=25,
        proposed_replenishment_quantity=20,
        preferred_supplier_id=preferred_supplier_id,
    )


def test_chooses_product_preferred_supplier_before_cheapest_offer() -> None:
    proposals = build_replenishment_proposals(
        agent_run_id="run-1",
        needs=[need(preferred_supplier_id="SUP-2")],
        suppliers=[Supplier("SUP-1", "approved"), Supplier("SUP-2", "approved")],
        offers=[offer("OFFER-1", "SUP-1", "3.00"), offer("OFFER-2", "SUP-2", "4.00")],
    )

    assert proposals[0].supplier_id == "SUP-2"
    assert proposals[0].stated_total == Decimal("80.00")


def test_rounds_to_supplier_minimum_and_multiple() -> None:
    proposals = build_replenishment_proposals(
        agent_run_id="run-1",
        needs=[need()],
        suppliers=[Supplier("SUP-1", "approved")],
        offers=[offer("OFFER-1", "SUP-1", "3.00", minimum=24, multiple=12)],
    )

    assert proposals[0].items[0].quantity == 24


def test_skips_blocked_supplier() -> None:
    proposals = build_replenishment_proposals(
        agent_run_id="run-1",
        needs=[need()],
        suppliers=[Supplier("SUP-1", "blocked")],
        offers=[offer("OFFER-1", "SUP-1", "3.00")],
    )

    assert proposals == []


def test_emits_controller_wire_contract_with_stable_idempotency_key() -> None:
    kwargs = dict(
        agent_run_id="run-1",
        needs=[need()],
        suppliers=[Supplier("SUP-1", "approved")],
        offers=[offer("OFFER-1", "SUP-1", "3.00")],
    )
    first = build_replenishment_proposals(**kwargs)[0].to_dict()
    second = build_replenishment_proposals(**kwargs)[0].to_dict()

    assert first == second
    assert first["items"] == [
        {"product_id": "PRD-001", "supplier_product_id": "OFFER-1", "quantity": 20, "unit_price": "3.00"}
    ]
    assert first["idempotency_key"].startswith("ctrl:")
