"""
Vance Credit — Enrollment service (FastAPI).

Runs when a customer signs up on the pricing page. CROA-safe: it VAULTS the card
($0.00, no charge) and creates the client record in the exact shape the billing
gate and verdict service already expect. The first charge happens later, via the
gate, only if the report moved.

Endpoints
---------
POST /enroll          vault card + create client (no charge)
GET  /enroll/health   liveness

Run:  uvicorn app:app --reload
Storage: in-memory by default; set DATABASE_URL to use Postgres/Supabase.
"""
from __future__ import annotations
import os
import uuid
from typing import Literal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import nmi
from enroll_core import Client, amount_for_plan, current_cycle
from storage import InMemoryStorage, PostgresStorage

app = FastAPI(title="Vance Credit — Enrollment")

_dsn = os.environ.get("DATABASE_URL")
STORAGE = PostgresStorage(_dsn) if _dsn else InMemoryStorage()


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #

class EnrollIn(BaseModel):
    collect_js_token: str
    # Literal -> unknown tiers (including the retired "rapid") are rejected at
    # parse time with 422.
    plan_tier: Literal["dispute", "complete"]
    name: str
    email: str
    phone: str = ""


class EnrollOut(BaseModel):
    ok: bool
    client_id: str
    plan_tier: str
    vaulted: bool
    charged: bool


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/enroll/health")
def health():
    return {"ok": True}


@app.post("/enroll", response_model=EnrollOut)
def enroll(body: EnrollIn):
    # a. plan_tier already validated by the Literal above; map to amount.
    monthly_amount = amount_for_plan(body.plan_tier)

    # b. NMI: vault the tokenized card with NO charge ($0.00). Card never hits us.
    first, _, last = body.name.partition(" ")
    try:
        resp = nmi.add_to_vault(
            body.collect_js_token,
            email=body.email, first_name=first, last_name=last)
    except Exception as exc:  # network / NMI unreachable
        raise HTTPException(status_code=402,
                            detail=f"vault request failed: {exc}") from exc

    if resp.get("response") != "1" or not resp.get("customer_vault_id"):
        raise HTTPException(
            status_code=402,
            detail=f"card vaulting declined: {resp.get('responsetext', 'unknown error')}")
    customer_vault_id = resp["customer_vault_id"]

    # c. Create + persist the client record. NOTHING charged.
    client = Client(
        client_id=uuid.uuid4().hex,
        plan_tier=body.plan_tier,
        monthly_amount=monthly_amount,
        customer_vault_id=customer_vault_id,
        cycle=current_cycle(),
        contact={"email": body.email, "phone": body.phone},
        status="active",
    )
    STORAGE.save_client(client)

    # d. STUB — push the client to CRC (Credit Repair Cloud). Best-effort, never
    #    blocks enrollment, and DOES NOT attach any CRC billing plan/subscription
    #    (billing is owned by our gate, not CRC). Replace the webhook + payload to
    #    match CRC's real create-client API when wiring it up.
    _push_to_crc_stub(client)

    # e.
    return EnrollOut(ok=True, client_id=client.client_id,
                     plan_tier=client.plan_tier, vaulted=True, charged=False)


def _push_to_crc_stub(client: Client) -> None:
    """<<CRC_CREATE_CLIENT_WEBHOOK>> placeholder — create the CRC client only.

    No billing plan, no subscription. Failures are swallowed so a CRC hiccup
    never blocks (or double-bills) enrollment.
    """
    webhook = os.environ.get("CRC_CREATE_CLIENT_WEBHOOK")
    if not webhook:
        return  # not configured yet — skip silently in dev/tests
    payload = urlencode({
        "client_id": client.client_id,
        "email": client.contact.get("email", ""),
        "phone": client.contact.get("phone", ""),
        "plan_tier": client.plan_tier,
        # NOTE: intentionally NO amount / plan / subscription fields.
    }).encode()
    try:
        req = Request(webhook, data=payload,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
        urlopen(req, timeout=10).read()  # noqa: S310
    except Exception:
        pass  # stub: log-and-continue in real impl
