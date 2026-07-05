# Vance Billing Runner

The billing "gate" as deterministic, tested Python — **replaces the n8n workflow** on the
critical money path. Every step is an HTTP call to services that already exist; this service
is the scheduler + orchestrator, but unlike n8n it is unit-tested, has no browser-global
pitfalls (the `URLSearchParams` crash class is impossible here), and does not depend on a
visual editor's database staying reachable to save/run.

## What it does (one pass)
For each client returned by `billing-api /billing/due`:
1. `GET verdict-service /parser/verdict` — did the report move this cycle?
2. **Not moved → skip** (no movement, no charge; no text sent).
3. **Moved → charge** the vaulted card via NMI (`customer_vault_id`, `orderid=client|cycle`).
4. Parse the NMI reply (dependency-free; empty/garbage ⇒ *not* approved).
5. **Approved →** `mark-billed` (immediately, to minimize re-charge window) → then best-effort
   `commit`, `create-invoice`, `send-receipt`.
6. **Declined / error / empty →** `notify payment_failed` (dunning). **Never** marked billed.

## Safety invariants (all covered by tests)
- No movement ⇒ no charge.
- A non-approved or unreadable NMI response can never reach `mark-billed`.
- `mark-billed` runs immediately after a successful charge. If it fails, we log
  `CHARGE-ORPHAN` (loud, greppable) with the transaction id for reconciliation and skip
  post-steps — a human resolves it before the next run.
- Post-charge steps (commit/invoice/receipt) are best-effort: their failure cannot cause a
  re-charge because the cycle is already recorded.
- `orderid = client_id|cycle` gives NMI-side duplicate-transaction protection as defense in depth.

## Run it
- **Manual / self-test:** `POST /run` with header `X-API-Key: <INTERNAL_API_KEY>`
  (optional `?date=YYYY-MM-DD`). Returns a per-client summary.
- **Scheduled:** Railway Cron runs `python run.py` daily. Exits non-zero if any orphan, so a
  failed cron surfaces a charged-but-unmarked client.

## Env vars (all required)
`VERDICT_BASE`, `BILLING_BASE`, `WEBHOOKS_BASE` (full https URLs),
`INTERNAL_API_KEY`, `NMI_ENDPOINT` (`ecrypt.transactiongateway.com`), `NMI_SECURITY_KEY`.

## Tests
`python -m pytest tests/ -q` — 10 behavioral tests covering charge, skip, decline, error,
empty-response, orphan handling, post-step failures, idempotency key, and multi-client runs.

## Why this exists
n8n broke the billing path twice (a browser-global crash in a Code node, and an orchestrator
DB connection drop mid-run). Billing is the revenue path; it must be deterministic and tested,
not clickable-and-hope. n8n can remain for non-critical automations or observability.
