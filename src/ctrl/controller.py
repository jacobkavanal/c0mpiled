"""Public deterministic decision entry point for CTRL."""

from __future__ import annotations

from datetime import datetime

from .approvals import action_fingerprint, approval_findings
from .models import Approval, Decision, Finding, PolicyConfig, ProposedAction, TrustedState
from .policy_checks import check_escalations, run_policy_checks
from .reason_codes import OUTCOME_PRECEDENCE, REASON_ORDER, Outcome


def evaluate(
    *,
    action: ProposedAction,
    state: TrustedState,
    policy: PolicyConfig,
    approval: Approval | None,
    now: datetime,
) -> Decision:
    """Evaluate an action without I/O and return an auditable verdict.

    Precedence is fixed: STOP > BLOCK > ESCALATE > ALLOW. A valid approval can
    remove only the escalation findings it explicitly scopes; it never changes
    a hard block or stop result.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    findings = list(run_policy_checks(action, state, policy))
    findings.extend(check_escalations(action, state, policy, now))
    approval_applied = False

    if approval is not None:
        findings.extend(approval_findings(action, approval, now))
        approval_errors = [
            finding
            for finding in findings
            if finding.reason_code.value.startswith("APPROVAL_")
        ]
        escalations = [finding for finding in findings if finding.outcome is Outcome.ESCALATE]
        if not approval_errors and escalations:
            escalation_codes = {finding.reason_code.value for finding in escalations}
            if escalation_codes.issubset(approval.resolvable_reason_codes):
                findings = [finding for finding in findings if finding.outcome is not Outcome.ESCALATE]
                approval_applied = True

    ordered = tuple(sorted(findings, key=lambda finding: (REASON_ORDER[finding.reason_code], finding.message)))
    outcome = max(
        (finding.outcome for finding in ordered),
        key=lambda candidate: OUTCOME_PRECEDENCE[candidate],
        default=Outcome.ALLOW,
    )
    explanation = (
        "Action satisfies all deterministic policy checks."
        if not ordered
        else " ".join(finding.message for finding in ordered)
    )
    return Decision(
        outcome=outcome,
        reason_codes=tuple(finding.reason_code for finding in ordered),
        explanation=explanation,
        action_fingerprint=action_fingerprint(action),
        approval_applied=approval_applied,
    )
