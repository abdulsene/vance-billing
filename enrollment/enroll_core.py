"""
Vance Credit — Enrollment core (pure stdlib, no FastAPI/NMI here).

Plan-tier rules, the client record shape, and the *billing view* — the exact
subset the billing gate's `/billing/due` reads off each client.

CROA note: enrollment never charges. We only vault the card. `monthly_amount`
is what the gate will charge *later*, per cycle, if the verdict says it moved.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

# plan_tier -> monthly_amount (USD, charged later by the gate, not at enrollment).
# Two movement-billed tiers; they differ only in mail class (handled outside code):
#   dispute  = $99/cycle, First Class mail   |   complete = $149/cycle, Certified mail
PLAN_AMOUNTS: dict[str, int] = {"dispute": 99, "complete": 149}

# The fields the billing gate reads off a client via /billing/due.
BILLING_FIELDS = ("client_id", "plan_tier", "monthly_amount",
                  "customer_vault_id", "cycle", "contact")


class UnknownPlanError(ValueError):
    """Raised when plan_tier is not one of PLAN_AMOUNTS."""


def amount_for_plan(plan_tier: str) -> int:
    if plan_tier not in PLAN_AMOUNTS:
        raise UnknownPlanError(plan_tier)
    return PLAN_AMOUNTS[plan_tier]


def current_cycle() -> str:
    """Current billing cycle as YYYY-MM (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Client:
    client_id: str
    plan_tier: str
    monthly_amount: int
    customer_vault_id: str
    cycle: str
    contact: dict                      # {"email": ..., "phone": ...}
    # Billing address captured at enrollment, stored for the record. It is also
    # sent to the NMI Customer Vault so AVS runs on every future charge; it is NOT
    # part of the gate's billing_view. {name, address1, address2, city, state, zip}.
    billing: dict = field(default_factory=dict)
    status: str = "active"
    created_at: str = field(default_factory=now_iso)

    def as_dict(self) -> dict:
        return asdict(self)


def billing_view(client: Client) -> dict:
    """The EXACT shape the gate's /billing/due returns per client."""
    return {
        "client_id": client.client_id,
        "plan_tier": client.plan_tier,
        "monthly_amount": client.monthly_amount,
        "customer_vault_id": client.customer_vault_id,
        "cycle": client.cycle,
        "contact": client.contact,
    }
