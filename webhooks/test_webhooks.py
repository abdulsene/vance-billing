"""
Webhook tests. All outbound senders are monkeypatched — NO real network.
"""
import pytest
from fastapi.testclient import TestClient

import app as appmod
import senders
from senders import _money
from messages import compose

client = TestClient(appmod.app)


@pytest.fixture(autouse=True)
def clear_sms_env(monkeypatch):
    # Deterministic baseline: no HighLevel, no Twilio -> stub, regardless of host env.
    for var in ("HIGHLEVEL_WEBHOOK_URL", "TWILIO_ACCOUNT_SID",
                "TWILIO_AUTH_TOKEN", "TWILIO_FROM"):
        monkeypatch.delenv(var, raising=False)
    yield


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


def test_notify_receipt_routes_through_deliver_sms(monkeypatch):
    captured = {}
    monkeypatch.setattr(senders, "deliver_sms",
                        lambda to, msg: (captured.update(to=to, msg=msg) or ("highlevel", True)))
    r = client.post("/notify", json=_notify(type="receipt", amount=99, cycle="2026-06"))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "channel": "highlevel", "sent": True}
    assert captured["to"] == "+15555550123"          # routed to the contact phone
    assert "moved this cycle" in captured["msg"]      # composed copy passed through
    assert "$99" in captured["msg"]


def test_notify_skipped_not_texted(monkeypatch):
    # A "skipped" (free-month) notify must NOT attempt any SMS.
    monkeypatch.setattr(senders, "deliver_sms",
                        lambda *a, **k: pytest.fail("skipped must not attempt SMS"))
    r = client.post("/notify", json=_notify(type="skipped", plan_tier="dispute"))
    assert r.json() == {"ok": True, "channel": "none", "sent": False, "note": "skip not texted"}


def test_notify_stub_when_no_channel_configured():
    # Neither HighLevel nor Twilio configured (cleared by fixture) -> ("stub", False).
    r = client.post("/notify", json=_notify(type="receipt", amount=99))
    assert r.json() == {"ok": True, "channel": "stub", "sent": False}


def test_unknown_notify_type_rejected_422():
    r = client.post("/notify", json=_notify(type="bogus"))
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Message copy (compose) — formatting fixes
# --------------------------------------------------------------------------- #

def test_receipt_copy_has_no_emdash_and_is_ascii():
    msg = compose(_notify(type="receipt", amount=99, cycle="2026-06"))
    assert "—" not in msg                        # no em-dash (was the "â" mojibake)
    msg.encode("ascii")                               # raises if any non-ASCII byte


def test_message_types_distinct_and_worded():
    receipt = compose(_notify(type="receipt", amount=99))
    failed = compose(_notify(type="payment_failed"))
    skipped = compose(_notify(type="skipped", plan_tier="dispute"))
    assert "moved this cycle" in receipt
    assert "couldn't process your card" in failed
    assert "no movement" in skipped and "no charge" in skipped
    assert len({receipt, failed, skipped}) == 3       # all three distinct


# --------------------------------------------------------------------------- #
# deliver_sms — three-tier routing
# --------------------------------------------------------------------------- #

def test_deliver_sms_highlevel(monkeypatch):
    captured = {}
    monkeypatch.setenv("HIGHLEVEL_WEBHOOK_URL", "https://hl.example.com/webhook")
    monkeypatch.setattr(senders, "_post_json",
                        lambda url, obj, **kw: (captured.update(url=url, obj=obj) or True))
    channel, sent = senders.deliver_sms("+15555550123", "hello")
    assert (channel, sent) == ("highlevel", True)
    assert captured["obj"] == {"phone": "+15555550123", "message": "hello"}


def test_deliver_sms_twilio(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACxxx")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM", "+15555550000")
    monkeypatch.setattr(senders, "_post_form", lambda *a, **k: "ok")   # mock the Twilio POST
    channel, sent = senders.deliver_sms("+15555550123", "hello")
    assert (channel, sent) == ("twilio", True)


def test_deliver_sms_stub():
    channel, sent = senders.deliver_sms("+15555550123", "hello")       # neither configured
    assert (channel, sent) == ("stub", False)


# --------------------------------------------------------------------------- #
# _money
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("val,expected", [(99, "99"), (99.0, "99"), (149.5, "149.50")])
def test_money(val, expected):
    assert _money(val) == expected


def test_health():
    assert client.get("/webhooks/health").json() == {"ok": True}
