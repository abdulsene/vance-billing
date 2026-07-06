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
import logging
import os
import uuid
from typing import Literal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import nmi
from enroll_core import Client, amount_for_plan, current_cycle, is_accepted_brand
from storage import InMemoryStorage, PostgresStorage
from _dbcheck import validate_database_url

log = logging.getLogger("vance.enrollment")

app = FastAPI(title="Vance Credit — Enrollment")

# Startup sanity: Collect.js tokens are host-specific. If NMI_ENDPOINT is unset or
# still the generic secure.nmi.com, it very likely does NOT match the gateway host
# that served Collect.js, and every vault call will be rejected. Make it loud.
_nmi_endpoint = os.environ.get("NMI_ENDPOINT", "secure.nmi.com")
if not os.environ.get("NMI_ENDPOINT") or _nmi_endpoint == "secure.nmi.com":
    log.warning(
        "NMI_ENDPOINT is %s — Collect.js tokens are host-specific; this must match "
        "the gateway host that served Collect.js (e.g. ecrypt.transactiongateway.com) "
        "or vault calls will be rejected.", _nmi_endpoint)

# Browser-facing: the vancecredit.com pricing page POSTs to /enroll cross-origin.
# Restrict ENROLL_CORS_ORIGINS to the pricing-page origin(s) in production; the
# default already scopes it to the apex + www vancecredit.com hosts.
_DEFAULT_CORS = "https://vancecredit.com,https://www.vancecredit.com"
_cors_origins = [o.strip() for o in
                 os.environ.get("ENROLL_CORS_ORIGINS", _DEFAULT_CORS).split(",")
                 if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
)

_dsn = os.environ.get("DATABASE_URL")
# Fail fast on a mispasted DSN (e.g. a service URL) at boot, before any storage —
# turns today's silent 500-at-first-enrollment into an explained startup crash.
validate_database_url(_dsn, required=True)
STORAGE = PostgresStorage(_dsn) if _dsn else InMemoryStorage()


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #

class EnrollIn(BaseModel):
    collect_js_token: str
    # Literal -> unknown tiers (including the retired "rapid") are rejected at
    # parse time with 422.
    plan_tier: Literal["dispute", "complete"]
    email: str
    phone: str = ""
    # Billing name + address for NMI Customer Vault AVS. Optional in the schema so
    # older/partial payloads still enroll (we do NOT hard-reject on AVS here — a
    # mismatch is evaluated at charge time), but name/address1/city/state/zip are
    # normally present. address2 is genuinely optional.
    name: str = ""
    address1: str = ""
    address2: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""


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
    #    Pass the billing address so AVS data is stored on the vault record and
    #    evaluated on every future sale via customer_vault_id.
    first, last = _split_name(body.name)
    try:
        resp = nmi.add_to_vault(
            body.collect_js_token,
            email=body.email, phone=body.phone,
            first_name=first, last_name=last,
            address1=body.address1, address2=body.address2,
            city=body.city, state=body.state, zip=body.zip)
    except Exception as exc:  # network / NMI unreachable
        log.warning("NMI vault request failed (unreachable): %s", exc)
        raise HTTPException(status_code=402,
                            detail=f"vault request failed: {exc}") from exc

    # NMI response: "1"=approved, "2"=declined, "3"=error. Anything but an approval
    # with a vault id is a clean 402 — never an unhandled 500. Log the exact NMI
    # reason for ops; never leak the security key or the raw request.
    response = resp.get("response")
    customer_vault_id = resp.get("customer_vault_id")
    if response != "1" or not customer_vault_id:
        responsetext = resp.get("responsetext", "unknown error")
        log.warning("NMI vault declined/error: response=%s response_code=%s responsetext=%r",
                    response, resp.get("response_code"), responsetext)
        raise HTTPException(status_code=402,
                            detail=f"Card could not be stored: {responsetext}")

    # Brand backstop. The front end blocks Amex/Discover before submit, but if NMI
    # reports a card type outside {Visa, Mastercard}, reject cleanly (422) and do
    # NOT create a client. NOTE: a $0 add_customer vault often does NOT return a
    # brand (cc_type is a sale/auth field), so this only fires when a brand is
    # present; when it's absent the front-end guard + the runner's "payment type
    # not accepted" decline (→ ops follow-up) are the safety net. Amex/Discover are
    # OFF pending processor approval — see enroll_core.ACCEPTED_CARD_BRANDS.
    brand = resp.get("cc_type") or resp.get("card_type") or ""
    if brand and not is_accepted_brand(brand):
        log.warning("Enrollment rejected: card brand %r not accepted "
                    "(vault %s left unbound, no client created).", brand, customer_vault_id)
        raise HTTPException(
            status_code=422,
            detail=f"Card type not accepted: {brand}. Please use Visa or Mastercard.")

    # c. Create + persist the client record. NOTHING charged.
    client = Client(
        client_id=uuid.uuid4().hex,
        plan_tier=body.plan_tier,
        monthly_amount=monthly_amount,
        customer_vault_id=customer_vault_id,
        cycle=current_cycle(),
        contact={"email": body.email, "phone": body.phone},
        billing={"name": body.name, "address1": body.address1,
                 "address2": body.address2, "city": body.city,
                 "state": body.state, "zip": body.zip},
        status="active",
    )
    # The card is ALREADY vaulted at NMI by this point. If persistence fails we have
    # an orphaned vault entry (no client row) — a money-adjacent integrity event.
    # Log it loudly + greppably for reconciliation, and return a clean 503 (never a
    # 500, and never leak the DSN or raw psycopg error to the browser).
    try:
        STORAGE.save_client(client)
    except Exception as exc:
        log.error(
            "VAULT-ORPHAN: card vaulted (customer_vault_id=%s) but save_client failed "
            "for client_id=%s; manual reconciliation needed: %s",
            customer_vault_id, client.client_id, exc)
        raise HTTPException(
            status_code=503,
            detail="Enrollment is temporarily unavailable. Your card was not charged; "
                   "please try again shortly.") from exc

    # d. STUB — push the client to CRC (Credit Repair Cloud). Best-effort, never
    #    blocks enrollment, and DOES NOT attach any CRC billing plan/subscription
    #    (billing is owned by our gate, not CRC). Replace the webhook + payload to
    #    match CRC's real create-client API when wiring it up.
    _push_to_crc_stub(client)

    # e.
    return EnrollOut(ok=True, client_id=client.client_id,
                     plan_tier=client.plan_tier, vaulted=True, charged=False)


def _split_name(name: str) -> tuple[str, str]:
    """Split on the LAST space: ('first everything before', 'last remainder').

    No space -> the whole string goes in last_name. Examples:
      "Mary Jane Smith" -> ("Mary Jane", "Smith")
      "Jane Doe"        -> ("Jane", "Doe")
      "Cher"            -> ("", "Cher")
    """
    name = (name or "").strip()
    if " " in name:
        first, _, last = name.rpartition(" ")
        return first, last
    return "", name


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
