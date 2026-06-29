"""
Webhook tests. CRC + Twilio senders are monkeypatched — NO real network.
"""
import pytest
from fastapi.testclient import TestClient

import app as appmod
import senders

client = TestClient(appmod.app)


# --------------------------------------------------------------------------- #
# /crc/invoice
# --------------------------------------------------------------------------- #

INVOICE = {"client_id": "c1", "amount": 199, "plan_tier": "complete",
           "cycle": "2026-06", "transaction_id": "txn_1",
           "description": "Vance Credit complete - documented round"}


def test_crc_invoice_forwards_when_url_set(monkeypatch):
    captured = {}
    def fake_post(url, data, **kw):
        captured["url"] = url; captured["data"] = data
        return "ok"
    monkeypatch.setenv("CRC_INVOICE_URL", "https://crc.example.com/invoice")
    monkeypatch.setattr(senders, "_post_form", fake_post)

    r = client.post("/crc/invoice", json=INVOICE)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "forwarded": True}
    # never sends a plan/subscription field
    assert not any(k in captured["data"] for k in ("plan", "subscription", "plan_tier", "recurring"))


def test_crc_invoice_stubs_when_url_unset(monkeypatch):
    monkeypatch.delenv("CRC_INVOICE_URL", raising=False)
    # if it tried to POST, this would blow up — proves the stub path is taken
    monkeypatch.setattr(senders, "_post_form",
                        lambda *a, **k: pytest.fail("must not POST when CRC_INVOICE_URL unset"))
    r = client.post("/crc/invoice", json=INVOICE)
    assert r.json() == {"ok": True, "forwarded": False}


# --------------------------------------------------------------------------- #
# /notify
# --------------------------------------------------------------------------- #

def _notify(**over):
    b = {"type": "receipt", "client_id": "c1", "cycle": "2026-06",
         "contact": {"email": "j@example.com", "phone": "+15555550123"},
         "plan_tier": "dispute", "amount": 99}
    b.update(over)
    return b


@pytest.fixture
def capture_sms(monkeypatch):
    sent = []
    monkeypatch.setattr(senders, "send_sms",
                        lambda to, msg: (sent.append((to, msg)) or True))
    return sent


def test_notify_receipt_message(capture_sms):
    r = client.post("/notify", json=_notify(type="receipt", amount=99, cycle="2026-06"))
    assert r.status_code == 200
    body = r.json()
    assert body["channel"] == "sms" and body["sent"] is True
    assert "moved this cycle" in body["message"]
    assert "$99" in body["message"] and "2026-06" in body["message"]
    assert capture_sms[0][0] == "+15555550123"          # routed to the contact phone


def test_notify_payment_failed_message(capture_sms):
    r = client.post("/notify", json=_notify(type="payment_failed", cycle="2026-06"))
    msg = r.json()["message"]
    assert "couldn't process your card" in msg
    assert "2026-06" in msg


def test_notify_skipped_dispute_message(capture_sms):
    r = client.post("/notify", json=_notify(type="skipped", plan_tier="dispute"))
    msg = r.json()["message"]
    assert "no movement" in msg and "no charge" in msg


def test_payment_failed_and_skipped_differ(capture_sms):
    failed = client.post("/notify", json=_notify(type="payment_failed")).json()["message"]
    skipped = client.post("/notify", json=_notify(type="skipped", plan_tier="dispute")).json()["message"]
    assert failed != skipped


def test_each_type_routes_to_distinct_copy(capture_sms):
    msgs = {t: client.post("/notify", json=_notify(
                type=t, plan_tier="dispute", amount=99)).json()["message"]
            for t in ("receipt", "payment_failed", "skipped")}
    assert len(set(msgs.values())) == 3                 # all three distinct


def test_unknown_notify_type_rejected_422(capture_sms):
    r = client.post("/notify", json=_notify(type="bogus"))
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Twilio stub path
# --------------------------------------------------------------------------- #

def test_twilio_unset_returns_ok_via_stub(monkeypatch):
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(senders, "_post_form",
                        lambda *a, **k: pytest.fail("must not POST when Twilio env unset"))
    r = client.post("/notify", json=_notify(type="receipt", amount=99))
    body = r.json()
    assert body["ok"] is True
    assert body["sent"] is False                        # stubbed, but still ok


def test_health():
    assert client.get("/webhooks/health").json() == {"ok": True}
