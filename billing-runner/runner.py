"""Billing runner â€” the 'gate' as tested code. Cannot 500; every failure is captured
per client+stage. Handles clients with no phone (skips SMS, never crashes). Supports a
read-only dry run that proves the path against live data without charging."""
import os, logging
from nmi import parse_nmi
log = logging.getLogger("billing-runner")

def _safe(stage, cid, fn):
    try:
        return fn(), None
    except Exception as e:
        body = ""
        try:
            r = getattr(e, "read", None)
            if r: body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        err = {"client_id": cid, "stage": stage, "error": f"{type(e).__name__}: {e}"}
        if body: err["response_body"] = body
        log.error("stage=%s client=%s FAILED: %s %s", stage, cid, err["error"], body)
        return None, err

def _notify(svc, cid, payload, phone, summary):
    """Send an SMS notify only if we have a phone; missing phone is logged, never fatal."""
    if not phone:
        log.warning("no phone for client=%s â€” SMS '%s' skipped", cid, payload.get("type"))
        summary["errors"].append({"client_id": cid, "stage": "notify",
                                  "error": "no phone on client (from /billing/due) â€” SMS skipped"})
        return
    payload = dict(payload); payload["contact"] = {"phone": phone}
    _, err = _safe("notify", cid, lambda: svc.notify(payload))
    if err: summary["errors"].append(err)

def _alert_orphan(svc, cid, cycle, txn, err, summary):
    """A charge-orphan is money taken with the cycle unrecorded - the loudest failure we have.
    Always log it; additionally page ops by SMS if ORPHAN_ALERT_PHONE is set. The alert is
    strictly best-effort: a failed alert must never raise or mask the orphan itself."""
    log.error("CHARGE-ORPHAN: client=%s cycle=%s CHARGED (txn=%s) mark_billed FAILED: %s",
              cid, cycle, txn, err["error"])
    ops = os.environ.get("ORPHAN_ALERT_PHONE")
    if not ops:
        return
    try:
        svc.notify({"type": "payment_failed", "client_id": "OPS-ALERT", "amount": 0,
                    "cycle": cycle,
                    "reason": f"CHARGE-ORPHAN client={cid} txn={txn} - reconcile now",
                    "contact": {"phone": ops}})
    except Exception as e:
        log.error("orphan alert FAILED (orphan still recorded) client=%s txn=%s: %s: %s",
                  cid, txn, type(e).__name__, e)
        summary["errors"].append({"client_id": cid, "stage": "orphan_alert",
                                  "error": f"{type(e).__name__}: {e}"})

def run_billing(svc, *, date: str, dry: bool = False) -> dict:
    summary = {"date": date, "dry_run": dry, "charged": [], "would_charge": [],
               "skipped": [], "declined": [], "orphans": [], "errors": []}

    due, err = _safe("get_due", "-", lambda: svc.get_due(date))
    if err:
        summary["errors"].append(err); summary["fatal"] = "get_due failed â€” cannot list clients"
        return summary

    for c in (due or {}).get("clients", []):
        cid = c.get("client_id"); cycle = c.get("cycle")
        vault = c.get("customer_vault_id"); amount = c.get("monthly_amount")
        phone = c.get("phone"); tier = c.get("plan_tier")

        verdict, err = _safe("get_verdict", cid, lambda: svc.get_verdict(cid, cycle))
        if err:
            summary["errors"].append(err); continue
        if not (verdict or {}).get("moved"):
            summary["skipped"].append(cid); continue

        if dry:
            summary["would_charge"].append({"client_id": cid, "cycle": cycle, "amount": amount,
                                            "vault": vault, "has_phone": bool(phone)})
            continue

        raw, err = _safe("vault_sale", cid, lambda: svc.vault_sale(vault, amount, orderid=f"{cid}|{cycle}"))
        if err:
            summary["errors"].append(err); continue
        nmi = parse_nmi(raw)

        if not nmi["approved"]:
            _notify(svc, cid, {"type": "payment_failed", "client_id": cid, "amount": amount,
                               "cycle": cycle, "reason": nmi.get("responsetext") or "declined"}, phone, summary)
            summary["declined"].append({"client_id": cid, "reason": nmi.get("responsetext"),
                                        "response_code": nmi.get("response_code"),
                                        "avs": nmi.get("avsresponse"), "cvv": nmi.get("cvvresponse"),
                                        "raw": (nmi.get("raw") or "")[:300]})
            continue

        txn = nmi.get("transactionid")
        _, err = _safe("mark_billed", cid, lambda: svc.mark_billed(cid, cycle, txn))
        if err:
            _alert_orphan(svc, cid, cycle, txn, err, summary)
            summary["orphans"].append({"client_id": cid, "cycle": cycle, "txn": txn, "error": err["error"]})
            continue

        _safe("commit", cid, lambda: svc.commit(cid, cycle))
        _safe("invoice", cid, lambda: svc.create_invoice({"client_id": cid, "amount": amount,
                                                          "cycle": cycle, "transaction_id": txn, "plan_tier": tier}))
        _notify(svc, cid, {"type": "receipt", "client_id": cid, "amount": amount,
                           "cycle": cycle, "reason": "report moved"}, phone, summary)
        summary["charged"].append({"client_id": cid, "txn": txn, "amount": amount,
                                   "avs": nmi.get("avsresponse"), "cvv": nmi.get("cvvresponse")})
    return summary
