"""
CROA-safe notification copy. Pure functions — no I/O — so they're trivially
testable and the same string is what gets sent.

CROA framing: we never promise a score/outcome. Receipts state a charge happened
because the report improved; the "skipped" copy makes the free month explicit.

Config:
  PORTAL_URL — the client portal login URL, linked in every receipt/failure SMS.
               Set this env var to your real client portal login page.
"""
from __future__ import annotations
import os
from datetime import datetime
from math import ceil

from senders import _money

# Client portal login URL, appended to receipt / payment-failed messages.
# Set PORTAL_URL to the real client portal login page in production.
PORTAL_URL = os.environ.get("PORTAL_URL", "https://vancecredit.com")


def cycle_label(cycle: str) -> str:
    """'YYYY-MM' -> 'Month YYYY' (e.g. '2026-06' -> 'June 2026').

    Returns the raw cycle unchanged on any parse failure.
    """
    try:
        return datetime.strptime(cycle, "%Y-%m").strftime("%B %Y")
    except (ValueError, TypeError):
        return cycle


def sms_segments(text: str) -> int:
    """GSM-7 concatenated segment count: 1 if <=160 chars, else ceil(len/153)."""
    n = len(text)
    return 1 if n <= 160 else ceil(n / 153)


def receipt_copy(amount, cycle: str) -> str:
    return (f"Vance Credit: your credit report improved this cycle. True to our "
            f"promise, you're only charged when it moves - so your ${_money(amount)} "
            f"fee for {cycle_label(cycle)} was applied. See the details anytime in "
            f"your client portal: {PORTAL_URL}")


def payment_failed_copy(amount, cycle: str) -> str:
    # Degrade gracefully when the amount is missing/blank: drop the dollar figure
    # but keep the sentence grammatical ("your payment" instead of "your $X fee").
    fee = f"${_money(amount)} fee" if amount not in (None, "") else "payment"
    return (f"Vance Credit: your credit report improved this cycle, but we couldn't "
            f"process your {fee} for {cycle_label(cycle)}. Update your "
            f"card in your client portal so we can keep working on your file: {PORTAL_URL}")


def skipped_copy(cycle: str = "", plan_tier: str = "dispute", message: str = "") -> str:
    # Logged only (never texted): free-month cycle, no charge.
    return ("Vance Credit: no changes posted to your report this cycle - no charge. "
            "Your disputes continue next round.")


def compose(body: dict) -> str:
    """Route a /notify body to its message by `type`."""
    t = body.get("type")
    cycle = body.get("cycle", "")
    if t == "receipt":
        return receipt_copy(body.get("amount", ""), cycle)
    if t == "payment_failed":
        return payment_failed_copy(body.get("amount", ""), cycle)
    if t == "skipped":
        return skipped_copy(cycle, body.get("plan_tier", "dispute"), body.get("message", ""))
    raise ValueError(f"unknown notify type: {t!r}")
