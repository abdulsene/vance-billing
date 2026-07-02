"""
Outbound senders for the webhook service — CRC invoice forward + Twilio SMS.

Both use stdlib urllib (no twilio SDK, no requests). Both degrade to a clearly
logged STUB when their env vars are unset, so the service runs end-to-end in dev
without any external account. Tests monkeypatch these functions, so no real
network is ever hit under pytest.
"""
from __future__ import annotations
import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("vance.webhooks")

TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def _post_form(url: str, data: dict, *, headers: dict | None = None,
               auth: tuple[str, str] | None = None, timeout: float = 15.0) -> str:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {})
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if auth:
        import base64
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", "replace")


def _post_json(url: str, obj: dict, *, timeout: float = 15.0) -> bool:
    """POST obj as JSON. Returns True on a 2xx response, False on any failure."""
    body = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except Exception as exc:  # network error, non-2xx (HTTPError), timeout, ...
        log.warning("JSON POST to %s failed: %s", url, exc)
        return False


def _money(a) -> str:
    """Render a dollar amount without trailing '.0': 99.0 -> '99', 149.5 -> '149.50'.

    Total-safe: a missing/blank amount (None or "") renders as "" so no message
    path can 500 on it.
    """
    if a is None or a == "":
        return ""
    a = float(a)
    return f"{a:.0f}" if a == int(a) else f"{a:.2f}"


def forward_invoice(payload: dict) -> bool:
    """
    Forward a per-cycle invoice line to CRC's add-invoice-to-client action.

    Adds a single invoice/charge record to the existing client. It does NOT
    create or attach any CRC subscription/billing plan — billing is owned by our
    gate, not CRC. Returns True if forwarded, False if stubbed (CRC_INVOICE_URL
    unset).
    """
    url = os.environ.get("CRC_INVOICE_URL")
    if not url:
        log.info("[STUB] CRC invoice (CRC_INVOICE_URL unset) would forward: %s", payload)
        return False
    _post_form(url, {
        "client_id": payload.get("client_id", ""),
        "amount": payload.get("amount", ""),
        "description": payload.get("description", ""),
        "cycle": payload.get("cycle", ""),
        "transaction_id": payload.get("transaction_id", ""),
        # NOTE: intentionally NO plan / subscription / recurring fields.
    })
    return True


def send_sms(to_phone: str, message: str) -> bool:
    """
    Send an SMS via Twilio. Returns True if sent, False if stubbed (Twilio env
    unset). Stub logs the exact message it WOULD send.
    """
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_ = os.environ.get("TWILIO_FROM")
    if not (sid and token and from_):
        log.info("[STUB] Twilio unset — would SMS %s: %s", to_phone, message)
        return False
    _post_form(TWILIO_API.format(sid=sid),
               {"To": to_phone, "From": from_, "Body": message},
               auth=(sid, token))
    return True


def send_via_highlevel(to_phone: str, message: str) -> bool:
    """
    Forward an SMS to the Marketing Hub (HighLevel) inbound webhook, which maps
    phone -> contact and message -> Send SMS from the verified toll-free number.
    Returns True on a 2xx, False if unconfigured or on failure.
    """
    url = os.environ.get("HIGHLEVEL_WEBHOOK_URL")
    if not url:
        log.info("[STUB] HIGHLEVEL_WEBHOOK_URL unset — would forward SMS to %s: %s",
                 to_phone, message)
        return False
    return _post_json(url, {"phone": to_phone, "message": message})


def deliver_sms(to_phone: str, message: str) -> tuple[str, bool]:
    """
    Route an SMS through the three-tier chain and report which channel handled it:
      HighLevel (if HIGHLEVEL_WEBHOOK_URL set) -> Twilio (if TWILIO_* set) -> stub.
    Returns (channel, sent).
    """
    if os.environ.get("HIGHLEVEL_WEBHOOK_URL"):
        return ("highlevel", send_via_highlevel(to_phone, message))
    sent = send_sms(to_phone, message)   # Twilio when TWILIO_* set, else stub
    return ("twilio", sent) if sent else ("stub", False)
