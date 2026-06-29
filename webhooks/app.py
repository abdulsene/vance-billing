"""
Vance Credit — Webhook receivers (FastAPI).

The endpoints the n8n billing gate posts to after it decides/charges:

POST /crc/invoice    <<CRC_INVOICE_WEBHOOK>> — add an invoice line to the CRC client
POST /notify         <<NOTIFY_WEBHOOK>>      — SMS the client (receipt/failed/skipped)
GET  /webhooks/health

Run:  uvicorn app:app --reload
Stateless: no DB. CRC + Twilio are reached via senders.py, which STUBS (logs +
returns ok) when their env vars are unset, so this runs in dev with no accounts.
"""
from __future__ import annotations
from typing import Literal, Optional

from fastapi import FastAPI
from pydantic import BaseModel

import senders
from messages import compose

app = FastAPI(title="Vance Credit — Webhooks")


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #

class Contact(BaseModel):
    email: str = ""
    phone: str = ""


class InvoiceIn(BaseModel):
    client_id: str
    amount: float
    plan_tier: str
    cycle: str
    transaction_id: Optional[str] = None
    description: str = ""


class NotifyIn(BaseModel):
    type: Literal["receipt", "payment_failed", "skipped"]
    client_id: str
    cycle: str
    contact: Contact
    plan_tier: str = "dispute"
    # type-specific (all optional; used by compose() as relevant)
    amount: Optional[float] = None
    reason: Optional[str] = None
    message: Optional[str] = None
    nmi_text: Optional[str] = None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/webhooks/health")
def health():
    return {"ok": True}


@app.post("/crc/invoice")
def crc_invoice(body: InvoiceIn):
    # Forward a single invoice line to CRC. NEVER attaches a plan/subscription.
    forwarded = senders.forward_invoice(body.model_dump())
    return {"ok": True, "forwarded": forwarded}


@app.post("/notify")
def notify(body: NotifyIn):
    message = compose(body.model_dump())
    sent = senders.send_sms(body.contact.phone, message)
    return {"ok": True, "channel": "sms", "sent": sent, "message": message}
