"""Tests for CTRL hard-rule checks. Fixtures mirror the real Supabase data:
PRD-001 (A4 paper) needs 40 units, OFFER-001 is $6.50, MOQ 10, multiple 5.
"""
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from ctrl.approvals import action_fingerprint
from ctrl.models import (
    Decision,
    OfferState,
    PolicyConfig,
    ProductState,
    ProposedAction,
    ProposedItem,
    SupplierState,
    TrustedState,
)
from ctrl.policy_checks import calculate_total, run_policy_checks
from ctrl.reason_codes import REASON_ORDER, Outcome, ReasonCode as R

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
BASE_OFFER = OfferState("OFFER-001", "PRD-001", "SUP-001", D("6.50"), "USD", 10, 5, 500, NOW)
BASE_PRODUCT = ProductState("PRD-001", True, 8, 3, 15, 60)  # position 20, need 40


def make_state(**over) -> TrustedState:
    base = dict(
        suppliers={"SUP-001": SupplierState("SUP-001", "approved")},
        products={"PRD-001": BASE_PRODUCT},
        offers={"OFFER-001": BASE_OFFER},
        spent_today=D("0.00"),
        existing_idempotency_keys=frozenset(),
        allowed_delivery_address_ids=frozenset({"ADDR-001"}),
        denied_action_fingerprints=frozenset(),
    )
    base.update(over)
    return TrustedState(**base)


def make_action(quantity=40, unit_price=D("6.50"), shipping=D("0.00"), tax=D("0.00"),
                stated_total=None, **over) -> ProposedAction:
    items = over.pop("items", (ProposedItem("PRD-001", "OFFER-001", quantity, unit_price),))
    if stated_total is None:
        stated_total = sum((i.quantity * i.unit_price for i in items), D("0")) + shipping + tax
        stated_total = stated_total.quantize(D("0.01"))
    base = dict(
        action_id="ACT-1", agent_run_id="RUN-1", tool_name="square.make_api_request",
        operation="create_order", supplier_id="SUP-001", items=items, currency="USD",
        shipping_cost=shipping, tax_amount=tax, stated_total=stated_total,
        delivery_address_id="ADDR-001", idempotency_key="KEY-1",
        is_subscription=False, retry_number=0,
    )
    base.update(over)
    return ProposedAction(**base)


def codes(findings):
    return {f.reason_code for f in findings}


def run(action=None, state=None, policy=None):
    return run_policy_checks(
        action if action is not None else make_action(),
        state if state is not None else make_state(),
        policy if policy is not None else PolicyConfig(),
    )


# ------------------------------------------------------------ allow cases
def test_baseline_order_has_no_findings():
    assert run() == ()


def test_exact_order_limit_boundary_is_allowed():
    assert run(policy=PolicyConfig(maximum_order_total=D("260.00"))) == ()


def test_exact_daily_limit_boundary_is_allowed():
    assert run(state=make_state(spent_today=D("740.00"))) == ()  # 740 + 260 = 1000


def test_exact_replenishment_need_boundary_is_allowed():
    assert run(action=make_action(quantity=40)) == ()


# ------------------------------------------------------------ block cases
def _offer(**kw):
    return {"OFFER-001": replace(BASE_OFFER, **kw)}


BLOCK_CASES = {
    "tool_not_allowed": (lambda: (make_action(tool_name="other.tool"), make_state(), PolicyConfig()), {R.TOOL_NOT_ALLOWED}),
    "operation_not_allowed": (lambda: (make_action(operation="refund"), make_state(), PolicyConfig()), {R.OPERATION_NOT_ALLOWED}),
    "subscription": (lambda: (make_action(is_subscription=True), make_state(), PolicyConfig()), {R.SUBSCRIPTION_PROHIBITED}),
    "supplier_unknown": (lambda: (make_action(supplier_id="SUP-999"), make_state(offers=_offer(supplier_id="SUP-999")), PolicyConfig()), {R.SUPPLIER_UNKNOWN}),
    "supplier_blocked": (lambda: (make_action(), make_state(suppliers={"SUP-001": SupplierState("SUP-001", "blocked")}), PolicyConfig()), {R.SUPPLIER_BLOCKED}),
    "supplier_suspended": (lambda: (make_action(), make_state(suppliers={"SUP-001": SupplierState("SUP-001", "suspended")}), PolicyConfig()), {R.SUPPLIER_SUSPENDED}),
    "supplier_weird_status_fails_closed": (lambda: (make_action(), make_state(suppliers={"SUP-001": SupplierState("SUP-001", "???")}), PolicyConfig()), {R.SUPPLIER_BLOCKED}),
    "product_unknown": (lambda: (make_action(items=(ProposedItem("PRD-999", "OFFER-001", 40, D("6.50")),)), make_state(offers=_offer(product_id="PRD-999")), PolicyConfig()), {R.PRODUCT_UNKNOWN}),
    "product_inactive": (lambda: (make_action(), make_state(products={"PRD-001": replace(BASE_PRODUCT, active=False)}), PolicyConfig()), {R.PRODUCT_INACTIVE}),
    "offer_unknown": (lambda: (make_action(items=(ProposedItem("PRD-001", "OFFER-999", 40, D("6.50")),)), make_state(), PolicyConfig()), {R.OFFER_UNKNOWN}),
    "offer_product_mismatch": (lambda: (make_action(), make_state(offers=_offer(product_id="PRD-002")), PolicyConfig()), {R.OFFER_PRODUCT_MISMATCH}),
    "offer_supplier_mismatch": (lambda: (make_action(), make_state(offers=_offer(supplier_id="SUP-002")), PolicyConfig()), {R.OFFER_SUPPLIER_MISMATCH}),
    "quantity_zero": (lambda: (make_action(quantity=0), make_state(), PolicyConfig()), {R.INVALID_QUANTITY}),
    "quantity_negative": (lambda: (make_action(quantity=-5), make_state(), PolicyConfig()), {R.INVALID_QUANTITY}),
    "below_minimum": (lambda: (make_action(quantity=5), make_state(), PolicyConfig()), {R.BELOW_MINIMUM_ORDER_QUANTITY}),
    "invalid_multiple": (lambda: (make_action(quantity=12), make_state(), PolicyConfig()), {R.INVALID_ORDER_MULTIPLE}),
    "supplier_stock_insufficient": (lambda: (make_action(), make_state(offers=_offer(available_quantity=30)), PolicyConfig()), {R.SUPPLIER_STOCK_INSUFFICIENT}),
    "currency_not_allowed": (lambda: (make_action(currency="EUR"), make_state(offers=_offer(currency="EUR")), PolicyConfig()), {R.CURRENCY_NOT_ALLOWED}),
    "offer_currency_mismatch": (lambda: (make_action(), make_state(offers=_offer(currency="EUR")), PolicyConfig()), {R.OFFER_CURRENCY_MISMATCH}),
    "incorrect_stated_total": (lambda: (make_action(stated_total=D("259.99")), make_state(), PolicyConfig()), {R.TOTAL_MISMATCH}),
    "order_limit_by_one_cent": (lambda: (make_action(), make_state(), PolicyConfig(maximum_order_total=D("259.99"))), {R.ORDER_LIMIT_EXCEEDED}),
    "daily_limit_by_one_cent": (lambda: (make_action(), make_state(spent_today=D("740.01")), PolicyConfig()), {R.DAILY_LIMIT_EXCEEDED}),
    "excess_replenishment": (lambda: (make_action(quantity=45), make_state(), PolicyConfig()), {R.EXCESS_REPLENISHMENT}),
    "excess_when_incoming_covers_need": (lambda: (make_action(quantity=10), make_state(products={"PRD-001": replace(BASE_PRODUCT, incoming_quantity=52)}), PolicyConfig()), {R.EXCESS_REPLENISHMENT}),
    "address_not_allowed": (lambda: (make_action(delivery_address_id="ADDR-999"), make_state(), PolicyConfig()), {R.DELIVERY_ADDRESS_NOT_ALLOWED}),
    "duplicate_idempotency_key": (lambda: (make_action(), make_state(existing_idempotency_keys=frozenset({"KEY-1"})), PolicyConfig()), {R.DUPLICATE_IDEMPOTENCY_KEY}),
}


@pytest.mark.parametrize("case", BLOCK_CASES, ids=list(BLOCK_CASES))
def test_block_case(case):
    build, expected = BLOCK_CASES[case]
    action, state, policy = build()
    findings = run_policy_checks(action, state, policy)
    assert codes(findings) == expected
    assert all(f.outcome == Outcome.BLOCK for f in findings)


def test_empty_order_is_blocked():
    assert codes(run(action=make_action(items=(), stated_total=D("0.00")))) == {R.INVALID_QUANTITY}


def test_unit_price_far_from_trusted_is_blocked():
    found = codes(run(action=make_action(unit_price=D("7.50"))))
    assert R.UNIT_PRICE_MISMATCH in found
    assert R.TOTAL_MISMATCH in found  # total is recalculated from the trusted price


def test_split_lines_are_summed_for_replenishment():
    items = (
        ProposedItem("PRD-001", "OFFER-001", 20, D("6.50")),
        ProposedItem("PRD-001", "OFFER-001", 25, D("6.50")),
    )
    assert codes(run(action=make_action(items=items))) == {R.EXCESS_REPLENISHMENT}


def test_overage_tolerance_allows_rounding_up_to_multiple():
    action = make_action(quantity=45)
    assert run(action=action, policy=PolicyConfig(maximum_replenishment_overage_units=5)) == ()
    assert codes(run(action=action, policy=PolicyConfig(maximum_replenishment_overage_units=4))) == {R.EXCESS_REPLENISHMENT}


# -------------------------------------------------------------- price rule
def test_small_price_change_escalates_not_blocks():
    findings = run(action=make_action(unit_price=D("6.70")))  # +3.08%
    assert codes(findings) == {R.PRICE_CHANGED}
    assert findings[0].outcome == Outcome.ESCALATE


def test_price_change_exactly_at_threshold_escalates():
    findings = run(action=make_action(unit_price=D("6.825")))  # exactly +5%
    assert codes(findings) == {R.PRICE_CHANGED}


def test_price_change_just_above_threshold_blocks():
    found = codes(run(action=make_action(unit_price=D("6.83"))))
    assert R.UNIT_PRICE_MISMATCH in found
    assert R.PRICE_CHANGED not in found


# --------------------------------------------------------------- stop cases
def test_retry_limit_reached_stops():
    findings = run(action=make_action(retry_number=3))
    assert codes(findings) == {R.RETRY_LIMIT_REACHED}
    assert findings[0].outcome == Outcome.STOP


def test_retry_below_limit_does_not_stop():
    assert run(action=make_action(retry_number=2)) == ()


def test_repeated_denied_action_stops():
    action = make_action()
    state = make_state(denied_action_fingerprints=frozenset({action_fingerprint(action)}))
    findings = run(action=action, state=state)
    assert codes(findings) == {R.REPEATED_DENIED_ACTION}
    assert findings[0].outcome == Outcome.STOP


def test_changed_action_is_not_a_repeat():
    denied = make_action(quantity=40)
    state = make_state(denied_action_fingerprints=frozenset({action_fingerprint(denied)}))
    assert run(action=make_action(quantity=35), state=state) == ()


# ------------------------------------------------------------ money helper
def test_total_recalculation_includes_shipping_and_tax():
    action = make_action(shipping=D("5.00"), tax=D("1.25"))
    assert calculate_total(action, make_state(), PolicyConfig()) == D("266.25")
    assert run(action=action) == ()


# ------------------------------------------------------------- fingerprint
def test_fingerprint_ignores_irrelevant_fields_and_item_order():
    a = make_action(items=(ProposedItem("PRD-001", "OFFER-001", 20, D("6.5")),
                           ProposedItem("PRD-002", "OFFER-005", 2, D("29.00"))))
    b = replace(a, action_id="OTHER", idempotency_key="OTHER", retry_number=2,
                items=tuple(reversed(a.items)))
    assert action_fingerprint(a) == action_fingerprint(b)


@pytest.mark.parametrize("change", [
    dict(delivery_address_id="ADDR-002"),
    dict(supplier_id="SUP-002"),
    dict(is_subscription=True),
    dict(currency="EUR"),
    dict(shipping_cost=D("1.00")),
])
def test_fingerprint_changes_with_security_relevant_fields(change):
    action = make_action()
    assert action_fingerprint(action) != action_fingerprint(replace(action, **change))


def test_fingerprint_changes_with_quantity_and_price():
    assert action_fingerprint(make_action(quantity=40)) != action_fingerprint(make_action(quantity=35))
    assert action_fingerprint(make_action(unit_price=D("6.50"))) != action_fingerprint(make_action(unit_price=D("6.60")))


# ------------------------------------------------------------- determinism
def test_repeated_runs_are_identical():
    action = make_action(quantity=45, currency="EUR", is_subscription=True)
    state = make_state(offers=_offer(currency="EUR"))
    assert run(action=action, state=state) == run(action=action, state=state)


def test_findings_follow_enum_order_and_ignore_dict_insertion_order():
    action = make_action(items=(
        ProposedItem("PRD-001", "OFFER-001", 45, D("6.50")),
        ProposedItem("PRD-002", "OFFER-002", 7, D("29.00")),
    ), is_subscription=True, retry_number=5)
    offer2 = OfferState("OFFER-002", "PRD-002", "SUP-001", D("29.00"), "USD", 2, 1, 120, NOW)
    prod2 = ProductState("PRD-002", True, 6, 2, 0, 24)

    s1 = make_state(offers={"OFFER-001": BASE_OFFER, "OFFER-002": offer2},
                    products={"PRD-001": BASE_PRODUCT, "PRD-002": prod2})
    s2 = make_state(offers={"OFFER-002": offer2, "OFFER-001": BASE_OFFER},
                    products={"PRD-002": prod2, "PRD-001": BASE_PRODUCT})

    f1, f2 = run(action=action, state=s1), run(action=action, state=s2)
    assert f1 == f2
    order = [REASON_ORDER[f.reason_code] for f in f1]
    assert order == sorted(order)


# ---------------------------------------------------------------- contract
def test_decision_round_trips_through_json():
    decision = Decision(Outcome.BLOCK, (R.TOTAL_MISMATCH, R.ORDER_LIMIT_EXCEEDED),
                        "Blocked.", "abc123", False)
    assert Decision.from_dict(json.loads(json.dumps(decision.to_dict()))) == decision
