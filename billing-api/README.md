# Vance Credit — Billing API

The two companion endpoints the n8n billing gate calls each cycle. It reads the
`vc_clients` table the **enrollment** service writes (same shape, same DB) and
owns two new tables for billing idempotency and dispatch tracking.

## Endpoints

| Method | Path | Who calls it |
|---|---|---|
| `GET`  | `/billing/due?date=YYYY-MM-DD` | the gate, once per cycle, to get clients to bill |
| `POST` | `/billing/mark-billed` | the gate, **after a successful charge** (idempotency guard) |
| `GET`  | `/dispatch/round-status?client_id=&cycle=` | the gate (Complete/Rapid branch) |
| `POST` | `/dispatch/record-round` | your dispatch system / CRC mail step, when a round goes out |
| `GET`  | `/billing/health` | liveness |

### `GET /billing/due`
Returns `{ "clients": [ ... ] }`, each item the gate's billing view:
```json
{ "client_id": "...", "plan_tier": "complete", "monthly_amount": 199,
  "customer_vault_id": "...", "cycle": "2026-06",
  "contact": { "email": "...", "phone": "..." } }
```
`cycle` is **YYYY-MM of the `date` param** (the current billing cycle), not the
client's enrollment cycle — the gate bills monthly. Only `status='active'`
clients whose `(client_id, current_cycle)` is **not** in `vc_billed_cycles` are
returned.

### `GET /dispatch/round-status`
`{ "round_documented": bool, "round_id": ..., "mailed_at": ... }`. If no round
row exists for that `(client_id, cycle)` → `round_documented: false`.

## ⚠️ Gate wiring — the idempotency guard (one addition)

**This is the critical piece.** `/billing/due` only excludes a client once a row
exists in `vc_billed_cycles`. So after a **successful charge**, the gate must
record it:

```
POST <billing-api>/billing/mark-billed
{ "client_id": "{{ $json.client_id }}",
  "cycle": "{{ $json.cycle }}",
  "transaction_id": "{{ $json.transaction_id }}" }
```

Add this in the gate workflow **right after a successful charge**, alongside the
existing verdict commit (`/parser/verdict/commit`). Without it, the same client
is re-billed on the next gate run. The insert is `on conflict do nothing`, so
duplicate calls are safe. *(This note flags the change; the `n8n` JSON is not
modified here.)*

## Storage

`Protocol` + `InMemoryStorage` (tests/local) + `PostgresStorage` (Supabase),
mirroring the verdict/enrollment services. It **reads** `vc_clients` and **owns**
`vc_billed_cycles` + `vc_dispatch_rounds` (see `dispatch_schema.sql`).

## Run it

```bash
pip install -r requirements.txt
uvicorn app:app --reload                       # in-memory
# production: apply dispatch_schema.sql to the SAME DB as enrollment, then:
export DATABASE_URL=postgresql://...
uvicorn app:app --host 0.0.0.0 --port 8080
```

## Tests

```bash
pytest            # due/idempotency, dispatch round status, exact field set
```
