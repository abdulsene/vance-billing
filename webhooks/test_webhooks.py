"""
Webhook tests. All outbound senders are monkeypatched — NO real network.
"""
import re
import pytest
from fastapi.testclient import TestClient

import app as appmod
import senders
import messages
from senders import _money
from messages import compose, cycle_label, sms_segments

client = TestClient(appmod.app)


@pytest.fixture(autouse=True)
def clear_sms_env(monkeypatch):
    # Deterministic baseline: no HighLevel, no Twilio -> stub, regardless of host env.
    for var in ("HIGHLEVEL_WEBHOOK_URL", "TWILIO_ACCOUNT_SID",
                "TWILIO_AUTH_TOKEN", "TWILIO_FROM"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def portal(monkeypatch):
    # Pin the portal link so rendered copy is deterministic.
    monkeypatch.setattr(messages, "PORTAL_URL", "https://portal.example.com")
    return "https://portal.example.com"


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
    assert not any(k in captured["data"] for k in ("plan", "subscription", "plan_tier", "recurring"))


def test_crc_invoice_stubs_when_url_unset(monkeypatch):
    monkeypatch.delenv("CRC_INVOICE_URL", raising=False)
    monkeypatch.setattr(senders, "_post_form",
                        lambda *a, **k: pytest.fail("must not POST when CRC_INVOICE_URL unset"))
    r = client.post("/crc/invoice", json=INVOICE)
    assert r.json() == {"ok": True, "forwarded": False}


# --------------------------------------------------------------------------- #
# /notify handler
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
    assert "improved this cycle" in captured["msg"]   # composed copy passed through
    assert "$99" in captured["msg"]


def test_notify_skipped_not_texted(monkeypatch):
    # A "skipped" (free-month) notify must NOT attempt any SMS.
    monkeypatch.setattr(senders, "deliver_sms",
                        lambda *a, **k: pytest.fail("skipped must not attempt SMS"))
    r = client.post("/notify", json=_notify(type="skipped", plan_tier="dispute"))
    assert r.json() == {"ok": True, "channel": "none", "sent": False, "note": "skip not texted"}


def test_notify_stub_when_no_channel_configured():
    r = client.post("/notify", json=_notify(type="receipt", amount=99))
    assert r.json() == {"ok": True, "channel": "stub", "sent": False}


def test_unknown_notify_type_rejected_422():
    r = client.post("/notify", json=_notify(type="bogus"))
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Message copy — EXACT rendered strings
# --------------------------------------------------------------------------- #

RECEIPT_EXACT = (
    "Vance Credit: your credit report improved this cycle. True to our "
    "promise, you're only charged when it moves - so your $99 fee for "
    "June 2026 was applied. See the details anytime in your client portal: "
    "https://portal.example.com"
)

PAYMENT_FAILED_EXACT = (
    "Vance Credit: your credit report improved this cycle, but we couldn't "
    "process your $99 fee for June 2026. Update your card in your client "
    "portal so we can keep working on your file: https://portal.example.com"
)


def test_receipt_exact_string(portal):
    rendered = compose(_notify(type="receipt", amount=99, cycle="2026-06"))
    print("\nRECEIPT:", rendered)
    assert rendered == RECEIPT_EXACT


def test_payment_failed_exact_string(portal):
    rendered = compose(_notify(type="payment_failed", amount=99, cycle="2026-06"))
    print("\nPAYMENT_FAILED:", rendered)
    assert rendered == PAYMENT_FAILED_EXACT


PAYMENT_FAILED_NO_AMOUNT_EXACT = (
    "Vance Credit: your credit report improved this cycle, but we couldn't "
    "process your payment for June 2026. Update your card in your client "
    "portal so we can keep working on your file: https://portal.example.com"
)

SKIPPED_EXACT = ("Vance Credit: no changes posted to your report this cycle - no charge. "
                 "Your disputes continue next round.")


def test_payment_failed_without_amount_is_graceful(portal):
    # No amount key at all -> "your payment" instead of "your $X fee", still grammatical.
    rendered = compose({"type": "payment_failed", "cycle": "2026-06"})
    print("\nPAYMENT_FAILED (no amount):", rendered)
    assert rendered == PAYMENT_FAILED_NO_AMOUNT_EXACT
    assert "$" not in rendered            # no dollar figure when amount is absent
    assert "  " not in rendered           # no double spaces
    assert sms_segments(rendered) <= 2


def test_skipped_independent_of_amount_and_cycle():
    # Logged-only, but must NEVER raise even with no amount and no cycle.
    rendered = compose({"type": "skipped"})
    assert rendered == SKIPPED_EXACT


# --------------------------------------------------------------------------- #
# cycle_label
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("cycle,label", [
    ("2026-06", "June 2026"),
    ("2026-12", "December 2026"),
    ("garbage", "garbage"),
])
def test_cycle_label(cycle, label):
    assert cycle_label(cycle) == label


# --------------------------------------------------------------------------- #
# Hygiene for EVERY message type
# --------------------------------------------------------------------------- #

def _all_messages():
    return {
        "receipt": compose(_notify(type="receipt", amount=99, cycle="2026-06")),
        "payment_failed": compose(_notify(type="payment_failed", amount=99, cycle="2026-06")),
        "skipped": compose(_notify(type="skipped", cycle="2026-06")),
    }


def test_every_message_is_clean(portal):
    for name, msg in _all_messages().items():
        msg.encode("ascii")                              # pure ASCII (raises otherwise)
        assert "—" not in msg, f"{name} has an em-dash"
        assert "  " not in msg, f"{name} has a double space"
        # "$" must sit immediately before digits, never after them ("99$")
        assert re.search(r"\d\$", msg) is None, f"{name} has a digit-then-$"
        assert sms_segments(msg) <= 2, f"{name} exceeds 2 SMS segments"


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

@pytest.mark.parametrize("val,expected", [
    (99, "99"), (99.0, "99"), (149.5, "149.50"),
    ("", ""), (None, ""),                         # total-safe: blank/missing -> ""
])
def test_money(val, expected):
    assert _money(val) == expected


def test_health():
    assert client.get("/webhooks/health").json() == {"ok": True}
