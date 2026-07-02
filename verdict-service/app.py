"""
Vance Credit — Movement Verdict service (FastAPI). [Path B update]

Endpoints
---------
POST /parser/snapshot         ingest a tri-bureau snapshot (Path A / automated)
POST /parser/letters          ingest bureau response-letter outcomes
POST /parser/manual-movement  reviewer-confirmed changes at CRC re-import (Path B)
GET  /parser/verdict          the gate calls this: did the report move this cycle?
POST /parser/verdict/commit   the gate calls this AFTER a charge succeeds

Run:  uvicorn app:app --reload
Storage: in-memory by default; set DATABASE_URL to use Postgres/Supabase.
"""
from __future__ import annotations
import os
from typing import Optional, Literal
from fastapi import FastAPI, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from verdict_core import Item, Snapshot, LetterOutcome, compute_verdict
from storage import InMemoryStorage, PostgresStorage
from _auth import require_api_key
from _dbcheck import validate_database_url

app = FastAPI(title="Vance Credit — Movement Verdict")

# Allow the browser capture form to call this service. Restrict CAPTURE_ORIGINS
# to your capture page's origin in production instead of "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CAPTURE_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

_dsn = os.environ.get("DATABASE_URL")
validate_database_url(_dsn, required=True)   # fail fast on a mispasted DSN, before any storage
STORAGE = PostgresStorage(_dsn) if _dsn else InMemoryStorage()


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


class ManualEntryIn(BaseModel):
    bureau: str
    item_type: str
    creditor: str
    account_mask: str = ""
    kind: Literal["deletion", "status_improvement"]
    target: str = ""           # new status for a status_improvement (e.g. "current")


class ManualMovementIn(BaseModel):
    client_id: str
    cycle: str
    entries: list[ManualEntryIn]


class CommitIn(BaseModel):
    client_id: str
    change_ids: list[str]
    transaction_id: Optional[str] = None


@app.post("/parser/snapshot", dependencies=[Depends(require_api_key)])
def ingest_snapshot(body: SnapshotIn):
    snap = Snapshot(client_id=body.client_id, cycle=body.cycle,
                    items=[Item(**i.model_dump()) for i in body.items])
    STORAGE.save_snapshot(snap)
    return {"ok": True, "items": len(snap.items)}


@app.post("/parser/letters", dependencies=[Depends(require_api_key)])
def ingest_letters(body: LettersIn):
    outcomes = [LetterOutcome(**o.model_dump()) for o in body.outcomes]
    STORAGE.save_letters(body.client_id, body.cycle, outcomes)
    return {"ok": True, "outcomes": len(outcomes)}


@app.post("/parser/manual-movement", dependencies=[Depends(require_api_key)])
def ingest_manual(body: ManualMovementIn):
    entries = [e.model_dump() for e in body.entries]
    STORAGE.save_manual(body.client_id, body.cycle, entries)
    return {"ok": True, "entries": len(entries)}


@app.get("/parser/verdict", dependencies=[Depends(require_api_key)])
def verdict(client_id: str = Query(...), cycle: str = Query(...)):
    current = STORAGE.get_snapshot(client_id, cycle)
    letters = STORAGE.get_letters(client_id, cycle)
    manual = STORAGE.get_manual(client_id, cycle)
    if current is None and not letters and not manual:
        return {"moved": False, "changes": [], "credit_token": [],
                "reason": "no snapshot, letters, or manual movement for this cycle"}
    previous = STORAGE.get_previous_snapshot(client_id, cycle)
    credited = STORAGE.get_credited(client_id)
    v = compute_verdict(client_id, cycle, current, previous, letters, credited, manual=manual)
    return {"moved": v.moved, "changes": v.changes, "credit_token": v.credit_token}


@app.post("/parser/verdict/commit", dependencies=[Depends(require_api_key)])
def commit(body: CommitIn):
    STORAGE.add_credited(body.client_id, body.change_ids)
    return {"ok": True, "credited": len(body.change_ids)}
