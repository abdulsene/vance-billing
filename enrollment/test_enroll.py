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


# --------------------------------------------------------------------------- #
# Billing address -> NMI Customer Vault (AVS) + persistence
# --------------------------------------------------------------------------- #

FULL_ADDRESS = {
    "name": "Mary Jane Smith",         # last-space split -> first "Mary Jane", last "Smith"
    "address1": "123 Main St",
    "address2": "Apt 4B",
    "city": "Austin",
    "state": "TX",
    "zip": "78701",
}


@pytest.fixture
def capture_vault(monkeypatch):
    """Capture the kwargs the app forwards to NMI add_to_vault (approves)."""
    seen = {}
    def fake(payment_token, **kw):
        seen["payment_token"] = payment_token
        seen.update(kw)
        return {"response": "1", "responsetext": "SUCCESS",
                "customer_vault_id": "1234567890", "response_code": "100"}
    monkeypatch.setattr(nmi, "add_to_vault", fake)
    return seen


def test_enroll_forwards_billing_address_to_nmi(capture_vault):
    r = client.post("/enroll", json=_body(**FULL_ADDRESS))
    assert r.status_code == 200

    # AVS billing fields forwarded to the vault request...
    assert capture_vault["address1"] == "123 Main St"
    assert capture_vault["address2"] == "Apt 4B"
    assert capture_vault["city"] == "Austin"
    assert capture_vault["state"] == "TX"
    assert capture_vault["zip"] == "78701"
    # ...and the name is split on the LAST space.
    assert capture_vault["first_name"] == "Mary Jane"
    assert capture_vault["last_name"] == "Smith"

    # ...and persisted on the client record.
    saved = appmod.STORAGE.get_client(r.json()["client_id"])
    assert saved.billing == FULL_ADDRESS


def test_nmi_payload_includes_avs_fields(monkeypatch):
    # Exercise the real add_to_vault wire format: mock only the HTTP post.
    captured = {}
    monkeypatch.setattr(nmi, "_post_form",
                        lambda url, data, **kw: captured.update(data=data) or
                        "response=1&customer_vault_id=999&responsetext=OK")
    resp = nmi.add_to_vault("tok_abc", first_name="Mary Jane", last_name="Smith",
                            email="j@example.com", phone="+15555550123",
                            address1="123 Main St", address2="Apt 4B",
                            city="Austin", state="TX", zip="78701")
    assert resp["customer_vault_id"] == "999"
    data = captured["data"]
    assert data["customer_vault"] == "add_customer"      # vault, not a sale
    assert data["address1"] == "123 Main St"
    assert data["address2"] == "Apt 4B"
    assert data["city"] == "Austin"
    assert data["state"] == "TX"
    assert data["zip"] == "78701"
    assert data["first_name"] == "Mary Jane" and data["last_name"] == "Smith"


def test_postgres_insert_includes_address_fields(monkeypatch):
    # Verify the vc_clients INSERT carries the address columns + values, without a DB.
    from storage import PostgresStorage
    from enroll_core import Client

    class _FakeConn:
        def __init__(self, sink): self.sink = sink
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            self.sink.append((sql, params))
            class _Cur:
                def fetchone(self): return None
                def fetchall(self): return []
            return _Cur()

    sink = []
    st = object.__new__(PostgresStorage)          # skip __init__ (psycopg import)
    monkeypatch.setattr(st, "_conn", lambda: _FakeConn(sink))

    st.save_client(Client(
        client_id="c1", plan_tier="dispute", monthly_amount=99,
        customer_vault_id="v1", cycle="2026-07",
        contact={"email": "j@example.com", "phone": "+15555550123"},
        billing=dict(FULL_ADDRESS)))

    sql, params = sink[0]
    for col in ("name", "address1", "address2", "city", "state", "zip"):
        assert col in sql
    for value in ("Mary Jane Smith", "123 Main St", "Apt 4B", "Austin", "TX", "78701"):
        assert value in params


def test_enroll_without_address_still_succeeds(vault_ok):
    # Backward compatible: no address fields at all -> still vaults + persists.
    r = client.post("/enroll", json=_body())     # _body has no address keys
    assert r.status_code == 200
    assert r.json()["vaulted"] is True
    saved = appmod.STORAGE.get_client(r.json()["client_id"])
    assert saved.billing == {"name": "Jane Doe", "address1": "", "address2": "",
                             "city": "", "state": "", "zip": ""}


def test_health():
    assert client.get("/enroll/health").json() == {"ok": True}
