"""
Vance Credit — Enrollment service (FastAPI).

Runs when a customer signs up on the pricing page. CROA-safe: it VAULTS the card
($0.00, no charge) and creates the client record in the exact shape the billing
gate and verdict service already expect. The first charge happens later, via the
gate, only if the report moved.

Endpoints
---------
POST /enroll                          vault card + create client (no charge)
GET  /enroll/health                   liveness
GET  /enroll/confirmation/{client_id} post-enrollment onboarding page (HTML)

Run:  uvicorn app:app --reload
Storage: in-memory by default; set DATABASE_URL to use Postgres/Supabase.
"""
from __future__ import annotations
import html
import json
import logging
import os
import uuid
from typing import Literal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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

# Where step 1 of onboarding sends the customer: the Credit Repair Cloud client
# portal signup. Distinct from the webhooks service's PORTAL_URL, which is the
# LOGIN url for existing clients in receipt/dunning SMS - different audience,
# different destination, deliberately not the same variable.
CRC_PORTAL_URL = os.environ.get(
    "CRC_PORTAL_URL", "https://vancecredit.getcredithelpnow.com/start")

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
        # Best-effort: delete the just-created vault entry so we keep NO record for a
        # rejected card, then reject cleanly. A delete failure must not turn this into
        # a 500 — log it for reconciliation and still return the 422.
        try:
            nmi.delete_from_vault(customer_vault_id)
        except Exception as exc:
            log.error("VAULT-ORPHAN: could not delete vault %s for rejected brand %r; "
                      "manual cleanup needed: %s", customer_vault_id, brand, exc)
        log.warning("Enrollment rejected: card brand %r not accepted "
                    "(vault %s deleted, no client created).", brand, customer_vault_id)
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

    # d2. Fire the HighLevel "New Enrollment" welcome-email webhook. Best-effort and
    #     fully non-blocking: it runs AFTER the client is durably committed, and any
    #     failure (incl. unset URL) is swallowed so it can never fail/roll back the
    #     enrollment. first_name = everything before the FIRST space of the name.
    first_name = (body.name or "").strip().split(" ", 1)[0]
    _fire_new_enrollment_webhook(
        first_name=first_name, email=body.email, phone=body.phone,
        plan_tier=client.plan_tier, client_id=client.client_id)

    # e.
    return EnrollOut(ok=True, client_id=client.client_id,
                     plan_tier=client.plan_tier, vaulted=True, charged=False)


# --------------------------------------------------------------------------- #
# Confirmation page (first step of onboarding)
# --------------------------------------------------------------------------- #
# Inline template, {{TOKEN}} placeholders filled by str.replace - no Jinja2, so the
# service keeps its current dependency footprint. Every interpolated value is
# html.escape()d at the call site: the email is customer-supplied at enrollment,
# so rendering it raw would be a stored-XSS hole.
#
# COMPLIANCE: no "guarantee"/"guaranteed" language anywhere on this page, and no
# claim about outcomes - only the billing promise (charged when the report moves).
_CONFIRMATION_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>You're enrolled &mdash; Vance Credit</title>
<style>
  :root {
    --ink: #0f172a; --muted: #6b7280; --line: #e5e7eb;
    --brand: #1d4ed8; --brand-dark: #1e40af; --ok: #047857; --wash: #f8fafc;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px 16px 64px;
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: var(--ink); background: var(--wash);
  }
  .card {
    max-width: 640px; margin: 0 auto; background: #fff;
    border: 1px solid var(--line); border-radius: 14px;
    padding: 32px 28px; box-shadow: 0 1px 3px rgba(15, 23, 42, .06);
  }
  .badge {
    display: inline-block; margin-bottom: 14px; padding: 5px 12px;
    background: #ecfdf5; color: var(--ok); border-radius: 999px;
    font-size: .82rem; font-weight: 600; letter-spacing: .02em;
  }
  h1 { margin: 0 0 10px; font-size: 1.7rem; line-height: 1.25; }
  .promise { margin: 0 0 22px; color: var(--muted); }
  .confirm {
    margin: 0 0 28px; padding: 12px 16px; background: var(--wash);
    border: 1px solid var(--line); border-radius: 10px;
  }
  .confirm .label {
    display: block; font-size: .74rem; text-transform: uppercase;
    letter-spacing: .07em; color: var(--muted); margin-bottom: 3px;
  }
  .confirm .value { font-size: 1.22rem; font-weight: 700; letter-spacing: .07em; }
  h2 { margin: 0 0 18px; font-size: 1.12rem; }
  ol.steps { margin: 0; padding: 0; list-style: none; counter-reset: step; }
  ol.steps > li {
    position: relative; counter-increment: step;
    padding: 0 0 24px 52px; border-left: 2px solid var(--line); margin-left: 15px;
  }
  ol.steps > li:last-child { border-left: 2px solid transparent; padding-bottom: 4px; }
  ol.steps > li::before {
    content: counter(step); position: absolute; left: -16px; top: -2px;
    width: 30px; height: 30px; border-radius: 50%;
    background: var(--brand); color: #fff;
    font-size: .9rem; font-weight: 700; display: grid; place-items: center;
  }
  .step-title { font-weight: 650; margin-bottom: 4px; }
  .when {
    display: inline-block; margin-left: 6px; padding: 2px 8px; border-radius: 999px;
    background: #eff6ff; color: var(--brand-dark);
    font-size: .72rem; font-weight: 600; letter-spacing: .03em; vertical-align: 1px;
  }
  .step-body { color: var(--muted); margin: 0; }
  .cta {
    display: block; margin: 14px 0 10px; padding: 15px 20px;
    background: var(--brand); color: #fff; text-decoration: none;
    border-radius: 10px; font-size: 1.04rem; font-weight: 650; text-align: center;
  }
  .cta:hover { background: var(--brand-dark); }
  .need { margin: 0; font-size: .9rem; color: var(--muted); }
  .cost { color: var(--ink); font-weight: 600; }
  footer {
    max-width: 640px; margin: 20px auto 0; padding: 0 4px;
    font-size: .9rem; color: var(--muted); text-align: center;
  }
  @media (max-width: 480px) {
    body { padding: 16px 12px 48px; }
    .card { padding: 24px 18px; border-radius: 12px; }
    h1 { font-size: 1.42rem; }
  }
</style>
</head>
<body>
  <main class="card">
    <span class="badge">Enrollment complete</span>
    <h1>You're enrolled &mdash; $0 charged today.</h1>
    <p class="promise">
      You're charged only in a cycle where your credit report improves.
    </p>

    <div class="confirm">
      <span class="label">Confirmation #</span>
      <span class="value">{{CONFIRMATION}}</span>
    </div>

    <h2>Here's exactly what happens next</h2>
    <ol class="steps">
      <li>
        <div class="step-title">Set up your secure client portal<span class="when">NOW &middot; ~5 min</span></div>
        <a class="cta" href="{{PORTAL_URL}}">Set up my client portal</a>
        <p class="need">You'll need: a photo ID and proof of address. Works on your phone.</p>
      </li>
      <li>
        <div class="step-title">Activate credit monitoring inside the portal</div>
        <p class="step-body">
          The portal will prompt you to start Credit Hero Score
          (<span class="cost">$19.99/mo</span>, billed directly by the monitoring service &mdash;
          separate from Vance Credit). This is what pulls the 3-bureau report we work from,
          so disputes can't start until it's active.
        </p>
      </li>
      <li>
        <div class="step-title">We start disputing</div>
        <p class="step-body">
          Once your portal and monitoring are active, our team begins working your file.
          You'll get updates by email and text as items move.
        </p>
      </li>
    </ol>
  </main>
  <footer>
    We've also emailed this link to {{EMAIL}}. Questions? Reply to that email.
  </footer>
</body>
</html>
"""


def confirmation_number(client_id: str) -> str:
    """Display-only confirmation number: first 8 chars of client_id, uppercased.

    Cosmetic - it is NOT a separate identifier and nothing looks clients up by it;
    client_id remains the real key.
    """
    return (client_id or "")[:8].upper()


def render_confirmation(*, client_id: str, email: str, portal_url: str) -> str:
    return (_CONFIRMATION_HTML
            .replace("{{CONFIRMATION}}", html.escape(confirmation_number(client_id)))
            .replace("{{PORTAL_URL}}", html.escape(portal_url, quote=True))
            .replace("{{EMAIL}}", html.escape(email or "your email")))


@app.get("/enroll/confirmation/{client_id}", response_class=HTMLResponse)
def confirmation(client_id: str):
    """The screen the customer lands on after a successful $0 enrollment.

    The email is looked up server-side from the same store /enroll writes to, so no
    PII rides in the query string. Unknown id -> clean 404, never a 500.
    """
    try:
        client = STORAGE.get_client(client_id)
    except Exception as exc:
        # A storage hiccup must not show a stack trace to a customer who has just
        # paid nothing but has handed over a card - fail as a clean 503.
        log.error("confirmation lookup failed for client_id=%s: %s", client_id, exc)
        raise HTTPException(status_code=503,
                            detail="Confirmation is temporarily unavailable.") from exc
    if client is None:
        raise HTTPException(status_code=404, detail="Unknown confirmation link.")
    return HTMLResponse(render_confirmation(
        client_id=client.client_id,
        email=client.contact.get("email", ""),
        portal_url=CRC_PORTAL_URL))


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


def _fire_new_enrollment_webhook(*, first_name: str, email: str, phone: str,
                                 plan_tier: str, client_id: str) -> None:
    """Best-effort POST to the HighLevel "Vance New Enrollment" inbound webhook,
    which triggers the welcome-email workflow.

    Fully non-blocking: if ENROLL_WEBHOOK_URL is unset it skips silently (debug),
    and ANY POST failure is logged (warning) and swallowed. A webhook/email hiccup
    must NEVER fail or roll back an enrollment that is already durably committed.
    """
    url = os.environ.get("ENROLL_WEBHOOK_URL")
    if not url:
        log.debug("ENROLL_WEBHOOK_URL unset — skipping new-enrollment webhook for %s", client_id)
        return
    try:
        payload = json.dumps({
            "first_name": first_name,
            "email": email,
            "phone": phone,
            "plan_tier": plan_tier,
            "client_id": client_id,
        }).encode()
        req = Request(url, data=payload, headers={"Content-Type": "application/json"})
        urlopen(req, timeout=10).read()  # noqa: S310
    except Exception as exc:
        log.warning("New-enrollment webhook failed for client_id=%s "
                    "(enrollment already succeeded): %s", client_id, exc)
