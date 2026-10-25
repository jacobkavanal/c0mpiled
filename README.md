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
