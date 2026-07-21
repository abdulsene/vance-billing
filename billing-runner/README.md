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
  post-steps — a human resolves it before the next run. If `ORPHAN_ALERT_PHONE` is set, the
  runner also texts that number (`client_id: OPS-ALERT`) so nobody has to be watching logs.
  The alert is best-effort: if the SMS itself fails, it is recorded as an `orphan_alert`
  error and the orphan is still reported — alerting can never mask the orphan.
- Post-charge steps (commit/invoice/receipt) are best-effort: their failure cannot cause a
  re-charge because the cycle is already recorded.
- `orderid = client_id|cycle` gives NMI-side duplicate-transaction protection as defense in depth.

## Run it
- **Preflight (check before you trust a run):** `GET /preflight` with header
  `X-API-Key: <INTERNAL_API_KEY>`. Returns `{"ready": bool, "version", "checks": [...]}` — one
  row per required env var plus a live health probe (8s timeout) of verdict-service
  (`/parser/health`), billing-api (`/billing/health`) and webhooks (`/webhooks/health`) —
  each service namespaces its own health route. `ready` is true only when every required row is ok. A dead
  dependency is a red row, never a 500. The `orphan alert` row reports whether
  `ORPHAN_ALERT_PHONE` is set; it is informational and does not gate `ready`.
- **Manual / self-test:** `POST /run` with header `X-API-Key: <INTERNAL_API_KEY>`
  (optional `?date=YYYY-MM-DD`). Returns a per-client summary.
- **Scheduled:** Railway Cron runs `python run.py` daily. Exits non-zero if any orphan, so a
  failed cron surfaces a charged-but-unmarked client.

## Env vars
**Required:** `VERDICT_BASE`, `BILLING_BASE`, `WEBHOOKS_BASE` (full https URLs),
`INTERNAL_API_KEY`, `NMI_ENDPOINT` (`ecrypt.transactiongateway.com`), `NMI_SECURITY_KEY`.

**Optional:** `ORPHAN_ALERT_PHONE` — E.164 number paged by SMS when a charge-orphan occurs.
Unset means orphans are logged only. `GET /preflight` shows which of these are in place.

## Tests
`python -m pytest tests/ -q` — behavioral tests covering charge, skip, decline, error,
empty-response, orphan handling (including the ops alert), null-phone clients, dry run,
post-step failures, idempotency key, and multi-client runs.

## Why this exists
n8n broke the billing path twice (a browser-global crash in a Code node, and an orchestrator
DB connection drop mid-run). Billing is the revenue path; it must be deterministic and tested,
not clickable-and-hope. n8n can remain for non-critical automations or observability.
