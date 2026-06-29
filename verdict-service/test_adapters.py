"""Adapter tests + adapter→verdict integration."""
from adapters import to_snapshot, SAMPLE_REPORT, normalize_bureau, mask_account
from verdict_core import Item, Snapshot, compute_verdict


def test_bureau_and_mask_normalization():
    assert normalize_bureau("TransUnion") == "TU"
    assert normalize_bureau("Equifax") == "EQ"
    assert normalize_bureau("Experian") == "EX"
    assert mask_account("XX1234") == "1234"
    assert mask_account("...9999") == "9999"


def test_sample_maps_to_three_items():
    snap = to_snapshot("c1", "2026-06", SAMPLE_REPORT)
    assert snap["client_id"] == "c1"
    assert len(snap["items"]) == 3
    types = sorted(i["item_type"] for i in snap["items"])
    assert types == ["collection", "inquiry", "tradeline"]


def test_inquiry_gets_unique_mask_from_date():
    snap = to_snapshot("c1", "2026-06", SAMPLE_REPORT)
    inq = next(i for i in snap["items"] if i["item_type"] == "inquiry")
    assert inq["account_mask"]  # not empty -> fingerprintable
    assert inq["status"] == "inquiry"


def test_adapter_output_flows_through_verdict():
    # Build two snapshots via the adapter and confirm a deleted collection reads as movement.
    prev_payload = to_snapshot("c1", "2026-05", SAMPLE_REPORT)
    report_after = dict(SAMPLE_REPORT)
    report_after["collections"] = []  # collection deleted
    curr_payload = to_snapshot("c1", "2026-06", report_after)

    prev = Snapshot("c1", "2026-05", [Item(**i) for i in prev_payload["items"]])
    curr = Snapshot("c1", "2026-06", [Item(**i) for i in curr_payload["items"]])
    v = compute_verdict("c1", "2026-06", curr, prev, [], set())
    assert v.moved is True
    assert any(c["item_type"] == "collection" for c in v.changes)
