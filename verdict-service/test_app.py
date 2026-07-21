"""End-to-end test of the verdict API, including the commit/dedup loop."""
import pytest
from fastapi.testclient import TestClient
import app as appmod

client = TestClient(appmod.app)


@pytest.fixture(autouse=True)
def fresh_storage():
    # Force in-memory storage for tests (module-level STORAGE is PostgresStorage
    # under the dummy test DSN); a fresh instance also isolates each test.
    appmod.STORAGE = appmod.InMemoryStorage()
    yield


def _snap(cycle, items):
    return {"client_id": "c1", "cycle": cycle, "items": items}


COLL = {"bureau": "EQ", "item_type": "collection",
        "creditor": "ABC Collections", "account_mask": "1234", "status": "collection"}


def test_full_flow_deletion_then_commit_blocks_rebill():
    # Cycle 05: baseline with a collection.
    client.post("/parser/snapshot", json=_snap("2026-05", [COLL]))
    # Cycle 06: collection gone -> movement.
    client.post("/parser/snapshot", json=_snap("2026-06", []))

    r = client.get("/parser/verdict", params={"client_id": "c1", "cycle": "2026-06"})
    body = r.json()
    assert body["moved"] is True
    assert len(body["changes"]) == 1
    token = body["credit_token"]

    # Charge succeeded -> commit.
    client.post("/parser/verdict/commit", json={"client_id": "c1", "change_ids": token})

    # Re-running the same cycle's verdict must now be False (already billed).
    r2 = client.get("/parser/verdict", params={"client_id": "c1", "cycle": "2026-06"})
    assert r2.json()["moved"] is False


def test_verdict_false_when_no_snapshot():
    r = client.get("/parser/verdict", params={"client_id": "ghost", "cycle": "2026-06"})
    body = r.json()
    assert body["moved"] is False
    assert "no snapshot" in body["reason"]


def test_health_is_200_and_needs_no_api_key(monkeypatch):
    # Set the key so require_api_key actually enforces (it fails open when unset) --
    # otherwise this test would pass even if /parser/health were behind auth.
    monkeypatch.setenv("INTERNAL_API_KEY", "sekret")
    assert client.get("/parser/verdict",
                      params={"client_id": "c1", "cycle": "2026-06"}).status_code == 401

    r = client.get("/parser/health")            # no X-API-Key header
    assert r.status_code == 200
    assert r.json() == {"ok": True, "service": "verdict"}


def test_letters_ingest_then_movement():
    client.post("/parser/snapshot", json=_snap("2026-05", [COLL]))
    client.post("/parser/snapshot", json=_snap("2026-06", [COLL]))  # snapshot lags
    client.post("/parser/letters", json={
        "client_id": "c1", "cycle": "2026-06",
        "outcomes": [{"bureau": "EQ", "item_type": "collection",
                      "creditor": "ABC Collections", "account_mask": "1234",
                      "outcome": "deleted"}]})
    r = client.get("/parser/verdict", params={"client_id": "c1", "cycle": "2026-06"})
    assert r.json()["moved"] is True
