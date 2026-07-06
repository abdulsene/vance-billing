"""
Enrollment API tests. NMI is monkeypatched — NO real network is ever hit.

Covers: happy-path vault+persist, NMI failure -> 402 + no client persisted,
bad plan_tier -> 422, and that the persisted record carries EXACTLY the fields
the billing gate's /billing/due reads.
"""
import json
import re
import pytest
from fastapi.testclient import TestClient

import app as appmod
import nmi
from enroll_core import BILLING_FIELDS, PLAN_AMOUNTS, billing_view

client = TestClient(appmod.app)


@pytest.fixture(autouse=True)
def fresh_storage():
    # Force in-memory storage for tests (module-level STORAGE is PostgresStorage
    # under the dummy test DSN); a fresh instance also isolates each test.
    appmod.STORAGE = appmod.InMemoryStorage()
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


def test_vault_decline_response_2_returns_clean_402(monkeypatch):
    # NMI declined the card (response "2") — clean 402, exact reason surfaced, nothing saved.
    def fake(payment_token, **kw):
        return {"response": "2", "response_code": "200",
                "responsetext": "DECLINE", "customer_vault_id": ""}
    monkeypatch.setattr(nmi, "add_to_vault", fake)

    r = client.post("/enroll", json=_body())
    assert r.status_code == 402
    detail = r.json()["detail"]
    assert "Card could not be stored" in detail
    assert "DECLINE" in detail                            # the NMI responsetext is surfaced
    assert appmod.STORAGE.list_clients() == []            # no client row saved


def test_vault_error_response_3_returns_clean_402(monkeypatch):
    # NMI gateway error (response "3"), e.g. host-mismatched Collect.js token.
    def fake(payment_token, **kw):
        return {"response": "3", "response_code": "300",
                "responsetext": "Invalid payment token", "customer_vault_id": ""}
    monkeypatch.setattr(nmi, "add_to_vault", fake)

    r = client.post("/enroll", json=_body())
    assert r.status_code == 402
    assert "Invalid payment token" in r.json()["detail"]
    assert appmod.STORAGE.list_clients() == []


# --------------------------------------------------------------------------- #
# Card-brand backstop (Visa/Mastercard only; Amex/Discover OFF)
# --------------------------------------------------------------------------- #

def _approved_with_brand(brand):
    def fake(payment_token, **kw):
        r = {"response": "1", "responsetext": "SUCCESS",
             "customer_vault_id": "1234567890", "response_code": "100"}
        if brand is not None:
            r["cc_type"] = brand
        return r
    return fake


@pytest.mark.parametrize("brand", ["Amex", "American Express", "Discover", "Diners", "JCB"])
def test_enroll_rejects_unsupported_brand_422(monkeypatch, brand):
    deleted = []
    monkeypatch.setattr(nmi, "add_to_vault", _approved_with_brand(brand))
    monkeypatch.setattr(nmi, "delete_from_vault",
                        lambda vid, **kw: deleted.append(vid) or {"response": "1"})

    r = client.post("/enroll", json=_body())
    assert r.status_code == 422
    assert r.json()["detail"] == f"Card type not accepted: {brand}. Please use Visa or Mastercard."
    assert appmod.STORAGE.list_clients() == []          # no client created for a blocked brand
    assert deleted == ["1234567890"]                    # vault entry cleaned up, not kept


def test_enroll_rejected_brand_still_422_if_vault_delete_fails(monkeypatch):
    # A failed cleanup must NOT turn the rejection into a 500 — still a clean 422.
    monkeypatch.setattr(nmi, "add_to_vault", _approved_with_brand("Amex"))
    def boom(vid, **kw):
        raise OSError("NMI unreachable")
    monkeypatch.setattr(nmi, "delete_from_vault", boom)

    r = client.post("/enroll", json=_body())
    assert r.status_code == 422
    assert appmod.STORAGE.list_clients() == []


@pytest.mark.parametrize("brand", ["Visa", "visa", "MasterCard", "mastercard"])
def test_enroll_allows_visa_mastercard(monkeypatch, brand):
    monkeypatch.setattr(nmi, "add_to_vault", _approved_with_brand(brand))
    r = client.post("/enroll", json=_body())
    assert r.status_code == 200
    assert len(appmod.STORAGE.list_clients()) == 1


def test_enroll_proceeds_when_brand_absent(monkeypatch):
    # $0 vaults often omit cc_type — we can't block pre-charge, so enrollment
    # proceeds (front-end guard + runner decline are the net). Backward compatible.
    monkeypatch.setattr(nmi, "add_to_vault", _approved_with_brand(None))
    r = client.post("/enroll", json=_body())
    assert r.status_code == 200
    assert len(appmod.STORAGE.list_clients()) == 1


def test_is_accepted_brand_unit():
    from enroll_core import is_accepted_brand
    for ok in ("Visa", "visa", "VISA", "MasterCard", "mastercard", "master card", "mc"):
        assert is_accepted_brand(ok) is True
    for bad in ("Amex", "American Express", "Discover", "JCB", "", None):
        assert is_accepted_brand(bad) is False


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
# Post-vault DB failure (VAULT-ORPHAN) + DSN-shape check
# --------------------------------------------------------------------------- #

def test_save_client_db_failure_returns_503_without_leaking(vault_ok, monkeypatch):
    # Card is vaulted, THEN save_client throws (e.g. misconfigured DSN). Must be a
    # clean 503 — never a 500 — and must not leak the DSN or the psycopg error.
    def boom(client):
        raise RuntimeError(
            "psycopg.ProgrammingError: relation vc_clients does not exist; "
            "dsn=postgresql://user:secret@db.host:5432/postgres")
    monkeypatch.setattr(appmod.STORAGE, "save_client", boom)

    r = client.post("/enroll", json=_body())
    assert r.status_code == 503
    assert r.status_code != 500
    detail = r.json()["detail"]
    assert "Your card was not charged" in detail
    assert "psycopg" not in detail                 # no raw error class
    assert "postgresql://" not in detail           # no DSN
    assert "secret" not in detail                  # no credentials
    assert appmod.STORAGE.list_clients() == []     # nothing persisted


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


# --------------------------------------------------------------------------- #
# CORS (browser-facing /enroll)
# --------------------------------------------------------------------------- #

def test_cors_preflight_allows_vancecredit_origin():
    # OPTIONS preflight from the pricing page origin must be allowed.
    r = client.options("/enroll", headers={
        "Origin": "https://vancecredit.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == "https://vancecredit.com"


def test_cors_preflight_blocks_unknown_origin():
    # A disallowed origin gets no allow-origin header (browser blocks the request).
    r = client.options("/enroll", headers={
        "Origin": "https://evil.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert r.headers.get("access-control-allow-origin") is None


# --------------------------------------------------------------------------- #
# New-enrollment webhook (HighLevel welcome email) — non-blocking, best-effort
# --------------------------------------------------------------------------- #

def test_enroll_fires_new_enrollment_webhook(vault_ok, monkeypatch):
    monkeypatch.setenv("ENROLL_WEBHOOK_URL", "https://hl.example.com/new-enrollment")
    captured = {}
    def fake_urlopen(req, *a, **k):
        captured["url"] = req.full_url
        captured["data"] = req.data
        class _R:
            def read(self): return b""
        return _R()
    monkeypatch.setattr(appmod, "urlopen", fake_urlopen)

    r = client.post("/enroll", json=_body(name="Mary Jane Smith"))
    assert r.status_code == 200
    cid = r.json()["client_id"]

    assert captured["url"] == "https://hl.example.com/new-enrollment"
    sent = json.loads(captured["data"])
    assert sent == {
        "first_name": "Mary",                 # everything before the FIRST space
        "email": "jane@example.com",
        "phone": "+15555550123",
        "plan_tier": "complete",
        "client_id": cid,
    }


def test_enroll_succeeds_even_if_webhook_raises(vault_ok, monkeypatch):
    # Non-blocking: a webhook failure must NEVER fail or roll back the enrollment.
    monkeypatch.setenv("ENROLL_WEBHOOK_URL", "https://hl.example.com/new-enrollment")
    def boom(req, *a, **k):
        raise OSError("HighLevel unreachable")
    monkeypatch.setattr(appmod, "urlopen", boom)

    r = client.post("/enroll", json=_body())
    assert r.status_code == 200
    assert len(appmod.STORAGE.list_clients()) == 1      # client still durably committed


def test_enroll_skips_webhook_when_url_unset(vault_ok, monkeypatch):
    monkeypatch.delenv("ENROLL_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(appmod, "urlopen",
                        lambda *a, **k: pytest.fail("must not POST when ENROLL_WEBHOOK_URL unset"))
    r = client.post("/enroll", json=_body())
    assert r.status_code == 200
    assert len(appmod.STORAGE.list_clients()) == 1


def test_health():
    assert client.get("/enroll/health").json() == {"ok": True}
