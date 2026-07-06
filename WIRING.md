# Vance Credit — Master Wiring (all 4 services + the n8n gate)

How enrollment, the billing-api, the verdict service, and the webhook receivers
connect to the n8n billing gate and Supabase. **Two tiers, both movement-billed:**
`dispute` = $99/cycle (First Class mail), `complete` = $149/cycle (Certified mail).
They differ **only** in mail class — billing is identical. CROA rule throughout:
**nothing is charged at enrollment; a charge happens only in cycles the report
moved, one cycle at a time ("no movement, no charge").**

---

## 1. Full flow

```
                         ┌──────────────────────────────────────────────┐
   Pricing page          │                 enrollment/                  │
   (Collect.js token) ──▶ │  POST /enroll                                │
                         │   • NMI Customer Vault add ($0, NO charge)   │
                         │   • write client ─────────────┐              │
                         └───────────────────────────────┼──────────────┘
                                                          ▼
                                              Supabase:  vc_clients
                                                          ▲   (read-only)
                                                          │
   ┌──────────────────────────── n8n billing gate (runs daily) ─────────────────────────┐
   │                                                      │                              │
   │  GET billing-api /billing/due?date=YYYY-MM-DD  ──────┘  (active, not-yet-billed;    │
   │        │                                                tier-agnostic — ALL clients)│
   │        ▼                                                                            │
   │  GET verdict /parser/verdict?client_id&cycle    ◀── BOTH tiers, movement-billed     │
   │        • moved?  yes → charge   no → SKIP ("no movement, no charge")                │
   │                                                                                     │
   │   CHARGE  ─▶  NMI sale  (customer_vault_id, amount: $99 dispute / $149 complete)    │
   │        │                                                                            │
   │        ├─ success ─▶ POST webhooks /crc/invoice   (add invoice line to CRC)         │
   │        │        └──▶ POST webhooks /notify type=receipt   (SMS)                     │
   │        │        └──▶ POST billing-api /billing/mark-billed     ◀── ADD (idempotency)│
   │        │        └──▶ POST verdict /parser/verdict/commit       ◀── ADD (both tiers) │
   │        │                                                                            │
   │        ├─ decline ─▶ POST webhooks /notify type=payment_failed (SMS)                │
   │        │                                                                            │
   │        └─ skip ────▶ POST webhooks /notify type=skipped        (SMS)                │
   └─────────────────────────────────────────────────────────────────────────────────────┘

   Side channel (NOT a billing gate): when a `complete` client's Certified round
   is mailed, the dispatch/CRC step POSTs billing-api /dispatch/record-round for
   proof tracking. /dispatch/round-status is ops/audit only — it no longer gates
   any charge.
```

The two `◀── ADD` calls are **manual n8n additions** (see §3). Without
`mark-billed`, the same client is re-billed next run; without `verdict/commit`,
a billed deletion stays "movement" and bills again next cycle. **`verdict/commit`
now applies to BOTH tiers** (both are movement-billed), not dispute-only.

`GET /billing/due` returns each due client with `client_id, plan_tier,
monthly_amount, customer_vault_id, cycle, contact{email,phone}` **plus top-level
`email` + `phone`** — the billing-runner reads those directly to send SMS
receipts/dunning (see `billing-api/README.md`).

---

## 2. Services

| Service | Railway root dir | Env vars | Supabase tables (own / read) |
|---|---|---|---|
| **verdict-service** | `verdict-service` | `DATABASE_URL`, `CAPTURE_ORIGINS` *(opt, CORS; default `*`)* | **owns** `vc_snapshots`, `vc_letters`, `vc_credited_changes`, `vc_manual_movements` |
| **enrollment** | `enrollment` | `DATABASE_URL`, `NMI_SECURITY_KEY`, `NMI_ENDPOINT` *(**must match the Collect.js gateway host** — `ecrypt.transactiongateway.com` for this account)*, `ENROLL_CORS_ORIGINS` *(opt, CORS; default vancecredit.com)*, `ENROLL_WEBHOOK_URL` *(opt, welcome email)*, `CRC_CREATE_CLIENT_WEBHOOK` *(opt)* | **owns/writes** `vc_clients` (incl. billing address) |
| **billing-api** | `billing-api` | `DATABASE_URL` | **owns** `vc_billed_cycles`, `vc_dispatch_rounds`; **reads** `vc_clients` |
| **webhooks** | `webhooks` | `CRC_INVOICE_URL` *(opt)*, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` *(all opt)* | **none** (stateless) |

Every service: NIXPACKS builder, `.python-version` = **3.12**, start command
`uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}`. Unset optional env vars
degrade to a logged stub (CRC forward / Twilio SMS) — safe for dev.

**Verdict has three movement sources, deduped to one charge.** Beyond tri-bureau
**snapshots** and bureau-response **letters**, the verdict service now accepts
reviewer-confirmed **manual movement** via `POST /parser/manual-movement`,
captured with `capture/movement-capture.html` after each CRC re-import. All three
sources share the same `change_id` (`bureau|type|creditor|account|kind|target`),
so a manual entry and a later snapshot/letter of the same change collapse to a
single billable change. A manual entry alone is enough — the verdict returns
`moved:true` with no snapshot ingested. (`CAPTURE_ORIGINS` restricts which origins
the browser form may call; defaults to `*`.)

**Enrollment captures a billing address for AVS.** `POST /enroll` accepts
`name, address1, address2, city, state, zip` (all optional in the schema so partial
payloads still enroll — we do **not** hard-reject on AVS at signup, since subprime
address mismatches shouldn't block sign-up). The address is sent to the NMI
**Customer Vault** on `add_customer` (`address1/city/state/zip`, `address2` when
present, plus `first_name/last_name` split on the client's **last** space) so AVS
data lives on the vault record and is **evaluated at charge time** on every future
sale via `customer_vault_id`. The same fields persist as columns on `vc_clients`
(apply the `alter table ... add column if not exists` block in
`enrollment/clients_schema.sql` to existing databases).

**`DATABASE_URL` must be the Supabase session-pooler DSN**
(`postgres://…:5432/…`), pointed at the same database `billing-api` reads. It is
validated at **startup**: a value that doesn't start with `postgres://` /
`postgresql://` (e.g. a service URL pasted in by mistake) logs a WARNING at boot —
`DATABASE_URL does not look like a Postgres DSN …` — instead of failing silently at
first enrollment. At request time, a persistence failure *after* the card is
vaulted returns a clean **503** (`"Enrollment is temporarily unavailable. Your card
was not charged; please try again shortly."`) — never a 500, and the DSN / raw
`psycopg` error is never leaked to the browser. Because the vault already
succeeded, that path logs a loud, greppable **`VAULT-ORPHAN`** ERROR
(`customer_vault_id` + `client_id`) so the orphaned $0 vault entry can be
reconciled (no money is at risk — enrollment only vaults).

**`NMI_ENDPOINT` must match the Collect.js gateway host.** Collect.js payment
tokens are **host-specific**: a token minted by the widget served from one gateway
host is only valid for vault/sale calls to that same host. For this account that
host is **`ecrypt.transactiongateway.com`** — set `NMI_ENDPOINT` to it. Leaving it
at the generic `secure.nmi.com` (the default) mismatches the token and NMI rejects
the vault call. Enrollment logs a startup WARNING when `NMI_ENDPOINT` is unset or
`secure.nmi.com`, and a vault decline/error now returns a clean **402**
("Card could not be stored: …") with the exact NMI `responsetext` logged — never
an unhandled 500.

**`ENROLL_WEBHOOK_URL` — welcome-email trigger (non-blocking).** On a **successful**
enrollment (card vaulted + `vc_clients` row committed), enrollment fires a
best-effort JSON `POST` to `ENROLL_WEBHOOK_URL` — the HighLevel **"Vance New
Enrollment"** inbound webhook that starts the welcome-email workflow. Body:
`{ first_name, email, phone, plan_tier, client_id }` (`first_name` = everything
before the first space of the name). It runs **after** the client is durable and is
fully **non-blocking**: if the URL is unset it skips silently (debug log), and any
POST failure is logged (warning) and swallowed — a webhook/email hiccup can **never**
fail or roll back an enrollment. Leave it unset in dev/test.

### Accepted card brands — Visa + Mastercard only (runbook)

**Amex + Discover are OFF** pending processor approval. Enrollment is gated to
Visa/Mastercard in **three** places (keep them in sync):

1. **Front end** (`enrollment/frontend/card-brand-guard.js`, paste target for the
   pricing-page modal): `const ACCEPTED_BRANDS = ['visa','mastercard']`. Collect.js
   reports the brand; an unsupported card marks the `ccnumber` field invalid, shows
   *"We currently accept Visa and Mastercard only. Please use a different card."*,
   and `submitEnroll()` is blocked (never calls `startPaymentRequest`). A muted
   *"We accept Visa and Mastercard."* line renders under the CARD DETAILS label.
   Pure logic lives in `brand_guard.mjs` (tested via `npm run test:brand`).
2. **Backend backstop** (`enrollment` `POST /enroll`): if NMI reports a card type
   outside `enroll_core.ACCEPTED_CARD_BRANDS = {visa, mastercard}`, it returns a
   clean **422** `{"detail":"Card type not accepted: <brand>. Please use Visa or
   Mastercard."}`, **does not create the client**, and **deletes the just-created
   vault entry** (`customer_vault=delete_customer`, best-effort) so no orphaned
   record is kept. A delete failure logs a `VAULT-ORPHAN` ERROR for manual cleanup
   but still returns the 422 (never a 500). Caveat: a `$0` `add_customer` vault
   often does **not** return a brand (`cc_type` is a sale/auth field), so this only
   fires when a brand is present; when it's absent the front-end guard + the runner
   net below are the safety net.
3. **Runner net**: when the brand is unknown pre-charge and an Amex/Discover slips
   through, the first real charge declines with a **"payment type not accepted"**
   reason. Treat that decline as an **ops follow-up** (contact the client to switch
   to Visa/Mastercard) — it is not a card-update/dunning-only case.

**To enable a brand** (once the processor approves): add it to `ACCEPTED_BRANDS`
in `card-brand-guard.js` **and** `brand_guard.mjs`, and to
`enroll_core.ACCEPTED_CARD_BRANDS` — then redeploy enrollment and re-paste the
front-end snippet.

**Two services are browser-facing and need CORS.** `enrollment` (the vancecredit.com
pricing page POSTs to `/enroll`) reads **`ENROLL_CORS_ORIGINS`** — comma-separated,
default `https://vancecredit.com,https://www.vancecredit.com`, scoped to
`POST/GET/OPTIONS` + `Content-Type`, no credentials. `verdict-service` (the
`capture/movement-capture.html` form) reads **`CAPTURE_ORIGINS`** the same way
(default `*` — tighten it in production). Set each to the exact origin(s) that call
it.

**Mail class is the only difference between the two tiers** and is handled
**outside the code**: for launch, the operator sets it manually in CloudMail per
`plan_tier` — `dispute` → First Class, `complete` → Certified. `billing-api`
`/dispatch/record-round` + `/dispatch/round-status` exist only to track/prove
Certified sends (ops/audit), not to gate any charge. Lob-direct automation (set
mail class programmatically from `plan_tier`) is the documented future step.

### Configuration guardrails

**`DATABASE_URL` is validated at startup** in every DB-backed service
(`verdict-service`, `enrollment`, `billing-api`) via a shared `_dbcheck.py`
(`validate_database_url(..., required=True)`, called before any storage is
created). If the value is missing, isn't a `postgres://` / `postgresql://` DSN, or
contains an HTTP / service-path fragment (`http`, `/parser/`, `/billing/`) — the
classic mistake of pasting a service URL into `DATABASE_URL` — **the service
refuses to boot** and logs an explained error. A mispaste now fails **visibly in
the Railway deploy logs** instead of silently at first request (previously a
runtime 500). `webhooks` has no database and skips the check.

Required env vars per service:

| Service | Required | Notes |
|---|---|---|
| **verdict-service** | `DATABASE_URL` (Postgres DSN), `INTERNAL_API_KEY`* | `CAPTURE_ORIGINS` opt (CORS). |
| **enrollment** | `DATABASE_URL` (Postgres DSN), `NMI_SECURITY_KEY`, `NMI_ENDPOINT` | `ENROLL_CORS_ORIGINS`, `CRC_CREATE_CLIENT_WEBHOOK` opt. |
| **billing-api** | `DATABASE_URL` (Postgres DSN), `INTERNAL_API_KEY`* | reads `vc_clients`. |
| **webhooks** | — (stateless; no `DATABASE_URL`) | `INTERNAL_API_KEY`*, `HIGHLEVEL_WEBHOOK_URL`/`TWILIO_*`/`CRC_INVOICE_URL` opt. |

\* `INTERNAL_API_KEY` is fail-open when unset (staged rollout); set it to enforce
auth on the write endpoints. `DATABASE_URL` must be the Supabase **session-pooler**
DSN (`postgres://…:5432/…`), the same database across `enrollment` + `billing-api`
(+ `verdict-service`).

---

## 3. Gate `<<placeholder>>` wiring

The n8n gate ships with these placeholders. Now that the services exist, point
them as follows. **Do this in the n8n editor — the JSON is not edited here.**

| Placeholder | Used by gate node(s) | Point it at |
|---|---|---|
| `<<YOUR_API_BASE>>` *(verdict paths)* | `GET /parser/verdict`, `POST /parser/verdict/commit` | **verdict-service** base URL |
| `<<YOUR_API_BASE>>` *(billing paths)* | `GET /billing/due`, `GET /dispatch/round-status` | **billing-api** base URL |
| `<<NMI_SECURITY_KEY>>` | the NMI **sale** (charge) node | your NMI security key (same secret as enrollment's `NMI_SECURITY_KEY`) |
| `<<NMI_ENDPOINT>>` | the NMI **sale** node | `secure.nmi.com` |
| `<<CRC_INVOICE_WEBHOOK>>` | post-success CRC invoice | **webhooks** base + `/crc/invoice` |
| `<<NOTIFY_WEBHOOK>>` | receipt / payment_failed / skipped | **webhooks** base + `/notify` |

> ⚠️ **`<<YOUR_API_BASE>>` is one token but now spans two services.** `/parser/*`
> lives in **verdict-service**; `/billing/due` + `/dispatch/round-status` live in
> **billing-api**. Either (a) replace each occurrence with the correct service's
> base, or (b) put both services behind one gateway/host. Don't point all four
> paths at a single service.

### Two post-charge calls to ADD (manual n8n additions)

On the **successful charge** path, after the NMI sale returns approved, add:

1. **`POST <billing-api>/billing/mark-billed`** — the idempotency guard. Drops the
   client from the next `/billing/due` for this cycle.
   ```json
   { "client_id": "{{ $json.client_id }}",
     "cycle": "{{ $json.cycle }}",
     "transaction_id": "{{ $json.transaction_id }}" }
   ```
2. **`POST <verdict>/parser/verdict/commit`** — **both tiers** (both are now
   movement-billed). Marks the moved item(s) as billed so the verdict never bills
   them again. (This node may already be stubbed in the gate as "Commit Credited
   Changes" — confirm its URL points at the verdict base. It previously ran on the
   dispute branch only; it must now run for `complete` too.)
   ```json
   { "client_id": "{{ $json.client_id }}",
     "change_ids": {{ $json.credit_token || [] }},
     "transaction_id": "{{ $json.transaction_id }}" }
   ```

Both are **manual additions in the n8n editor.** They must fire only after a
**confirmed successful** charge — a declined card must leave both ledgers
untouched so the client is retried next cycle.

### Gate NMI response handling (Code node)

**n8n Code nodes run in a Node sandbox with NO browser globals.** `URLSearchParams`,
`atob`, `btoa`, `fetch`, `window`, etc. are **undefined** — referencing them throws
(`"URLSearchParams is not defined"`) and crashes the node. Parse manually instead.

**NMI returns `x-www-form-urlencoded` text.** The **NMI Vault Sale** HTTP node uses
`responseFormat: text`, so the raw body arrives on **`$json.data`** (fallback
`$json.body`). Example body:
`response=1&responsetext=Approved&authcode=..&transactionid=..&avsresponse=Y&cvvresponse=M&response_code=100`.

The **Parse NMI Response** node splits on `&`, `decodeURIComponent`s each half
(turning `+` into space), and emits these fields on `json`:

| Field | Meaning |
|---|---|
| `response` | `"1"` approved · `"2"` decline · `"3"` error |
| `responsetext` | human-readable NMI reason |
| `authcode`, `transactionid`, `response_code` | auth code, txn id, numeric code |
| `avsresponse`, `cvvresponse` | AVS / CVV result letters (fraud posture) |
| `nmi_response`, `nmi_text`, `transaction_id` | aliases the existing downstream nodes read — kept for compatibility |

**Approval is strict.** `NMI Approved?` branches only on `response === "1"`
(exposed as `nmi_response`, strict string compare). **Anything else — `"2"`, `"3"`,
or missing/empty — routes to `Payment Failed — Dunning`, never to Create CRC
Invoice / Send Receipt / Commit.** An unparseable or empty body yields
`nmi_response === ""`, which fails the strict check and routes to dunning, so a
crash-or-garbage response can **never** fall through to "billed".

**AVS/CVV are surfaced, not enforced.** `avsresponse` + `cvvresponse` are included
in the receipt, dunning, and CRC-invoice payloads so an AVS-related decline is
distinguishable in logs. We do **not** hard-block on AVS mismatch (subprime
customers often mismatch) — the vault stores the address and AVS is evaluated at
the gateway; the gate only records it.

The parser logic is unit-tested in `test/parse_nmi.test.mjs` (`npm run test:gate`),
which mirrors the exact code embedded in the node.

---

## 4. All Supabase tables (consolidated)

From every `schema.sql` across the repo:

| Table | Defined in | Written by | Read by |
|---|---|---|---|
| `vc_snapshots` | `verdict-service/schema.sql` | verdict-service | verdict-service |
| `vc_letters` | `verdict-service/schema.sql` | verdict-service | verdict-service |
| `vc_credited_changes` | `verdict-service/schema.sql` | verdict-service (on commit) | verdict-service |
| `vc_manual_movements` | `verdict-service/manual_schema.sql` | verdict-service (on manual capture) | verdict-service |
| `vc_clients` | `enrollment/clients_schema.sql` | enrollment | billing-api |
| `vc_billed_cycles` | `billing-api/dispatch_schema.sql` | billing-api (mark-billed) | billing-api |
| `vc_dispatch_rounds` | `billing-api/dispatch_schema.sql` | billing-api (record-round) | billing-api |

> **enrollment + billing-api MUST share one Supabase database** — billing-api
> reads the `vc_clients` table enrollment writes. Simplest setup: put **all seven
> tables in one Supabase database** and give all three DB-backed services the same
> `DATABASE_URL`. (verdict-service can technically use its own DB, but one shared
> database is the recommended, simplest configuration.)

Apply all four schema files once to that database:
```
verdict-service/schema.sql         -- vc_snapshots, vc_letters, vc_credited_changes
verdict-service/manual_schema.sql  -- vc_manual_movements (Path B)
enrollment/clients_schema.sql      -- vc_clients
billing-api/dispatch_schema.sql    -- vc_billed_cycles, vc_dispatch_rounds
```

---

## 5. Go-live checklist (in order)

1. **Supabase** — create one project. In the SQL editor, run, in any order:
   `verdict-service/schema.sql`, `enrollment/clients_schema.sql`,
   `billing-api/dispatch_schema.sql`. Copy the connection string → this is
   `DATABASE_URL` (use the pooler string, port `6543`, for serverless).
2. **Deploy the 4 Railway services** from this repo, each with its own **Root
   Directory** (`verdict-service`, `enrollment`, `billing-api`, `webhooks`).
   Generate a public domain for each.
3. **Set env vars** per §2:
   - verdict-service, enrollment, billing-api → `DATABASE_URL` (the same one).
   - enrollment → `NMI_SECURITY_KEY` (+ `NMI_ENDPOINT`, optional
     `CRC_CREATE_CLIENT_WEBHOOK`).
   - webhooks → `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`/`TWILIO_FROM` and
     `CRC_INVOICE_URL` (any unset → stub).
4. **Smoke-test each** `*/health` (or the verdict ghost check) returns ok.
5. **Wire the gate** (§3): set the 6 placeholder targets, splitting
   `<<YOUR_API_BASE>>` between verdict-service and billing-api.
6. **Add the two post-charge n8n nodes** (§3): `mark-billed` and `verdict/commit`,
   both for **all clients** (both tiers are movement-billed), on the
   successful-charge path.
7. **Point the pricing page** Collect.js form at `enrollment` `POST /enroll`.
8. **Dry run on one test client**: enroll → confirm `vc_clients` row → run the
   gate for the current cycle → verify a single charge, a CRC invoice line, one
   SMS, and that the client disappears from the next `/billing/due`.
9. **Schedule the gate daily** once the dry run is clean.

> **Note:** CRC confirmed (support, June 2026) that credit report item-level data
> is dashboard-only with no API — manual capture (Path B) is the deliberate
> bridge; Path A (direct data API) is the future automation.

> **Note:** mail class is set **manually in CloudMail** per `plan_tier` for launch
> (`dispute` → First Class, `complete` → Certified). Lob-direct automation (mail
> class driven programmatically from `plan_tier`) is the documented future step.
