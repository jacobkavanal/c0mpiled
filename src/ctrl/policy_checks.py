"""Deterministic hard-rule checks for CTRL.

Every check is a pure function: (action, trusted state, policy) -> list of
Findings. No I/O, no clock, no randomness, no LLM. `run_policy_checks` runs all
of them and returns every finding in a fixed order.

Not in this module yet: pending-supplier, substitution, stale-price and
missing-data escalations, approval validation, and the final verdict.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable

from .approvals import action_fingerprint
from .models import (
    Finding,
    OfferState,
    PolicyConfig,
    ProductState,
    ProposedAction,
    ProposedItem,
    TrustedState,
    make_finding,
)
from .reason_codes import REASON_ORDER, ReasonCode as R

Check = Callable[[ProposedAction, TrustedState, PolicyConfig], list[Finding]]

CENT = Decimal("0.01")


# ------------------------------------------------------------- helpers
def money(value: Decimal) -> Decimal:
    """Quantize to cents (half-up)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def inventory_position(product: ProductState) -> int:
    return product.quantity_on_hand - product.quantity_reserved + product.incoming_quantity


def replenishment_need(product: ProductState) -> int:
    return max(product.target_stock - inventory_position(product), 0)


EXACT, WITHIN_BAND, BEYOND_BAND = "exact", "within_band", "beyond_band"


def price_band(proposed: Decimal, trusted: Decimal, policy: PolicyConfig) -> str:
    """Compare a proposed unit price with the trusted price.

    exact       -> identical
    within_band -> differs by at most price_change_escalation_percent
    beyond_band -> differs by more (or trusted price is not positive)
    """
    if proposed == trusted:
        return EXACT
    if trusted <= 0:
        return BEYOND_BAND
    if abs(proposed - trusted) / trusted <= policy.price_change_escalation_percent:
        return WITHIN_BAND
    return BEYOND_BAND


def effective_unit_price(
    item: ProposedItem, offer: OfferState | None, policy: PolicyConfig
) -> Decimal:
    """Price used when recalculating totals.

    A small change inside the escalation band uses the proposed price (that is
    the price a human would be approving). Anything else uses the trusted price.
    """
    if offer is None:
        return item.unit_price
    if price_band(item.unit_price, offer.unit_price, policy) == WITHIN_BAND:
        return item.unit_price
    return offer.unit_price


def calculate_total(action: ProposedAction, state: TrustedState, policy: PolicyConfig) -> Decimal:
    """Recalculated order total: items + shipping + tax. Never trusts stated_total."""
    items_total = sum(
        (
            money(item.quantity * effective_unit_price(item, state.offers.get(item.supplier_product_id), policy))
            for item in action.items
        ),
        Decimal("0"),
    )
    return money(items_total + money(action.shipping_cost) + money(action.tax_amount))


# --------------------------------------------------------------- STOP
def check_retry_and_repeats(action, state, policy):
    findings = []
    if action.retry_number >= policy.maximum_retries:
        findings.append(
            make_finding(R.RETRY_LIMIT_REACHED, f"retry {action.retry_number} of max {policy.maximum_retries}")
        )
    if action_fingerprint(action) in state.denied_action_fingerprints:
        findings.append(make_finding(R.REPEATED_DENIED_ACTION))
    return findings


# -------------------------------------------------------------- BLOCK
def check_tool_and_operation(action, state, policy):
    findings = []
    if action.tool_name not in policy.allowed_tools:
        findings.append(make_finding(R.TOOL_NOT_ALLOWED, action.tool_name))
    if action.operation not in policy.allowed_operations:
        findings.append(make_finding(R.OPERATION_NOT_ALLOWED, action.operation))
    return findings


def check_subscription(action, state, policy):
    if action.is_subscription and not policy.subscriptions_allowed:
        return [make_finding(R.SUBSCRIPTION_PROHIBITED)]
    return []


def check_supplier(action, state, policy):
    supplier = state.suppliers.get(action.supplier_id)
    if supplier is None:
        return [make_finding(R.SUPPLIER_UNKNOWN, action.supplier_id)]
    status = supplier.approval_status
    if status == "blocked":
        return [make_finding(R.SUPPLIER_BLOCKED, action.supplier_id)]
    if status == "suspended":
        return [make_finding(R.SUPPLIER_SUSPENDED, action.supplier_id)]
    if status in ("approved", "pending"):
        # "pending" becomes an ESCALATE finding in the escalation step.
        return []
    # Fail closed on any status we do not recognise.
    return [make_finding(R.SUPPLIER_BLOCKED, f"{action.supplier_id}: unknown status '{status}'")]


def check_currency(action, state, policy):
    if action.currency not in policy.allowed_currencies:
        return [make_finding(R.CURRENCY_NOT_ALLOWED, action.currency)]
    return []


def check_line_items(action, state, policy):
    """Product, offer, quantity, minimum, multiple, availability, offer currency."""
    findings = []
    if not action.items:
        return [make_finding(R.INVALID_QUANTITY, "order has no items")]

    for item in action.items:
        label = f"{item.product_id}/{item.supplier_product_id}"

        product = state.products.get(item.product_id)
        if product is None:
            findings.append(make_finding(R.PRODUCT_UNKNOWN, item.product_id))
        elif not product.active:
            findings.append(make_finding(R.PRODUCT_INACTIVE, item.product_id))

        offer = state.offers.get(item.supplier_product_id)
        if offer is None:
            findings.append(make_finding(R.OFFER_UNKNOWN, item.supplier_product_id))
        else:
            if offer.product_id != item.product_id:
                findings.append(make_finding(R.OFFER_PRODUCT_MISMATCH, label))
            if offer.supplier_id != action.supplier_id:
                findings.append(make_finding(R.OFFER_SUPPLIER_MISMATCH, label))
            if offer.currency != action.currency:
                findings.append(make_finding(R.OFFER_CURRENCY_MISMATCH, label))

        if item.quantity <= 0:
            findings.append(make_finding(R.INVALID_QUANTITY, f"{item.product_id}: {item.quantity}"))
        elif offer is not None:
            if item.quantity < offer.minimum_order_quantity:
                findings.append(
                    make_finding(
                        R.BELOW_MINIMUM_ORDER_QUANTITY,
                        f"{item.product_id}: {item.quantity} < {offer.minimum_order_quantity}",
                    )
                )
            if offer.order_multiple > 0 and item.quantity % offer.order_multiple != 0:
                findings.append(
                    make_finding(
                        R.INVALID_ORDER_MULTIPLE,
                        f"{item.product_id}: {item.quantity} not a multiple of {offer.order_multiple}",
                    )
                )
            if offer.available_quantity is not None and item.quantity > offer.available_quantity:
                findings.append(
                    make_finding(
                        R.SUPPLIER_STOCK_INSUFFICIENT,
                        f"{item.product_id}: {item.quantity} > {offer.available_quantity}",
                    )
                )
    return findings


def check_unit_prices(action, state, policy):
    """One unambiguous price rule (see handoff):
    exact -> fine; within band -> ESCALATE PRICE_CHANGED; beyond -> BLOCK.
    """
    findings = []
    for item in action.items:
        offer = state.offers.get(item.supplier_product_id)
        if offer is None:
            continue
        band = price_band(item.unit_price, offer.unit_price, policy)
        detail = f"{item.product_id}: proposed {item.unit_price}, trusted {offer.unit_price}"
        if band == WITHIN_BAND:
            findings.append(make_finding(R.PRICE_CHANGED, detail))
        elif band == BEYOND_BAND:
            findings.append(make_finding(R.UNIT_PRICE_MISMATCH, detail))
    return findings


def check_totals_and_limits(action, state, policy):
    findings = []
    total = calculate_total(action, state, policy)

    if money(action.stated_total) != total:
        findings.append(make_finding(R.TOTAL_MISMATCH, f"stated {money(action.stated_total)}, calculated {total}"))
    if total > policy.maximum_order_total:
        findings.append(make_finding(R.ORDER_LIMIT_EXCEEDED, f"{total} > {policy.maximum_order_total}"))
    if state.spent_today + total > policy.maximum_daily_spend:
        findings.append(
            make_finding(
                R.DAILY_LIMIT_EXCEEDED,
                f"{state.spent_today} + {total} > {policy.maximum_daily_spend}",
            )
        )
    return findings


def check_replenishment(action, state, policy):
    """Requested quantity (summed per product) must not exceed need + tolerance."""
    requested: dict[str, int] = defaultdict(int)
    for item in action.items:
        requested[item.product_id] += item.quantity

    findings = []
    for product_id in sorted(requested):
        product = state.products.get(product_id)
        if product is None:
            continue  # PRODUCT_UNKNOWN already reported
        need = replenishment_need(product)
        allowed = need + policy.maximum_replenishment_overage_units
        if requested[product_id] > allowed:
            findings.append(
                make_finding(
                    R.EXCESS_REPLENISHMENT,
                    f"{product_id}: requested {requested[product_id]}, need {need}, allowed {allowed}",
                )
            )
    return findings


def check_delivery_address(action, state, policy):
    if action.delivery_address_id not in state.allowed_delivery_address_ids:
        return [make_finding(R.DELIVERY_ADDRESS_NOT_ALLOWED, action.delivery_address_id)]
    return []


def check_idempotency(action, state, policy):
    if action.idempotency_key in state.existing_idempotency_keys:
        return [make_finding(R.DUPLICATE_IDEMPOTENCY_KEY, action.idempotency_key)]
    return []


# ---------------------------------------------------------------- run
ALL_CHECKS: tuple[Check, ...] = (
    check_retry_and_repeats,
    check_tool_and_operation,
    check_subscription,
    check_supplier,
    check_currency,
    check_line_items,
    check_unit_prices,
    check_totals_and_limits,
    check_replenishment,
    check_delivery_address,
    check_idempotency,
)


def run_policy_checks(
    action: ProposedAction, state: TrustedState, policy: PolicyConfig
) -> tuple[Finding, ...]:
    """Run every check and return all findings in a fixed, deterministic order."""
    findings: list[Finding] = []
    for check in ALL_CHECKS:
        findings.extend(check(action, state, policy))
    return tuple(sorted(findings, key=lambda f: (REASON_ORDER[f.reason_code], f.message)))
