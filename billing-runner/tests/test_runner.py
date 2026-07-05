"""Full behavioral tests for the billing runner — the charge path, proven."""
import sys, os, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from runner import run_billing
from nmi import parse_nmi

APPROVED = "response=1&responsetext=SUCCESS&authcode=041233&transactionid=10598765432&avsresponse=Y&cvvresponse=M&response_code=100"
DECLINED = "response=2&responsetext=DECLINE&transactionid=999&avsresponse=N&response_code=200"
ERROR    = "response=3&responsetext=Authentication+Failed&response_code=300"

class FakeSvc:
    """Records every call so tests can assert exact ordering + idempotency."""
    def __init__(self, clients, verdict_moved=True, nmi_raw=APPROVED, fail_on=None):
        self._clients = clients
        self._moved = verdict_moved
        self._nmi = nmi_raw
        self._fail_on = fail_on or set()
        self.calls = []
    def get_due(self, date):
        self.calls.append(("get_due", date)); return {"clients": self._clients}
    def get_verdict(self, cid, cycle):
        self.calls.append(("get_verdict", cid, cycle))
        moved = self._moved(cid) if callable(self._moved) else self._moved
        return {"moved": moved, "changes": []}
    def vault_sale(self, vault, amount, *, orderid):
        self.calls.append(("vault_sale", vault, amount, orderid))
        return self._nmi(vault) if callable(self._nmi) else self._nmi
    def mark_billed(self, cid, cycle, txn):
        self.calls.append(("mark_billed", cid, cycle, txn))
        if "mark_billed" in self._fail_on: raise RuntimeError("db down")
    def commit(self, cid, cycle):
        self.calls.append(("commit", cid, cycle))
        if "commit" in self._fail_on: raise RuntimeError("commit fail")
    def create_invoice(self, payload):
        self.calls.append(("create_invoice", payload["client_id"]))
        if "invoice" in self._fail_on: raise RuntimeError("invoice fail")
    def notify(self, payload):
        self.calls.append(("notify", payload["type"], payload["client_id"]))

C = lambda cid="c1", cycle="2026-07": {"client_id": cid, "cycle": cycle,
        "customer_vault_id": "427602090", "monthly_amount": 99, "phone": "+12025732022", "plan_tier": "dispute"}

def names(svc): return [c[0] for c in svc.calls]

def test_moved_and_approved_charges_marks_commits_receipts():
    s = FakeSvc([C()], verdict_moved=True, nmi_raw=APPROVED)
    out = run_billing(s, date="2026-07-05")
    seq = names(s)
    # exact order: due -> verdict -> vault_sale -> mark_billed -> commit -> invoice -> receipt
    assert seq == ["get_due","get_verdict","vault_sale","mark_billed","commit","create_invoice","notify"], seq
    assert out["charged"] and out["charged"][0]["txn"] == "10598765432"
    # mark_billed happens immediately after the charge (no post-step between)
    assert seq.index("mark_billed") == seq.index("vault_sale") + 1
    # receipt is the charged receipt
    assert ("notify","receipt","c1") in s.calls

def test_not_moved_never_charges():
    s = FakeSvc([C()], verdict_moved=False)
    out = run_billing(s, date="2026-07-05")
    assert "vault_sale" not in names(s)          # THE core promise: no movement, no charge
    assert "mark_billed" not in names(s)
    assert out["skipped"] == ["c1"]

def test_declined_goes_to_dunning_never_marks_billed():
    s = FakeSvc([C()], verdict_moved=True, nmi_raw=DECLINED)
    out = run_billing(s, date="2026-07-05")
    assert "vault_sale" in names(s)
    assert "mark_billed" not in names(s)         # declined must NOT be recorded as billed
    assert ("notify","payment_failed","c1") in s.calls
    assert out["declined"][0]["reason"] == "DECLINE"

def test_gateway_error_goes_to_dunning():
    s = FakeSvc([C()], verdict_moved=True, nmi_raw=ERROR)
    out = run_billing(s, date="2026-07-05")
    assert "mark_billed" not in names(s)
    assert out["declined"] and out["declined"][0]["reason"] == "Authentication Failed"

def test_empty_nmi_response_never_marks_billed():
    s = FakeSvc([C()], verdict_moved=True, nmi_raw="")   # the exact crash case from n8n
    out = run_billing(s, date="2026-07-05")
    assert "mark_billed" not in names(s)                 # unreadable response cannot bill
    assert out["declined"]

def test_charge_orphan_when_mark_billed_fails():
    s = FakeSvc([C()], verdict_moved=True, nmi_raw=APPROVED, fail_on={"mark_billed"})
    out = run_billing(s, date="2026-07-05")
    # charged but mark_billed failed -> recorded as orphan, post-steps NOT run
    assert out["orphans"] and out["orphans"][0]["txn"] == "10598765432"
    assert "commit" not in names(s) and "create_invoice" not in names(s)

def test_post_charge_failures_do_not_break_billing():
    # invoice + commit fail, but charge already marked -> client still counts as charged, no re-charge
    s = FakeSvc([C()], verdict_moved=True, nmi_raw=APPROVED, fail_on={"commit","invoice"})
    out = run_billing(s, date="2026-07-05")
    assert out["charged"] and not out["orphans"]
    assert "mark_billed" in names(s)

def test_orderid_is_client_cycle_for_nmi_idempotency():
    s = FakeSvc([C("cX","2026-07")], verdict_moved=True, nmi_raw=APPROVED)
    run_billing(s, date="2026-07-05")
    vs = [c for c in s.calls if c[0]=="vault_sale"][0]
    assert vs[3] == "cX|2026-07"                 # NMI duplicate-guard key

def test_multiple_clients_mixed_outcomes():
    clients = [C("moved1"), C("still1"), C("moved2")]
    moved = lambda cid: cid in ("moved1","moved2")
    nmi = lambda vault: APPROVED
    s = FakeSvc(clients, verdict_moved=moved, nmi_raw=nmi)
    out = run_billing(s, date="2026-07-05")
    assert {x["client_id"] for x in out["charged"]} == {"moved1","moved2"}
    assert out["skipped"] == ["still1"]

def test_parser_matches_gate_fix():
    assert parse_nmi(APPROVED)["approved"] is True
    assert parse_nmi(DECLINED)["approved"] is False
    assert parse_nmi("")["approved"] is False
