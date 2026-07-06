"""
Vance Credit — Billing-API core (pure stdlib).

Reads the SAME client record shape the enrollment service writes to vc_clients.
The billing *cycle* for a run comes from the gate's `date` param (YYYY-MM of it),
NOT the client's enrollment cycle — the gate bills monthly, so in 2026-07 it bills
cycle '2026-07' even for a client who enrolled in '2026-05'.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime, timezone

# Exactly the fields the gate's /billing/due returns per client.
# email/phone are also exposed top-level (in addition to nested `contact`) so the
# billing-runner can read them directly for SMS receipts/dunning.
BILLING_FIELDS = ("client_id", "plan_tier", "monthly_amount",
                  "customer_vault_id", "cycle", "contact", "email", "phone")

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def cycle_of_date(date_str: str) -> str:
    """YYYY-MM-DD -> 'YYYY-MM' (the billing cycle for that date)."""
    m = _DATE_RE.match(date_str or "")
    if not m:
        raise ValueError(f"date must be YYYY-MM-DD, got {date_str!r}")
    return f"{m.group(1)}-{m.group(2)}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Client:
    """Mirror of the enrollment service's vc_clients record."""
    client_id: str
    plan_tier: str
    monthly_amount: int
    customer_vault_id: str
    cycle: str                  # enrollment cycle (informational; not the billing cycle)
    contact: dict               # {"email": ..., "phone": ...}
    status: str = "active"
    created_at: str = ""


def billing_view(client: Client, cycle: str) -> dict:
    """The gate's billing view, with `cycle` set to the CURRENT billing cycle."""
    return {
        "client_id": client.client_id,
        "plan_tier": client.plan_tier,
        "monthly_amount": client.monthly_amount,
        "customer_vault_id": client.customer_vault_id,
        "cycle": cycle,
        "contact": client.contact,
        # top-level too, so the billing-runner can read them directly for SMS.
        "email": client.contact.get("email", ""),
        "phone": client.contact.get("phone", ""),
    }
