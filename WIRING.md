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

---

## 2. Services

| Service | Railway root dir | Env vars | Supabase tables (own / read) |
|---|---|---|---|
| **verdict-service** | `verdict-service` | `DATABASE_URL`, `CAPTURE_ORIGINS` *(opt, CORS; default `*`)* | **owns** `vc_snapshots`, `vc_letters`, `vc_credited_changes`, `vc_manual_movements` |
| **enrollment** | `enrollment` | `DATABASE_URL`, `NMI_SECURITY_KEY`, `NMI_ENDPOINT` *(default secure.nmi.com)*, `CRC_CREATE_CLIENT_WEBHOOK` *(opt)* | **owns/writes** `vc_clients` |
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

**Mail class is the only difference between the two tiers** and is handled
**outside the code**: for launch, the operator sets it manually in CloudMail per
`plan_tier` — `dispute` → First Class, `complete` → Certified. `billing-api`
`/dispatch/record-round` + `/dispatch/round-status` exist only to track/prove
Certified sends (ops/audit), not to gate any charge. Lob-direct automation (set
mail class programmatically from `plan_tier`) is the documented future step.

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
