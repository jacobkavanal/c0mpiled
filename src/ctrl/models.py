"""Immutable domain models for the CTRL controller.

Money is always Decimal. Timestamps are UTC-aware datetimes.
Nothing in this package performs I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .reason_codes import DEFAULT_OUTCOME, MESSAGES, Outcome, ReasonCode


# ---------------------------------------------------------------- action
@dataclass(frozen=True)
class ProposedItem:
    product_id: str
    supplier_product_id: str
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True)
class ProposedAction:
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
    is_subscription: bool
    retry_number: int
    replaces_product_id: str | None = None
    original_approved_action_hash: str | None = None


# ---------------------------------------------------------- trusted state
@dataclass(frozen=True)
class SupplierState:
    supplier_id: str
    approval_status: str  # approved, pending, suspended, blocked


@dataclass(frozen=True)
class ProductState:
    product_id: str
    active: bool
    quantity_on_hand: int
    quantity_reserved: int
    incoming_quantity: int
    target_stock: int


@dataclass(frozen=True)
class OfferState:
    supplier_product_id: str
    product_id: str
    supplier_id: str
    unit_price: Decimal
    currency: str
    minimum_order_quantity: int
    order_multiple: int
    available_quantity: int | None
    price_updated_at: datetime


@dataclass(frozen=True)
class TrustedState:
    suppliers: Mapping[str, SupplierState]
    products: Mapping[str, ProductState]
    offers: Mapping[str, OfferState]
    spent_today: Decimal
    existing_idempotency_keys: frozenset[str]
    allowed_delivery_address_ids: frozenset[str]
    denied_action_fingerprints: frozenset[str]


# ---------------------------------------------------------------- policy
@dataclass(frozen=True)
class PolicyConfig:
    allowed_tools: frozenset[str] = frozenset({"square.make_api_request"})
    allowed_operations: frozenset[str] = frozenset({"create_order"})
    allowed_currencies: frozenset[str] = frozenset({"USD"})
    maximum_order_total: Decimal = Decimal("500.00")
    maximum_daily_spend: Decimal = Decimal("1000.00")
    maximum_replenishment_overage_units: int = 0
    price_change_escalation_percent: Decimal = Decimal("0.05")
    price_stale_after_days: int = 7
    maximum_retries: int = 3
    subscriptions_allowed: bool = False


# ------------------------------------------------------ approval/results
@dataclass(frozen=True)
class Approval:
    approval_id: str
    action_fingerprint: str
    expires_at: datetime
    used: bool
    resolvable_reason_codes: frozenset[str]


@dataclass(frozen=True)
class Finding:
    outcome: Outcome  # ESCALATE, BLOCK, or STOP
    reason_code: ReasonCode
    message: str


def make_finding(code: ReasonCode, detail: str = "") -> Finding:
    """Build a Finding whose outcome and wording come from the reason code."""
    message = MESSAGES[code] if not detail else f"{MESSAGES[code]} ({detail})"
    return Finding(DEFAULT_OUTCOME[code], code, message)


@dataclass(frozen=True)
class Decision:
    outcome: Outcome  # ALLOW, ESCALATE, BLOCK, or STOP
    reason_codes: tuple[ReasonCode, ...]
    explanation: str
    action_fingerprint: str
    approval_applied: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "reason_codes": [code.value for code in self.reason_codes],
            "explanation": self.explanation,
            "action_fingerprint": self.action_fingerprint,
            "approval_applied": self.approval_applied,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Decision":
        return cls(
            outcome=Outcome(data["outcome"]),
            reason_codes=tuple(ReasonCode(c) for c in data["reason_codes"]),
            explanation=data["explanation"],
            action_fingerprint=data["action_fingerprint"],
            approval_applied=data["approval_applied"],
        )
