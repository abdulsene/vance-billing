"""Path B tests: manual (reviewer-confirmed) movement + cross-source dedup."""
import pytest
from fastapi.testclient import TestClient
import app as appmod
from verdict_core import (Item, Snapshot, compute_verdict, changes_from_manual)

client = TestClient(appmod.app)


@pytest.fixture(autouse=True)
def fresh():
    appmod.STORAGE.clear()
    yield


DEL = {"bureau": "EQ", "item_type": "collection", "creditor": "ABC Collections",
       "account_mask": "1234", "kind": "deletion"}


# --- core: manual produces movement with no snapshot at all ----------------- #

def test_manual_deletion_moves_without_snapshot():
    v = compute_verdict("c1", "2026-06", None, None, [],
                        credited=set(), manual=[DEL])
    assert v.moved is True
    assert v.changes[0]["kind"] == "deletion"
    assert v.changes[0]["source"] == "manual"


def test_manual_status_improvement():
    e = {"bureau": "EQ", "item_type": "tradeline", "creditor": "Capital One",
         "account_mask": "9999", "kind": "status_improvement", "target": "current"}
    v = compute_verdict("c1", "2026-06", None, None, [], set(), manual=[e])
    assert v.moved is True
    assert v.changes[0]["kind"] == "status_improvement"
    assert v.changes[0]["target"] == "current"


# --- the cross-source dedup that protects Path B -> Path A ------------------- #

def test_manual_deletion_then_snapshot_same_item_bills_once():
    # Reviewer logs the deletion in June (manual), it's credited after the charge.
    v_june = compute_verdict("c1", "2026-06", None, None, [], set(), manual=[DEL])
    credited = set(v_june.credit_token)

    # Later (Path A), a snapshot finally shows the item gone. Same change_id ->
    # already credited -> NOT billed again.
    prev = Snapshot("c1", "2026-06", [Item("EQ", "collection", "ABC Collections", "1234", "collection")])
    curr = Snapshot("c1", "2026-07", [])
    v_july = compute_verdict("c1", "2026-07", curr, prev, [], credited, manual=[])
    assert v_july.moved is False


def test_manual_and_snapshot_same_cycle_merge_to_one():
    prev = Snapshot("c1", "2026-05", [Item("EQ", "collection", "ABC Collections", "1234", "collection")])
    curr = Snapshot("c1", "2026-06", [])  # snapshot also shows it gone
    v = compute_verdict("c1", "2026-06", curr, prev, [], set(), manual=[DEL])
    assert len(v.changes) == 1                     # not double-counted
    assert "manual" in v.changes[0]["source"]
    assert "snapshot" in v.changes[0]["source"]


# --- API end-to-end --------------------------------------------------------- #

def test_api_manual_flow_then_commit_blocks_rebill():
    r = client.post("/parser/manual-movement",
                    json={"client_id": "c1", "cycle": "2026-06", "entries": [DEL]})
    assert r.json()["ok"] is True

    v = client.get("/parser/verdict", params={"client_id": "c1", "cycle": "2026-06"}).json()
    assert v["moved"] is True
    token = v["credit_token"]

    client.post("/parser/verdict/commit", json={"client_id": "c1", "change_ids": token})

    v2 = client.get("/parser/verdict", params={"client_id": "c1", "cycle": "2026-06"}).json()
    assert v2["moved"] is False


def test_api_no_data_is_safe_skip():
    v = client.get("/parser/verdict", params={"client_id": "ghost", "cycle": "2026-06"}).json()
    assert v["moved"] is False
    assert "no snapshot" in v["reason"]


def test_api_reviewer_adds_entries_incrementally():
    # Two separate manual posts in the same cycle accumulate.
    client.post("/parser/manual-movement",
                json={"client_id": "c1", "cycle": "2026-06", "entries": [DEL]})
    e2 = {"bureau": "TU", "item_type": "collection", "creditor": "XYZ Recovery",
          "account_mask": "5678", "kind": "deletion"}
    client.post("/parser/manual-movement",
                json={"client_id": "c1", "cycle": "2026-06", "entries": [e2]})
    v = client.get("/parser/verdict", params={"client_id": "c1", "cycle": "2026-06"}).json()
    assert v["moved"] is True
    assert len(v["changes"]) == 2
