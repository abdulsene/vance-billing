"""
NMI Customer Vault adapter — vault a card with NO charge.

We send `customer_vault=add_customer` with the Collect.js `payment_token`. This
stores the card in the NMI Customer Vault and returns a `customer_vault_id`. It
is NOT a sale and NOT an authorization for any amount — $0.00, CROA-safe.

Card data never touches our server: the browser tokenizes via Collect.js and we
only ever forward the opaque `payment_token`.

Creds come from env: NMI_SECURITY_KEY, NMI_ENDPOINT (default secure.nmi.com).
The transact endpoint speaks application/x-www-form-urlencoded in BOTH
directions, so the response is URL-encoded text we parse with parse_qs.

Network uses stdlib urllib so there's no runtime HTTP dependency. Tests
monkeypatch `add_to_vault`, so no real network is ever hit under pytest.
"""
from __future__ import annotations
import os
import urllib.parse
import urllib.request

DEFAULT_ENDPOINT = "secure.nmi.com"


def _post_form(url: str, data: dict, timeout: float = 20.0) -> str:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https only)
        return resp.read().decode("utf-8", "replace")


def add_to_vault(payment_token: str, *,
                 security_key: str | None = None,
                 endpoint: str | None = None,
                 email: str = "",
                 first_name: str = "",
                 last_name: str = "",
                 phone: str = "",
                 address1: str = "",
                 address2: str = "",
                 city: str = "",
                 state: str = "",
                 zip: str = "") -> dict:
    """
    Vault a Collect.js-tokenized card. Returns NMI's parsed URL-encoded response
    as a flat dict, e.g. {"response": "1", "customer_vault_id": "1234567890",
    "responsetext": "...", "response_code": "100"}.

    The billing address (address1/city/state/zip, etc.) is stored on the vault
    record so AVS runs on every future sale made via the customer_vault_id.

    response == "1" means approved. The caller decides what an approval/decline
    means for the HTTP status; this function only talks to NMI and parses.
    """
    security_key = security_key if security_key is not None else os.environ.get("NMI_SECURITY_KEY", "")
    endpoint = endpoint or os.environ.get("NMI_ENDPOINT", DEFAULT_ENDPOINT)
    url = f"https://{endpoint}/api/transact.php"

    payload = {
        "security_key": security_key,
        "customer_vault": "add_customer",   # vault only — no sale, no auth, $0.00
        "payment_token": payment_token,     # opaque Collect.js token
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
        # AVS billing fields — stored on the vault, evaluated at charge time.
        "address1": address1,
        "city": city,
        "state": state,
        "zip": zip,
    }
    if address2:
        payload["address2"] = address2
    raw = _post_form(url, payload)
    return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
