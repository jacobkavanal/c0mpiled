"""Small Supabase REST reader. It only reads trusted inventory/catalog state."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import ReplenishmentNeed, Supplier, SupplierOffer


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
