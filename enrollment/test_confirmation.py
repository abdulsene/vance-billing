"""Tests for the post-enrollment confirmation page (first step of onboarding).

Covers: renders for a real client, carries the CRC portal link and the derived
confirmation number, looks the email up server-side (no PII in the query string),
404s cleanly on an unknown id, and contains no prohibited claim language.
"""
import re
import pytest
from fastapi.testclient import TestClient

import app as appmod
from enroll_core import Client

client = TestClient(appmod.app)

# Compliance: these must never appear on a customer-facing page.
BANNED_WORDS = ["guarantee", "guaranteed"]

CID = "484e1aff9c2b4d6e8f0a1b2c3d4e5f60"
EMAIL = "jane@example.com"


@pytest.fixture(autouse=True)
def fresh_storage():
    appmod.STORAGE = appmod.InMemoryStorage()
    yield


def _save(client_id=CID, email=EMAIL):
    appmod.STORAGE.save_client(Client(
        client_id=client_id, plan_tier="complete", monthly_amount=149,
        customer_vault_id="1234567890", cycle="2026-07",
        contact={"email": email, "phone": "+15555550123"}))
    return client_id


def _get(client_id=CID):
    return client.get(f"/enroll/confirmation/{client_id}")


# ---- renders ---------------------------------------------------------------

def test_page_renders_html_for_a_real_client():
    _save()
    r = _get()
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<!doctype html>" in r.text.lower()
    assert "You're enrolled" in r.text


def test_page_shows_the_headline_and_zero_charged():
    _save()
    body = _get().text
    assert "$0 charged today" in body
    assert "Confirmation #" in body


def test_page_restates_the_billing_promise():
    _save()
    assert "only in a cycle where your credit report improves" in _get().text


# ---- confirmation number ---------------------------------------------------

def test_page_contains_the_derived_confirmation_number():
    _save()
    assert "484E1AFF" in _get().text


def test_confirmation_number_is_first_8_uppercased():
    assert appmod.confirmation_number(CID) == "484E1AFF"
    assert appmod.confirmation_number("abcdef1234567890") == "ABCDEF12"
    assert appmod.confirmation_number("") == ""


def test_full_client_id_is_not_displayed_as_the_confirmation_number():
    # The id is in the URL, but the visible number stays the short derived form.
    _save()
    body = _get().text
    assert CID.upper() not in body


# ---- portal link -----------------------------------------------------------

def test_page_contains_the_crc_portal_url():
    _save()
    assert appmod.CRC_PORTAL_URL in _get().text


def test_default_crc_portal_url_is_the_signup_link():
    assert appmod.CRC_PORTAL_URL == "https://vancecredit.getcredithelpnow.com/start"


def test_portal_url_is_configurable(monkeypatch):
    _save()
    monkeypatch.setattr(appmod, "CRC_PORTAL_URL", "https://portal.example.com/start")
    body = _get().text
    assert "https://portal.example.com/start" in body
    assert "getcredithelpnow" not in body


def test_page_links_the_portal_as_a_real_anchor():
    _save()
    assert re.search(r'<a[^>]+href="https://vancecredit\.getcredithelpnow\.com/start"',
                     _get().text)


# ---- the three steps -------------------------------------------------------

def test_page_lists_all_three_onboarding_steps():
    _save()
    body = _get().text
    assert "Set up your secure client portal" in body
    assert "photo ID and proof of address" in body
    assert "Activate credit monitoring" in body
    assert "Credit Hero Score" in body
    assert "$19.99/mo" in body
    assert "We start disputing" in body


def test_monitoring_is_disclosed_as_separately_billed():
    # It is billed by the monitoring service, not Vance - must be unambiguous.
    body = _get(_save()).text
    assert "billed directly by the monitoring service" in body
    assert "separate from Vance Credit" in body


# ---- email footer ----------------------------------------------------------

def test_footer_shows_the_email_looked_up_server_side():
    _save(email="jane@example.com")
    assert "We've also emailed this link to jane@example.com" in _get().text


def test_email_is_not_taken_from_the_query_string():
    # PII must come from storage, not the URL - an injected address is ignored.
    _save(email="real@example.com")
    body = client.get(f"/enroll/confirmation/{CID}?email=attacker@evil.test").text
    assert "real@example.com" in body
    assert "attacker@evil.test" not in body


def test_email_is_html_escaped():
    # Email is customer-supplied at enrollment; rendering it raw would be stored XSS.
    _save(email='<script>alert(1)</script>@x.test')
    body = _get().text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# ---- unknown / missing -----------------------------------------------------

def test_unknown_client_id_404s_cleanly():
    r = _get("does-not-exist")
    assert r.status_code == 404
    assert "Unknown confirmation link." in r.json()["detail"]


def test_unknown_client_id_leaks_no_page_content():
    body = _get("does-not-exist").text
    assert "getcredithelpnow" not in body
    assert "Confirmation #" not in body


def test_storage_failure_is_a_clean_503_not_a_500(monkeypatch):
    class Boom:
        def get_client(self, cid): raise RuntimeError("db down")
    monkeypatch.setattr(appmod, "STORAGE", Boom())
    r = _get()
    assert r.status_code == 503
    assert "db down" not in r.text           # no internals leaked to the customer


# ---- compliance ------------------------------------------------------------

@pytest.mark.parametrize("word", BANNED_WORDS)
def test_rendered_page_contains_no_banned_words(word):
    _save()
    assert word not in _get().text.lower()


def test_banned_words_absent_from_the_template_source():
    # Catches a banned word added to a branch of the template no test renders.
    assert not any(w in appmod._CONFIRMATION_HTML.lower() for w in BANNED_WORDS)
