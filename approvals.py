"""Approval helpers for CTRL.

Currently holds the canonical action fingerprint, which the hard-rule checks
need (to spot a repeated denied action). Scoped approval validation will be
added here next.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from .models import ProposedAction


def _dec(value: Decimal) -> str:
    """Stable text for a Decimal: 6.5 and 6.50 hash identically."""
    return format(value.normalize(), "f")


def action_fingerprint(action: ProposedAction) -> str:
    """SHA-256 of the security-relevant fields of an action.

    Included: tool, operation, supplier, items (product, offer, quantity,
    unit price), currency, shipping, tax, delivery address, subscription flag.
    Excluded on purpose: ids, retry count, stated total, descriptive text.
    """
    items = sorted(
        (
            {
                "product_id": item.product_id,
                "supplier_product_id": item.supplier_product_id,
                "quantity": item.quantity,
                "unit_price": _dec(item.unit_price),
            }
            for item in action.items
        ),
        key=lambda d: (d["product_id"], d["supplier_product_id"], d["quantity"], d["unit_price"]),
    )
    payload = {
        "tool": action.tool_name,
        "operation": action.operation,
        "supplier_id": action.supplier_id,
        "items": items,
        "currency": action.currency,
        "shipping_cost": _dec(action.shipping_cost),
        "tax_amount": _dec(action.tax_amount),
        "delivery_address_id": action.delivery_address_id,
        "is_subscription": action.is_subscription,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
