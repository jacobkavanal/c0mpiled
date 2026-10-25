from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from ctrl.approvals import action_fingerprint
from ctrl.controller import evaluate
from ctrl.models import (
    Approval,
    OfferState,
    PolicyConfig,
    ProductState,
    ProposedAction,
    ProposedItem,
    SupplierState,
    TrustedState,
)
from ctrl.reason_codes import Outcome, ReasonCode as R

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
OFFER = OfferState("OFFER-1", "PRD-1", "SUP-1", D("5.00"), "USD", 1, 1, 100, NOW)


def action(**changes) -> ProposedAction:
    values = dict(
        action_id="ACT-1", agent_run_id="RUN-1", tool_name="square.make_api_request",
        operation="create_order", supplier_id="SUP-1",
        items=(ProposedItem("PRD-1", "OFFER-1", 10, D("5.00")),), currency="USD",
        shipping_cost=D("0.00"), tax_amount=D("0.00"), stated_total=D("50.00"),
        delivery_address_id="WAREHOUSE-1", idempotency_key="IDEMP-1",
        is_subscription=False, retry_number=0,
    )
    values.update(changes)
    return ProposedAction(**values)


def state(**changes) -> TrustedState:
    values = dict(
        suppliers={"SUP-1": SupplierState("SUP-1", "approved")},
        products={"PRD-1": ProductState("PRD-1", True, 0, 0, 0, 10)},
        offers={"OFFER-1": OFFER}, spent_today=D("0.00"),
        existing_idempotency_keys=frozenset(),
        allowed_delivery_address_ids=frozenset({"WAREHOUSE-1"}),
        denied_action_fingerprints=frozenset(),
    )
    values.update(changes)
    return TrustedState(**values)


def evaluate_action(action_changes=None, state_changes=None, approval=None, now=NOW):
    return evaluate(
        action=action(**(action_changes or {})),
        state=state(**(state_changes or {})),
        policy=PolicyConfig(),
        approval=approval,
        now=now,
    )


def test_allow_returns_structured_decision() -> None:
    decision = evaluate_action()
    assert decision.outcome is Outcome.ALLOW
    assert decision.reason_codes == ()
    assert decision.approval_applied is False


def test_pending_supplier_escalates() -> None:
    decision = evaluate_action(state_changes={"suppliers": {"SUP-1": SupplierState("SUP-1", "pending")}})
    assert decision.outcome is Outcome.ESCALATE
    assert decision.reason_codes == (R.SUPPLIER_PENDING_REVIEW,)


def test_hard_block_beats_escalation() -> None:
    decision = evaluate_action(
        action_changes={"stated_total": D("600.00")},
        state_changes={"suppliers": {"SUP-1": SupplierState("SUP-1", "pending")}},
    )
    assert decision.outcome is Outcome.BLOCK
    assert R.SUPPLIER_PENDING_REVIEW in decision.reason_codes
    assert R.TOTAL_MISMATCH in decision.reason_codes


def test_stop_beats_every_other_outcome() -> None:
    decision = evaluate_action(
        action_changes={"retry_number": 3, "is_subscription": True},
        state_changes={"suppliers": {"SUP-1": SupplierState("SUP-1", "pending")}},
    )
    assert decision.outcome is Outcome.STOP
    assert decision.reason_codes[0] is R.RETRY_LIMIT_REACHED


def test_valid_scoped_approval_resolves_pending_supplier() -> None:
    proposed = action()
    approval = Approval(
        approval_id="APR-1", action_fingerprint=action_fingerprint(proposed),
        expires_at=NOW + timedelta(minutes=5), used=False,
        resolvable_reason_codes=frozenset({R.SUPPLIER_PENDING_REVIEW.value}),
    )
    decision = evaluate(
        action=proposed,
        state=state(suppliers={"SUP-1": SupplierState("SUP-1", "pending")}),
        policy=PolicyConfig(), approval=approval, now=NOW,
    )
    assert decision.outcome is Outcome.ALLOW
    assert decision.approval_applied is True


def test_approval_cannot_override_hard_block() -> None:
    proposed = action(stated_total=D("600.00"))
    approval = Approval(
        approval_id="APR-1", action_fingerprint=action_fingerprint(proposed),
        expires_at=NOW + timedelta(minutes=5), used=False,
        resolvable_reason_codes=frozenset({R.SUPPLIER_PENDING_REVIEW.value}),
    )
    decision = evaluate(
        action=proposed,
        state=state(suppliers={"SUP-1": SupplierState("SUP-1", "pending")}),
        policy=PolicyConfig(), approval=approval, now=NOW,
    )
    assert decision.outcome is Outcome.BLOCK
    assert decision.approval_applied is True


def test_expired_approval_blocks() -> None:
    proposed = action()
    approval = Approval(
        approval_id="APR-1", action_fingerprint=action_fingerprint(proposed),
        expires_at=NOW, used=False,
        resolvable_reason_codes=frozenset({R.SUPPLIER_PENDING_REVIEW.value}),
    )
    decision = evaluate(
        action=proposed,
        state=state(suppliers={"SUP-1": SupplierState("SUP-1", "pending")}),
        policy=PolicyConfig(), approval=approval, now=NOW,
    )
    assert decision.outcome is Outcome.BLOCK
    assert R.APPROVAL_EXPIRED in decision.reason_codes


def test_stale_price_escalates() -> None:
    decision = evaluate_action(
        state_changes={"offers": {"OFFER-1": replace(OFFER, price_updated_at=NOW - timedelta(days=8))}}
    )
    assert decision.outcome is Outcome.ESCALATE
    assert decision.reason_codes == (R.PRICE_DATA_STALE,)


def test_naive_clock_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_action(now=datetime(2026, 10, 24, 12))
