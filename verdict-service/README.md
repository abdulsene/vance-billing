# Vance Credit — Movement Verdict Service

The brain behind `/parser/verdict`. It decides whether a client's tri-bureau
report **moved** in a billing cycle — the boolean the Dispute branch of the
billing gate reads to decide whether to charge $99.

It is anchored to the **report itself** (what the customer can also see), never
to the credit score.

## What counts as "movement"

A cycle moved if there is **at least one favorable, item-level change that has
not already been billed**:

- **Deletion of a negative item** — a collection, public record, unauthorized
  inquiry, or derogatory tradeline that was on the report last cycle is gone now.
  (Losing a *healthy* account does **not** count.)
- **Status improvement** — same item, strictly healthier status (e.g. 90-days-late → current).

Bureau **response letters** are used two ways: a letter confirming a deletion
counts immediately (before the snapshot refreshes), and it supplies the
"per Experian's response" attribution line for the receipt.

**Never counts:** a "verified"/"remains" letter, a score change, new negative
items, or any unknown/uncomparable status.

## The dedup ledger (why this isn't just a diff)

A single deletion often appears twice — first when the bureau's letter says
"deleted," then again when the next monthly snapshot finally drops the item. A
naive diff would bill it twice. So every favorable change gets a stable
`change_id` (`bureau|type|creditor|account|kind|target`), and the verdict only
counts change_ids **not already in the credited ledger**. A deletion seen by
letter and later by snapshot shares one `change_id`, so it bills exactly once.

The verdict is **read-only** — it never writes the ledger. You commit only
*after the charge succeeds*, so a declined card leaves the change uncredited and
billable next cycle.

## Endpoints

| Method | Path | Who calls it |
|---|---|---|
| `POST` | `/parser/snapshot` | your tri-bureau monitoring feed, each refresh |
| `POST` | `/parser/letters` | your existing bureau-response-letter parser |
| `GET`  | `/parser/verdict?client_id=&cycle=` | the billing gate (Dispute branch) |
| `POST` | `/parser/verdict/commit` | the billing gate, **after** a successful NMI charge |

`GET /parser/verdict` returns:
```json
{ "moved": true,
  "changes": [{"change_id":"...","creditor":"ABC Collections","kind":"deletion","detail":"collection 'ABC Collections' deleted","source":"snapshot+letter"}],
  "credit_token": ["EQ|collection|abc|1234|deletion|deleted"] }
```
If no snapshot has been ingested for the cycle yet, it returns `moved:false`
(safe — you never charge without proof on the report).

## Wiring into the billing gate (one addition)

In `vance-billing-gate.n8n.json`, after a **successful Dispute charge** (the
"Send Receipt" path), add one HTTP node:

```
POST <verdict-service>/parser/verdict/commit
{ "client_id": "{{ $json.client_id }}",
  "change_ids": {{ $json.credit_token }},
  "transaction_id": "{{ $json.transaction_id }}" }
```

Carry `credit_token` through from the verdict call so it's available here. That
closes the loop: a change is credited only once a real charge clears.

## Run it

```bash
pip install fastapi uvicorn "pydantic>=2" psycopg[binary]
# local / in-memory:
uvicorn app:app --reload
# production (Supabase): apply schema.sql, then:
export DATABASE_URL=postgresql://...   # service uses PostgresStorage automatically
uvicorn app:app --host 0.0.0.0 --port 8080
```

Deploy on Railway alongside your other services; point the gate's
`<<YOUR_API_BASE>>/parser/verdict` at it.

## Tests

```bash
pip install pytest
pytest            # 16 tests: differ, classifier, dedup, and end-to-end API
```

## Files

- `verdict_core.py` — models, fingerprinting, differ, classifier, verdict (pure stdlib)
- `storage.py` — `InMemoryStorage` + `PostgresStorage`
- `app.py` — FastAPI service
- `adapters.py` — maps a monitoring feed's report JSON → `POST /parser/snapshot` (edit `FieldMap`)
- `schema.sql` — Postgres/Supabase tables
- `test_verdict_core.py`, `test_app.py` — the suite

## What still plugs into your stack

- **Snapshots:** map your monitoring feed into the `POST /parser/snapshot` shape
  using `adapters.py` — edit its `FieldMap` to your feed's field names (send a
  sample payload and it can be tailored exactly).
- **Letters:** point your existing response-letter parser at `POST /parser/letters`.
- **Status vocabulary:** `SEVERITY` / `_STATUS_ALIASES` in `verdict_core.py` cover
  common statuses; add any raw strings your feed emits that aren't mapped yet.
