"""Billing runner — the entire 'gate' as tested code. No n8n, no browser globals."""
import logging
from nmi import parse_nmi
log = logging.getLogger("billing-runner")

def run_billing(svc, *, date: str) -> dict:
    """svc = a Services adapter (real or fake). Returns a per-client outcome summary.

    Invariants:
      * A client is charged ONLY if verdict.moved is true.
      * mark_billed is called IMMEDIATELY after a successful charge (minimize re-charge window).
      * A non-approved / empty NMI response NEVER reaches mark_billed -> goes to dunning.
      * If mark_billed fails after a successful charge, we log CHARGE-ORPHAN (loud) for
        reconciliation and do NOT continue post-steps.
      * Post-charge steps (commit/invoice/receipt) are best-effort; their failure cannot
        cause a re-charge because mark_billed already recorded the cycle.
    """
    summary = {"date": date, "charged": [], "skipped": [], "declined": [], "orphans": []}
    due = svc.get_due(date) or {}
    for c in due.get("clients", []):
        cid = c["client_id"]; cycle = c["cycle"]
        vault = c.get("customer_vault_id"); amount = c.get("monthly_amount")
        phone = c.get("phone"); tier = c.get("plan_tier")

        verdict = svc.get_verdict(cid, cycle) or {}
        if not verdict.get("moved"):
            summary["skipped"].append(cid)
            continue  # no movement, no charge — and we don't text "skipped"

        raw = svc.vault_sale(vault, amount, orderid=f"{cid}|{cycle}")
        nmi = parse_nmi(raw)
        if not nmi["approved"]:
            svc.notify({"type": "payment_failed", "client_id": cid, "amount": amount,
                        "cycle": cycle, "reason": nmi.get("responsetext") or "declined",
                        "contact": {"phone": phone}})
            summary["declined"].append({"client_id": cid, "reason": nmi.get("responsetext"),
                                        "avs": nmi.get("avsresponse")})
            continue

        txn = nmi.get("transactionid")
        try:
            svc.mark_billed(cid, cycle, txn)
        except Exception as e:
            log.error("CHARGE-ORPHAN: client=%s cycle=%s CHARGED (txn=%s) but mark_billed FAILED "
                      "— reconcile before next run to avoid re-charge: %s", cid, cycle, txn, e)
            summary["orphans"].append({"client_id": cid, "cycle": cycle, "txn": txn})
            continue

        # best-effort post-charge (cannot cause re-charge; cycle already recorded)
        for stage, fn in (
            ("commit",  lambda: svc.commit(cid, cycle)),
            ("invoice", lambda: svc.create_invoice({"client_id": cid, "amount": amount,
                                                    "cycle": cycle, "transaction_id": txn,
                                                    "plan_tier": tier})),
            ("receipt", lambda: svc.notify({"type": "receipt", "client_id": cid, "amount": amount,
                                            "cycle": cycle, "reason": "report moved",
                                            "contact": {"phone": phone}})),
        ):
            try:
                fn()
            except Exception as e:
                log.warning("post-charge '%s' failed for client=%s (charge OK, no re-charge): %s", stage, cid, e)

        summary["charged"].append({"client_id": cid, "txn": txn, "amount": amount,
                                   "avs": nmi.get("avsresponse"), "cvv": nmi.get("cvvresponse")})
    return summary
