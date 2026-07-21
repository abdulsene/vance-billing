"""Behavioral tests for the billing runner â€” proves the charge path and every failure mode."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from runner import run_billing
from nmi import parse_nmi

APPROVED = "response=1&responsetext=SUCCESS&authcode=041233&transactionid=10598765432&avsresponse=Y&cvvresponse=M&response_code=100"
DECLINED = "response=2&responsetext=DECLINE&transactionid=999&avsresponse=N&response_code=200"
ERROR    = "response=3&responsetext=Authentication+Failed&response_code=300"

class FakeSvc:
    def __init__(self, clients, verdict_moved=True, nmi_raw=APPROVED, fail_on=None):
        self._clients=clients; self._moved=verdict_moved; self._nmi=nmi_raw
        self._fail_on=fail_on or set(); self.calls=[]
    def get_due(self, date): self.calls.append(("get_due",date)); return {"clients":self._clients}
    def get_verdict(self, cid, cycle):
        self.calls.append(("get_verdict",cid,cycle))
        return {"moved": self._moved(cid) if callable(self._moved) else self._moved}
    def vault_sale(self, vault, amount, *, orderid):
        self.calls.append(("vault_sale",vault,amount,orderid))
        return self._nmi(vault) if callable(self._nmi) else self._nmi
    def mark_billed(self, cid, cycle, txn):
        self.calls.append(("mark_billed",cid,cycle,txn))
        if "mark_billed" in self._fail_on: raise RuntimeError("db down")
    def commit(self, cid, cycle): self.calls.append(("commit",cid,cycle))
    def create_invoice(self, p): self.calls.append(("create_invoice",p["client_id"]))
    def notify(self, p):
        self.calls.append(("notify",p["type"],p["client_id"]))
        if "notify" in self._fail_on: raise RuntimeError("HTTP 422")

def C(cid="c1", cycle="2026-07", phone="+12025732022"):
    return {"client_id":cid,"cycle":cycle,"customer_vault_id":"427602090",
            "monthly_amount":99,"phone":phone,"plan_tier":"dispute"}
def names(s): return [c[0] for c in s.calls]

def test_moved_approved_charges_marks_commits_receipts():
    s=FakeSvc([C()]); out=run_billing(s,date="2026-07-05")
    assert names(s)==["get_due","get_verdict","vault_sale","mark_billed","commit","create_invoice","notify"]
    assert out["charged"][0]["txn"]=="10598765432"
    assert names(s).index("mark_billed")==names(s).index("vault_sale")+1
    assert ("notify","receipt","c1") in s.calls

def test_not_moved_never_charges():
    s=FakeSvc([C()],verdict_moved=False); out=run_billing(s,date="2026-07-05")
    assert "vault_sale" not in names(s) and out["skipped"]==["c1"]

def test_declined_dunning_never_marks_billed():
    s=FakeSvc([C()],nmi_raw=DECLINED); out=run_billing(s,date="2026-07-05")
    assert "mark_billed" not in names(s)
    assert ("notify","payment_failed","c1") in s.calls
    assert out["declined"][0]["reason"]=="DECLINE"

def test_gateway_error_dunning():
    s=FakeSvc([C()],nmi_raw=ERROR); out=run_billing(s,date="2026-07-05")
    assert "mark_billed" not in names(s) and out["declined"][0]["reason"]=="Authentication Failed"

def test_empty_response_never_bills():
    s=FakeSvc([C()],nmi_raw=""); out=run_billing(s,date="2026-07-05")
    assert "mark_billed" not in names(s) and out["declined"]

def test_charge_orphan_on_mark_billed_fail():
    s=FakeSvc([C()],fail_on={"mark_billed"}); out=run_billing(s,date="2026-07-05")
    assert out["orphans"][0]["txn"]=="10598765432" and "commit" not in names(s)

def test_orderid_idempotency_key():
    s=FakeSvc([C("cX","2026-07")]); run_billing(s,date="2026-07-05")
    assert [c for c in s.calls if c[0]=="vault_sale"][0][3]=="cX|2026-07"

def test_multiple_clients_mixed():
    s=FakeSvc([C("m1"),C("s1"),C("m2")],verdict_moved=lambda cid: cid in ("m1","m2"))
    out=run_billing(s,date="2026-07-05")
    assert {x["client_id"] for x in out["charged"]}=={"m1","m2"} and out["skipped"]==["s1"]

# ---- error capture ----
def test_verdict_failure_captured_not_fatal():
    class Boom(FakeSvc):
        def get_verdict(self,cid,cycle): self.calls.append(("get_verdict",cid,cycle)); raise RuntimeError("502")
    s=Boom([C("c1"),C("c2")]); out=run_billing(s,date="2026-07-05")
    assert len(out["errors"])==2 and out["charged"]==[]

def test_get_due_failure_reported():
    class Boom(FakeSvc):
        def get_due(self,date): raise RuntimeError("billing-api down")
    out=run_billing(Boom([]),date="2026-07-05")
    assert out.get("fatal") and out["errors"][0]["stage"]=="get_due"

def test_vault_sale_error_never_bills():
    class Boom(FakeSvc):
        def vault_sale(self,v,a,*,orderid): raise RuntimeError("timeout")
    s=Boom([C("c1")]); out=run_billing(s,date="2026-07-05")
    assert out["errors"][0]["stage"]=="vault_sale" and "mark_billed" not in names(s)

def test_declined_with_failing_notify_still_clean():
    s=FakeSvc([C()],nmi_raw=DECLINED,fail_on={"notify"}); out=run_billing(s,date="2026-07-05")
    assert out["declined"] and "mark_billed" not in names(s)
    assert any(e["stage"]=="notify" for e in out["errors"])

# ---- null-phone (the real bug behind the 422) ----
def test_no_phone_skips_sms_never_crashes_charge_still_happens():
    s=FakeSvc([C(phone=None)]); out=run_billing(s,date="2026-07-05")
    assert out["charged"]                        # charge still succeeds
    assert "notify" not in names(s)              # SMS skipped, no null-phone sent
    assert any("no phone" in e.get("error","") for e in out["errors"])

def test_no_phone_declined_still_records_decline():
    s=FakeSvc([C(phone=None)],nmi_raw=DECLINED); out=run_billing(s,date="2026-07-05")
    assert out["declined"] and "notify" not in names(s)   # no null-phone dunning => no 422

# ---- dry run (read-only, proves path without charging) ----
def test_dry_run_reports_would_charge_and_never_touches_money():
    s=FakeSvc([C("m1"),C("s1")],verdict_moved=lambda cid: cid=="m1")
    out=run_billing(s,date="2026-07-05",dry=True)
    assert out["dry_run"] is True
    assert out["would_charge"][0]["client_id"]=="m1"
    assert out["skipped"]==["s1"]
    assert "vault_sale" not in names(s)          # NO charge
    assert "mark_billed" not in names(s)         # NO writes
    assert out["charged"]==[]

# ---- charge-orphan ops alert (r7) ----
def test_orphan_pages_ops_when_alert_phone_set(monkeypatch):
    monkeypatch.setenv("ORPHAN_ALERT_PHONE", "+12025550147")
    s=FakeSvc([C()],fail_on={"mark_billed"}); out=run_billing(s,date="2026-07-05")
    assert ("notify","payment_failed","OPS-ALERT") in s.calls      # ops paged
    assert out["orphans"][0]["txn"]=="10598765432"                 # orphan still recorded
    assert "commit" not in names(s)                                # post-steps still skipped

def test_orphan_recorded_without_alert_phone(monkeypatch):
    monkeypatch.delenv("ORPHAN_ALERT_PHONE", raising=False)
    s=FakeSvc([C()],fail_on={"mark_billed"}); out=run_billing(s,date="2026-07-05")
    assert out["orphans"][0]["txn"]=="10598765432"                 # orphan still recorded
    assert not [c for c in s.calls if c[0]=="notify"]              # no alert attempted

def test_failing_orphan_alert_never_masks_the_orphan(monkeypatch):
    monkeypatch.setenv("ORPHAN_ALERT_PHONE", "+12025550147")
    s=FakeSvc([C()],fail_on={"mark_billed","notify"}); out=run_billing(s,date="2026-07-05")
    assert out["orphans"][0]["txn"]=="10598765432"                 # alert blew up, orphan survives
    assert any(e["stage"]=="orphan_alert" for e in out["errors"])

def test_parser_sanity():
    assert parse_nmi(APPROVED)["approved"] and not parse_nmi("")["approved"]
