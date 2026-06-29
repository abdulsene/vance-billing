"""
Enrollment API tests. NMI is monkeypatched — NO real network is ever hit.

Covers: happy-path vault+persist, NMI failure -> 402 + no client persisted,
bad plan_tier -> 422, and that the persisted record carries EXACTLY the fields
the billing gate's /billing/due reads.
"""
import re
import pytest
from fastapi.testclient import TestClient

import app as appmod
import nmi
from enroll_core import BILLING_FIELDS, PLAN_AMOUNTS, billing_view

client = TestClient(appmod.app)


@pytest.fixture(autouse=True)
def fresh_storage():
    appmod.STORAGE.clear()
    yield


@pytest.fixture
def vault_ok(monkeypatch):
    """NMI approves and returns a vault id; assert card data never leaves as PAN."""
    def fake(payment_token, **kw):
        assert payment_token == "tok_collectjs_abc"   # only the opaque token is forwarded
        return {"response": "1", "responsetext": "SUCCESS",
                "customer_vault_id": "1234567890", "response_code": "100"}
    monkeypatch.setattr(nmi, "add_to_vault", fake)


def _body(**over):
    b = {"collect_js_token": "tok_collectjs_abc", "plan_tier": "complete",
         "name": "Jane Doe", "email": "jane@example.com", "phone": "+15555550123"}
    b.update(over)
    return b


def test_happy_path_vaults_and_persists(vault_ok):
    r = client.post("/enroll", json=_body(plan_tier="complete"))
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True
    assert out["vaulted"] is True
    assert out["charged"] is False          # CROA: never charged at enrollment
    assert out["plan_tier"] == "complete"
    cid = out["client_id"]

    saved = appmod.STORAGE.get_client(cid)
    assert saved is not None
    assert saved.plan_tier == "complete"
    assert saved.monthly_amount == 149
    assert saved.customer_vault_id == "1234567890"
    assert saved.contact == {"email": "jane@example.com", "phone": "+15555550123"}
    assert saved.status == "active"
    assert re.fullmatch(r"\d{4}-\d{2}", saved.cycle)   # current YYYY-MM
    assert saved.created_at                            # stamped


@pytest.mark.parametrize("tier,amount", list(PLAN_AMOUNTS.items()))
def test_each_tier_maps_to_amount(vault_ok, tier, amount):
    r = client.post("/enroll", json=_body(plan_tier=tier))
    assert r.status_code == 200
    saved = appmod.STORAGE.get_client(r.json()["client_id"])
    assert saved.monthly_amount == amount


def test_nmi_failure_returns_402_and_persists_nothing(monkeypatch):
    def fake_decline(payment_token, **kw):
        return {"response": "3", "responsetext": "DECLINE", "customer_vault_id": ""}
    monkeypatch.setattr(nmi, "add_to_vault", fake_decline)

    r = client.post("/enroll", json=_body())
    assert r.status_code == 402
    assert appmod.STORAGE.list_clients() == []        # no client written on failure


def test_nmi_network_error_returns_402(monkeypatch):
    def boom(payment_token, **kw):
        raise OSError("connection refused")
    monkeypatch.setattr(nmi, "add_to_vault", boom)

    r = client.post("/enroll", json=_body())
    assert r.status_code == 402
    assert appmod.STORAGE.list_clients() == []


@pytest.mark.parametrize("bad_tier", ["platinum", "rapid"])  # "rapid" is retired
def test_bad_plan_tier_returns_422(vault_ok, bad_tier):
    r = client.post("/enroll", json=_body(plan_tier=bad_tier))
    assert r.status_code == 422
    assert appmod.STORAGE.list_clients() == []        # rejected before any vault/persist


def test_persisted_record_has_exactly_billing_fields(vault_ok):
    r = client.post("/enroll", json=_body())
    saved = appmod.STORAGE.get_client(r.json()["client_id"])
    view = billing_view(saved)
    # EXACTLY the fields the gate's /billing/due will read — no more, no less.
    assert set(view.keys()) == set(BILLING_FIELDS)
    assert set(view["contact"].keys()) == {"email", "phone"}


def test_health():
    assert client.get("/enroll/health").json() == {"ok": True}
