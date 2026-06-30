"""
Vance Credit — Billing-API (FastAPI).

The two companion endpoints the n8n billing gate calls each cycle, reading the
vc_clients table the enrollment service writes.

Endpoints
---------
GET  /billing/due?date=YYYY-MM-DD     clients to bill this cycle (idempotent)
POST /billing/mark-billed             gate's idempotency guard, AFTER a charge
GET  /dispatch/round-status           did a dispute round go out this cycle?
POST /dispatch/record-round           dispatch/CRC-mail step records a sent round
GET  /billing/health                  liveness

Run:  uvicorn app:app --reload
Storage: in-memory by default; set DATABASE_URL to use Postgres/Supabase.

GATE WIRING — idempotency guard (do NOT edit the n8n json here, just know this):
After a SUCCESSFUL charge, the gate must POST /billing/mark-billed
{client_id, cycle, transaction_id}, alongside the existing verdict commit. That
insert is what makes a client drop out of the NEXT /billing/due for this cycle.
Without it, the same client would be re-billed on the next gate run.
"""
from __future__ import annotations
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Depends
from pydantic import BaseModel

from billing_core import billing_view, cycle_of_date, now_iso
from storage import InMemoryStorage, PostgresStorage
from _auth import require_api_key

app = FastAPI(title="Vance Credit — Billing API")

_dsn = os.environ.get("DATABASE_URL")
STORAGE = PostgresStorage(_dsn) if _dsn else InMemoryStorage()


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #

class MarkBilledIn(BaseModel):
    client_id: str
    cycle: str
    transaction_id: Optional[str] = None


class RecordRoundIn(BaseModel):
    client_id: str
    cycle: str
    round_id: str
    mailed_at: Optional[str] = None     # ISO timestamp; defaults to now if omitted


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/billing/health")
def health():
    return {"ok": True}


@app.get("/billing/due", dependencies=[Depends(require_api_key)])
def billing_due(date: str = Query(..., description="YYYY-MM-DD")):
    try:
        cycle = cycle_of_date(date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    due = STORAGE.get_due_clients(cycle)
    return {"clients": [billing_view(c, cycle) for c in due]}


@app.post("/billing/mark-billed", dependencies=[Depends(require_api_key)])
def mark_billed(body: MarkBilledIn):
    # Gate calls this right after a successful charge. on-conflict-do-nothing,
    # so re-calls are safe (idempotent).
    STORAGE.mark_billed(body.client_id, body.cycle, body.transaction_id)
    return {"ok": True, "client_id": body.client_id, "cycle": body.cycle}


@app.get("/dispatch/round-status", dependencies=[Depends(require_api_key)])
def round_status(client_id: str = Query(...), cycle: str = Query(...)):
    row = STORAGE.get_round(client_id, cycle)
    if row is None:
        return {"round_documented": False, "round_id": None, "mailed_at": None}
    return {"round_documented": True,
            "round_id": row["round_id"], "mailed_at": row["mailed_at"]}


@app.post("/dispatch/record-round", dependencies=[Depends(require_api_key)])
def record_round(body: RecordRoundIn):
    mailed_at = body.mailed_at or now_iso()
    STORAGE.record_round(body.client_id, body.cycle, body.round_id, mailed_at)
    return {"ok": True, "client_id": body.client_id, "cycle": body.cycle,
            "round_id": body.round_id, "mailed_at": mailed_at}
