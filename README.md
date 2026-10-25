# CTRL purchasing-agent backend

This branch contains the minimal deterministic purchasing-agent harness. It reads the trusted Supabase inventory/catalog data, finds low-stock products, deterministically selects one compatible supplier offer, and prints normalized `ProposedAction` JSON for CTRL's policy engine.

It **does not execute purchases**. The only permitted next step is to send every proposal to Person 1's `evaluate(action, state, policy, approval, now)` controller. An execution adapter may be added only after an `ALLOW` decision.

It also loads the controller's trusted state from Supabase: supplier approval statuses, all active/inactive products and on-order quantities, offers, committed UTC-day spend, and existing idempotency keys. It deliberately returns no denied-action fingerprints yet because the current `purchase_attempts` schema does not persist a canonical fingerprint; the audit step will add that bridge.

## Run it

Use a server-side environment only (never a browser client):

```powershell
$env:SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY = "your-server-only-service-role-key"
$env:PYTHONPATH = "src"
python -m purchasing_agent.main
```

The default command evaluates and prints decisions without writing to the database. For the demo, append immutable `purchase_attempts` audit rows explicitly:

```powershell
python -m purchasing_agent.main --record-audit
```

The Supabase schema already enables RLS with no public policies, so the demo backend needs the service-role key for reads. Keep it in `.env` or your deployment secret manager; never commit or expose it to the UI.

## Selection behavior

For each record in `inventory_replenishment_status` where `requires_replenishment` is true, the planner:

1. excludes suspended and blocked suppliers;
2. prefers the inventory product's preferred supplier;
3. then prefers a preferred offer, lower unit price, shorter lead time, and stable offer ID;
4. rounds to supplier MOQ/order multiples; and
5. creates a single-item JSON proposal matching Person 1's controller contract.

The planner may round upward for supplier MOQ/multiples. Person 1's policy configuration must explicitly allow that resulting overage, or the controller will correctly block the proposal. This is intentional, visible policy behavior—not an agent bypass.

## What remains

- Persist the controller's canonical action fingerprint alongside denied audit records (the current schema does not yet have a column for it).
- Add a human-approval flow for `ESCALATE` results.
- Add an execution adapter that is reachable only after `ALLOW`.

---

# Full project handoff

## One-sentence product definition

CTRL is a deterministic, state-aware enforcement layer for a paper-company purchasing agent: the agent can propose an order, but only explicit policy checks against trusted Supabase state can allow, block, stop, or escalate it.

## Non-negotiable decisions

| Topic | Decision |
| --- | --- |
| LLM/Jev | Not used in any control decision. No semantic classifier or agent decides policy. |
| Enforcement | Pure Python, deterministic rules with stable reason codes. |
| Verdict precedence | `STOP > BLOCK > ESCALATE > ALLOW`. Hard violations always win. |
| Money | `Decimal`, never floating point. JSON uses decimal strings. |
| State source | Supabase, freshly loaded before evaluation/re-evaluation. |
| Execution | Not implemented. An audit row is not a purchase. |
| Policy table | Supabase table exists but has zero rows by intentional team decision. |
| Human escalation | Twilio is the communication/UI layer; CTRL remains the authority. |

## End-to-end flow

```text
inventory_replenishment_status + inventory + suppliers + supplier_products
                              ↓
               deterministic planner creates proposal
                              ↓
                  fresh trusted-state snapshot
                              ↓
          CTRL evaluate(action, state, policy, approval, now)
                              ↓
          ALLOW | ESCALATE | BLOCK | STOP
                              ↓
             optional purchase_attempts audit record
```

The current repository ends at audit logging. It cannot make a real purchase, call Square, or charge money.

## Code ownership and files

| Location | Responsibility | Key facts |
| --- | --- | --- |
| `src/ctrl/models.py` | Controller contract | Immutable dataclasses: action, trusted state, policy, approval, finding, decision. |
| `src/ctrl/reason_codes.py` | Stable language of control | `Outcome`, all reason codes, message templates, precedence, stable ordering. |
| `src/ctrl/policy_checks.py` | Individual policy rules | Pure checks only; no I/O, environment reads, network, or database. |
| `src/ctrl/approvals.py` | Scoped approval security | Canonical SHA-256 action fingerprint and expiry/used/scope checks. |
| `src/ctrl/controller.py` | Public entry point | Implements `evaluate(...)`, aggregation, approval resolution, and final verdict. |
| `src/purchasing_agent/planner.py` | Proposal generation | Finds low stock and deterministically chooses one candidate offer. |
| `src/purchasing_agent/trusted_state.py` | State construction | Builds controller state from trusted database reads. |
| `src/purchasing_agent/supabase_repository.py` | Supabase boundary | Reads inventory/catalog/spend; appends audit records only. |
| `src/integration/controller_adapter.py` | Explicit bridge | Converts agent models to controller types and constructs schema-compatible audit rows. |

## Controller API contract

```python
decision = evaluate(
    action=proposed_action,
    state=trusted_state,
    policy=policy_config,
    approval=approval_or_none,
    now=current_utc_datetime,
)
```

`evaluate()` must remain pure. It does not query Supabase, execute tools, call an LLM, or read the system clock. `now` is passed by the caller so time-based behavior remains testable.

Decision JSON has this shape:

```json
{
  "outcome": "ALLOW",
  "reason_codes": [],
  "explanation": "Action satisfies all deterministic policy checks.",
  "action_fingerprint": "sha256 hex digest",
  "approval_applied": false
}
```

## Current default policy

| Policy setting | Demo default |
| --- | --- |
| tool / operation | `square.make_api_request` / `create_order` |
| currency | `USD` |
| per-order maximum | `$500.00` |
| daily maximum | `$1,000.00` |
| replenishment overage | `0` units |
| price-change escalation band | `5%` |
| stale price age | `7` days |
| max retries | `3` |
| subscriptions | prohibited |

These are in code as `PolicyConfig`, not yet loaded from `purchasing_policies`. Do not populate that table or change values silently; agree as a team first.

## What CTRL checks

`STOP`: retry limit reached; identical previously denied action retried.

`BLOCK`: unapproved tool/operation, subscription, unknown/blocked/suspended supplier, unknown/inactive product, invalid offer relationship, bad quantity/MOQ/multiple/availability, forbidden currency, large price mismatch, mismatched stated total, order/daily spend cap, over-replenishment, unapproved address, or duplicated idempotency key.

`ESCALATE`: pending supplier, substitution, small price change, stale price, or missing required action fields.

`ALLOW`: no unresolved findings. All reason codes remain in the result, in deterministic enum order, even if a higher-priority finding controls the final outcome.

### Price and inventory semantics

- Exact trusted price: proceed to other checks.
- Difference at or below the configured price band: `PRICE_CHANGED` escalation.
- Greater difference: `UNIT_PRICE_MISMATCH` block.
- Inventory position is `quantity_on_hand - quantity_reserved + incoming_quantity`.
- Need is `max(target_stock - inventory_position, 0)`.
- Incoming units from placed/shipped orders prevent duplicate purchases.

## Planner behavior

For each low-stock product, the planner chooses an offer in this exact order:

1. Exclude blocked/suspended suppliers and non-USD offers.
2. Prefer the product's preferred supplier.
3. Then prefer `is_preferred` offer.
4. Then lower price, shorter lead time, and stable offer ID.
5. Round quantity upward to MOQ and order multiple.
6. Skip an offer if known available quantity is insufficient.

Rounding may exceed the controller's zero-overage default. That should be visible as a block until policy explicitly permits the overage; it is not a bypass.

## Database assumptions

The live Supabase database was created before this repository code. Required objects are:

| Object | Used for |
| --- | --- |
| `suppliers` | trusted approval status |
| `inventory` | activity, on-hand, reserved, target stock, preferred supplier |
| `supplier_products` | trusted offer price, MOQ, multiple, availability, currency, price timestamp |
| `purchase_orders` | UTC-day committed spend and used idempotency keys |
| `purchase_attempts` | audit trail |
| `inventory_replenishment_status` | low-stock and incoming-quantity derived view |

The current fake paper-company data has 11 products, 24 offers, 9 orders, 15 order items, 21 inventory movements, 10 attempts, and zero policy rows. RLS is enabled with no client policies, so this backend needs a **server-only** service-role key. Never put that key in Twilio client code, a browser, Git, or chat.

## Approval / Twilio handoff

Escalation is already implemented. What Twilio must add is the human-response path:

1. Receive an `ESCALATE` decision and its reason codes.
2. Ask the human to approve or deny that exact action.
3. On approval, create `Approval(action_fingerprint, expires_at, used=False, resolvable_reason_codes)`.
4. Reload fresh trusted state and call `evaluate()` again with the approval.
5. Continue only when the re-evaluated result is `ALLOW`.
6. Denial is audited. After persistent denied fingerprints are added, an identical retry should produce `STOP`.

Approvals may resolve only pending-supplier, substitution, small-price-change, and stale-price escalations. They can never override a hard `BLOCK` or `STOP`.

## Audit mapping

`purchase_attempts` receives the original proposal JSON, result reason codes, deterministic explanation, and `execution_status = not_executed`.

| Controller outcome | Stored decision |
| --- | --- |
| `ALLOW` | `allow` |
| `ESCALATE` | `require_approval` |
| `BLOCK` / `STOP` | `block` |

## Setup, verification, and safe demo commands

Use Python 3.12 and run:

```powershell
$env:PYTHONPATH = "src"
python -m pytest -q
```

Expected result: **69 passed**.

For live, server-side validation only:

```powershell
$env:SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY = "SERVER_ONLY_SECRET"
$env:PYTHONPATH = "src"
python -m purchasing_agent.main
```

That command reads live Supabase data, generates proposals, evaluates them, and prints JSON without writing anything. The program does not auto-load `.env`; variables must exist in the process environment.

To append audit rows deliberately (still no purchase execution):

```powershell
python -m purchasing_agent.main --record-audit
```

## Remaining tasks

1. Configure secrets locally and run the read-only live validation.
2. Connect the Twilio approval/deny response to the existing `Approval` contract and re-evaluation call.
3. Add a durable denied-action fingerprint field/store for cross-run `STOP` behavior.
4. Build a minimal demo display: proposal → decision → reason codes → audit record.
5. Create 10–20 fixed scenarios for allow/escalate/block/stop benchmarking.
6. Only if required for the pitch, add a fake executor behind an unbreakable current-`ALLOW` gate. Do not add real payments.

## Git history

Active branch: `feature/purchasing-agent-backend`.

Key commits: `eacf07e` proposal planner, `e04e210` trusted-state loader, `f69d216` controller integration, `916b719` `evaluate()`, and `2026dc1` controller-to-audit integration. Initial Person 1 work remains at `origin/person1` for history.

### Invariants another agent must preserve

- No LLM/Jev inside CTRL decisions.
- No I/O in `src/ctrl/`.
- Use `Decimal`, injected UTC time, fixed reason codes, and fixed precedence.
- Reload trusted state immediately before approval re-evaluation or any future execution.
- An audit event is never evidence of execution.
- No external purchase is permitted without a fresh `ALLOW`.
