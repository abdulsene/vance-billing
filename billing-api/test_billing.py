"""
Billing-API tests. In-memory storage seeded with a client; no network.

Covers the gate contract: /billing/due returns active unbilled clients, the
mark-billed idempotency guard removes a client for that cycle, dispatch round
status flips false->true, and the due objects carry exactly the gate's fields.
"""
import pytest
from fastapi.testclient import TestClient

import app as appmod
from billing_core import Client, BILLING_FIELDS

client = TestClient(appmod.app)


@pytest.fixture(autouse=True)
def fresh_storage():
    # Force in-memory storage for tests (module-level STORAGE is PostgresStorage
    # under the dummy test DSN); a fresh instance also isolates each test.
    appmod.STORAGE = appmod.InMemoryStorage()
    yield


def seed(client_id="c1", status="active"):
    appmod.STORAGE.save_client(Client(
        client_id=client_id, plan_tier="complete", monthly_amount=149,
        customer_vault_id="vault_1", cycle="2026-05",
        contact={"email": "jane@example.com", "phone": "+15555550123"},
        status=status, created_at="2026-05-01T00:00:00+00:00"))


def _due(date="2026-06-15"):
    r = client.get("/billing/due", params={"date": date})
    assert r.status_code == 200
    return r.json()["clients"]


def test_due_returns_active_unbilled_client():
    seed()
    clients = _due()
    assert len(clients) == 1
    assert clients[0]["client_id"] == "c1"
    assert clients[0]["cycle"] == "2026-06"          # current cycle from `date`, not enrollment cycle
    assert clients[0]["monthly_amount"] == 149


def test_due_includes_phone_and_email_for_receipts():
    # The billing-runner needs phone (+ email) to send SMS receipts/dunning.
    seed()
    obj = _due()[0]
    # New: top-level contact fields the runner reads directly.
    assert obj["phone"] == "+15555550123"
    assert obj["email"] == "jane@example.com"
    # Additive: existing fields + nested contact are all still present.
    assert obj["client_id"] == "c1"
    assert obj["plan_tier"] == "complete"
    assert obj["monthly_amount"] == 149
    assert obj["customer_vault_id"] == "vault_1"
    assert obj["cycle"] == "2026-06"
    assert obj["contact"] == {"email": "jane@example.com", "phone": "+15555550123"}


def test_mark_billed_makes_client_drop_out_idempotency():
    seed()
    assert len(_due("2026-06-15")) == 1               # due before billing

    client.post("/billing/mark-billed",
                json={"client_id": "c1", "cycle": "2026-06", "transaction_id": "txn_1"})

    assert _due("2026-06-15") == []                   # gone for the billed cycle
    assert len(_due("2026-07-15")) == 1               # but still due next cycle


def test_mark_billed_is_idempotent_on_conflict():
    seed()
    a = client.post("/billing/mark-billed", json={"client_id": "c1", "cycle": "2026-06", "transaction_id": "txn_1"})
    b = client.post("/billing/mark-billed", json={"client_id": "c1", "cycle": "2026-06", "transaction_id": "txn_2"})
    assert a.status_code == 200 and b.status_code == 200   # second call is a no-op, not an error
    assert _due("2026-06-15") == []


def test_inactive_client_not_due():
    seed(status="cancelled")
    assert _due() == []


def test_round_status_false_then_true_after_record():
    r = client.get("/dispatch/round-status", params={"client_id": "c1", "cycle": "2026-06"})
    body = r.json()
    assert body == {"round_documented": False, "round_id": None, "mailed_at": None}

    client.post("/dispatch/record-round", json={
        "client_id": "c1", "cycle": "2026-06",
        "round_id": "R-1", "mailed_at": "2026-06-10T00:00:00+00:00"})

    body2 = client.get("/dispatch/round-status", params={"client_id": "c1", "cycle": "2026-06"}).json()
    assert body2["round_documented"] is True
    assert body2["round_id"] == "R-1"
    assert body2["mailed_at"] == "2026-06-10T00:00:00+00:00"


def test_record_round_defaults_mailed_at_when_omitted():
    client.post("/dispatch/record-round", json={"client_id": "c1", "cycle": "2026-06", "round_id": "R-9"})
    body = client.get("/dispatch/round-status", params={"client_id": "c1", "cycle": "2026-06"}).json()
    assert body["round_documented"] is True
    assert body["mailed_at"]                          # stamped automatically


def test_due_objects_match_gate_field_set_exactly():
    seed()
    obj = _due()[0]
    assert set(obj.keys()) == set(BILLING_FIELDS)
    assert set(obj["contact"].keys()) == {"email", "phone"}


def test_bad_date_returns_422():
    seed()
    assert client.get("/billing/due", params={"date": "06/2026"}).status_code == 422


def test_health():
    assert client.get("/billing/health").json() == {"ok": True}
