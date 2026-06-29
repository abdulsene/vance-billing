"""
Vance Credit — Movement Verdict service (FastAPI).

Endpoints
---------
POST /parser/snapshot          ingest a tri-bureau snapshot (your monitoring feed)
POST /parser/letters           ingest bureau response-letter outcomes (your parser)
GET  /parser/verdict           the gate calls this: did the report move this cycle?
POST /parser/verdict/commit    the gate calls this AFTER a charge succeeds

Run:  uvicorn app:app --reload
Storage: in-memory by default; set DATABASE_URL to use Postgres/Supabase.
"""
from __future__ import annotations
import os
from typing import Optional
from fastapi import FastAPI, Query
from pydantic import BaseModel

from verdict_core import Item, Snapshot, LetterOutcome, compute_verdict
from storage import InMemoryStorage, PostgresStorage

app = FastAPI(title="Vance Credit — Movement Verdict")

_dsn = os.environ.get("DATABASE_URL")
STORAGE = PostgresStorage(_dsn) if _dsn else InMemoryStorage()


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #

class ItemIn(BaseModel):
    bureau: str
    item_type: str
    creditor: str
    account_mask: str = ""
    status: str = ""
    balance: Optional[float] = None
    past_due: Optional[float] = None


class SnapshotIn(BaseModel):
    client_id: str
    cycle: str
    items: list[ItemIn]


class LetterIn(BaseModel):
    bureau: str
    item_type: str
    creditor: str
    account_mask: str = ""
    outcome: str = ""


class LettersIn(BaseModel):
    client_id: str
    cycle: str
    outcomes: list[LetterIn]


class CommitIn(BaseModel):
    client_id: str
    change_ids: list[str]
    transaction_id: Optional[str] = None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.post("/parser/snapshot")
def ingest_snapshot(body: SnapshotIn):
    snap = Snapshot(client_id=body.client_id, cycle=body.cycle,
                    items=[Item(**i.model_dump()) for i in body.items])
    STORAGE.save_snapshot(snap)
    return {"ok": True, "items": len(snap.items)}


@app.post("/parser/letters")
def ingest_letters(body: LettersIn):
    outcomes = [LetterOutcome(**o.model_dump()) for o in body.outcomes]
    STORAGE.save_letters(body.client_id, body.cycle, outcomes)
    return {"ok": True, "outcomes": len(outcomes)}


@app.get("/parser/verdict")
def verdict(client_id: str = Query(...), cycle: str = Query(...)):
    current = STORAGE.get_snapshot(client_id, cycle)
    if current is None:
        # No report for this cycle yet -> can't prove movement -> safe skip (free month).
        return {"moved": False, "changes": [], "credit_token": [],
                "reason": "no snapshot ingested for this cycle"}
    previous = STORAGE.get_previous_snapshot(client_id, cycle)
    letters = STORAGE.get_letters(client_id, cycle)
    credited = STORAGE.get_credited(client_id)
    v = compute_verdict(client_id, cycle, current, previous, letters, credited)
    return {"moved": v.moved, "changes": v.changes, "credit_token": v.credit_token}


@app.post("/parser/verdict/commit")
def commit(body: CommitIn):
    # Call this only after the NMI charge for these change_ids has succeeded.
    STORAGE.add_credited(body.client_id, body.change_ids)
    return {"ok": True, "credited": len(body.change_ids)}
