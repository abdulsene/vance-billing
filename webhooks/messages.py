"""
CROA-safe notification copy. Pure functions — no I/O — so they're trivially
testable and the same string is what gets sent.

CROA framing: we never promise a score/outcome. Receipts state a charge happened
because the report moved; the "skipped" copy makes the free month explicit.
"""
from __future__ import annotations

from senders import _money


def receipt_copy(amount, cycle: str) -> str:
    return (f"Vance Credit: your report moved this cycle - ${_money(amount)} for {cycle}. "
            f"Details in your portal.")


def payment_failed_copy(cycle: str) -> str:
    return (f"Vance Credit: we couldn't process your card for {cycle}. "
            f"Update it in your portal to continue.")


def skipped_copy(cycle: str, plan_tier: str = "dispute", message: str = "") -> str:
    # Dispute plan: no movement => no charge (the free month).
    if plan_tier == "dispute":
        return ("Vance Credit: no movement on your report this cycle, so there's "
                "no charge. We keep working.")
    # Complete/Rapid: pass through the gate's skip_message if provided, else a
    # generic CROA-safe line (internal review, no charge).
    if message:
        return f"Vance Credit: {message}"
    return ("Vance Credit: no documented round this cycle - internal review, "
            "no charge. We keep working.")


def compose(body: dict) -> str:
    """Route a /notify body to its message by `type`."""
    t = body.get("type")
    cycle = body.get("cycle", "")
    if t == "receipt":
        return receipt_copy(body.get("amount", ""), cycle)
    if t == "payment_failed":
        return payment_failed_copy(cycle)
    if t == "skipped":
        return skipped_copy(cycle, body.get("plan_tier", "dispute"), body.get("message", ""))
    raise ValueError(f"unknown notify type: {t!r}")
