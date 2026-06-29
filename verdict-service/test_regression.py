"""Confirms the recreated core still honors the original invariants."""
from verdict_core import Item, Snapshot, LetterOutcome, compute_verdict


def snap(cycle, *items): return Snapshot("c1", cycle, list(items))
def coll(b="EQ"): return Item(b, "collection", "ABC Collections", "1234", "collection")
def tl(status): return Item("EQ", "tradeline", "Capital One", "9999", status)


def test_deletion_of_negative_moves():
    v = compute_verdict("c1","2026-06", snap("2026-06"), snap("2026-05", coll()), [], set())
    assert v.moved and v.changes[0]["kind"] == "deletion"

def test_deletion_of_good_account_does_not_move():
    v = compute_verdict("c1","2026-06", snap("2026-06"), snap("2026-05", tl("current")), [], set())
    assert v.moved is False

def test_status_improvement_moves():
    v = compute_verdict("c1","2026-06", snap("2026-06", tl("current")), snap("2026-05", tl("90 days late")), [], set())
    assert v.moved and v.changes[0]["kind"] == "status_improvement"

def test_status_worsening_does_not_move():
    v = compute_verdict("c1","2026-06", snap("2026-06", tl("60 days late")), snap("2026-05", tl("30 days late")), [], set())
    assert v.moved is False

def test_no_change_does_not_move():
    v = compute_verdict("c1","2026-06", snap("2026-06", coll()), snap("2026-05", coll()), [], set())
    assert v.moved is False

def test_letter_deletion_moves():
    letters = [LetterOutcome("EQ","collection","ABC Collections","1234","deleted")]
    v = compute_verdict("c1","2026-06", snap("2026-06", coll()), snap("2026-05", coll()), letters, set())
    assert v.moved and v.changes[0]["source"] == "letter"

def test_snapshot_and_letter_merge_source_order():
    letters = [LetterOutcome("EQ","collection","ABC Collections","1234","deleted")]
    v = compute_verdict("c1","2026-06", snap("2026-06"), snap("2026-05", coll()), letters, set())
    assert len(v.changes) == 1
    assert v.changes[0]["source"] == "snapshot+letter"

def test_dedup_credited_blocks_rebill():
    v1 = compute_verdict("c1","2026-06", snap("2026-06"), snap("2026-05", coll()), [], set())
    credited = set(v1.credit_token)
    v2 = compute_verdict("c1","2026-07", snap("2026-07"), snap("2026-06", coll()), [], credited)
    assert v2.moved is False
