"""Small Supabase REST reader. It only reads trusted inventory/catalog state."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import InventoryRecord, ReplenishmentNeed, Supplier, SupplierOffer


class SupabaseRepository:
    """Read-only adapter; use a server-only service-role key in the demo backend."""

    def __init__(self, url: str, service_role_key: str) -> None:
        self._url = url.rstrip("/")
        self._key = service_role_key

    @classmethod
    def from_environment(cls) -> "SupabaseRepository":
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        return cls(url=url, service_role_key=key)

    def low_stock_needs(self) -> list[ReplenishmentNeed]:
        rows = self._get(
            "inventory_replenishment_status",
            {"select": "*", "requires_replenishment": "eq.true"},
        )
        inventory_rows = self._get(
            "inventory",
            {"select": "product_id,preferred_supplier_id"},
        )
        preferred_supplier = {
            row["product_id"]: row.get("preferred_supplier_id") for row in inventory_rows
        }
        return [
            ReplenishmentNeed(
                product_id=row["product_id"],
                sku=row["sku"],
                name=row["name"],
                inventory_position=int(row["inventory_position"]),
                reorder_point=int(row["reorder_point"]),
                target_stock=int(row["target_stock"]),
                proposed_replenishment_quantity=int(row["proposed_replenishment_quantity"]),
                preferred_supplier_id=preferred_supplier.get(row["product_id"]),
            )
            for row in rows
        ]

    def suppliers(self) -> list[Supplier]:
        return [
            Supplier(supplier_id=row["supplier_id"], approval_status=row["approval_status"])
            for row in self._get("suppliers", {"select": "supplier_id,approval_status"})
        ]

    def inventory_records(self) -> list[InventoryRecord]:
        return [
            InventoryRecord(
                product_id=row["product_id"],
                active=bool(row["active"]),
                quantity_on_hand=int(row["quantity_on_hand"]),
                quantity_reserved=int(row["quantity_reserved"]),
                target_stock=int(row["target_stock"]),
            )
            for row in self._get(
                "inventory",
                {
                    "select": (
                        "product_id,active,quantity_on_hand,quantity_reserved,target_stock"
                    )
                },
            )
        ]

    def incoming_quantities(self) -> dict[str, int]:
        rows = self._get(
            "inventory_replenishment_status",
            {"select": "product_id,incoming_quantity"},
        )
        return {row["product_id"]: int(row["incoming_quantity"]) for row in rows}

    def spent_today(self, now: datetime) -> Decimal:
        """Sum committed orders placed since UTC midnight; drafts do not count."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        utc_now = now.astimezone(UTC)
        midnight = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self._get(
            "purchase_orders",
            {
                "select": "status,total_amount,ordered_at",
                "ordered_at": f"gte.{midnight.isoformat()}",
                "status": "in.(placed,shipped,received)",
            },
        )
        return sum((Decimal(str(row["total_amount"])) for row in rows), Decimal("0"))

    def existing_idempotency_keys(self) -> set[str]:
        return {
            row["idempotency_key"]
            for row in self._get("purchase_orders", {"select": "idempotency_key"})
        }

    def offers(self) -> list[SupplierOffer]:
        return [
            SupplierOffer(
                supplier_product_id=row["supplier_product_id"],
                product_id=row["product_id"],
                supplier_id=row["supplier_id"],
                unit_price=Decimal(str(row["unit_price"])),
                currency=row["currency"],
                minimum_order_quantity=int(row["minimum_order_quantity"]),
                order_multiple=int(row["order_multiple"]),
                lead_time_days=int(row["lead_time_days"]),
                available_quantity=(
                    int(row["available_quantity"])
                    if row["available_quantity"] is not None
                    else None
                ),
                is_preferred=bool(row["is_preferred"]),
                price_updated_at=row["price_updated_at"],
            )
            for row in self._get("supplier_products", {"select": "*"})
        ]

    def _get(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        query = urlencode(params, safe=",*")
        request = Request(
            f"{self._url}/rest/v1/{table}?{query}",
            headers={"apikey": self._key, "Authorization": f"Bearer {self._key}"},
        )
        with urlopen(request, timeout=15) as response:  # noqa: S310 -- configured Supabase URL
            return json.loads(response.read().decode("utf-8"))
