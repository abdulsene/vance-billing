"""Tests for the movement verdict core logic."""
from verdict_core import (
    Item, Snapshot, LetterOutcome, compute_verdict, diff_snapshots,
)


def snap(cycle, *items):
    return Snapshot(client_id="c1", cycle=cycle, items=list(items))


def coll(creditor="ABC Collections", mask="1234", bureau="EQ"):
    return Item(bureau=bureau, item_type="collection", creditor=creditor,
                account_mask=mask, status="collection")


def tradeline(status, creditor="Capital One", mask="9999", bureau="EQ"):
    return Item(bureau=bureau, item_type="tradeline", creditor=creditor,
                account_mask=mask, status=status)


# --- deletions -------------------------------------------------------------- #

def test_deletion_of_negative_item_is_movement():
    prev = snap("2026-05", coll())
    curr = snap("2026-06")  # collection gone
    v = compute_verdict("c1", "2026-06", curr, prev, [], set())
    assert v.moved is True
    assert len(v.changes) == 1
    assert v.changes[0]["kind"] == "deletion"


def test_deletion_of_good_account_is_not_movement():
    # Losing a healthy, current tradeline is NOT a favorable change.
    prev = snap("2026-05", tradeline("current"))
    curr = snap("2026-06")
    v = compute_verdict("c1", "2026-06", curr, prev, [], set())
    assert v.moved is False
    assert v.changes == []


# --- status changes --------------------------------------------------------- #

def test_status_improvement_is_movement():
    prev = snap("2026-05", tradeline("90 days late"))
    curr = snap("2026-06", tradeline("current"))
    v = compute_verdict("c1", "2026-06", curr, prev, [], set())
    assert v.moved is True
    assert v.changes[0]["kind"] == "status_improvement"


def test_status_worsening_is_not_movement():
    prev = snap("2026-05", tradeline("30 days late"))
    curr = snap("2026-06", tradeline("60 days late"))
    v = compute_verdict("c1", "2026-06", curr, prev, [], set())
    assert v.moved is False


def test_no_change_is_not_movement():
    prev = snap("2026-05", coll(), tradeline("current"))
    curr = snap("2026-06", coll(), tradeline("current"))
    v = compute_verdict("c1", "2026-06", curr, prev, [], set())
    assert v.moved is False


def test_unknown_status_is_not_credited():
    prev = snap("2026-05", tradeline("some weird status"))
    curr = snap("2026-06", tradeline("another weird status"))
    v = compute_verdict("c1", "2026-06", curr, prev, [], set())
    assert v.moved is False


# --- letters ---------------------------------------------------------------- #

def test_letter_deletion_counts_before_snapshot_catches_up():
    # Snapshot hasn't refreshed yet (item still present), but the bureau letter
    # confirms the deletion this cycle.
    prev = snap("2026-05", coll())
    curr = snap("2026-06", coll())  # still showing
    letters = [LetterOutcome(bureau="EQ", item_type="collection",
                             creditor="ABC Collections", account_mask="1234",
                             outcome="deleted")]
    v = compute_verdict("c1", "2026-06", curr, prev, letters, set())
    assert v.moved is True
    assert v.changes[0]["source"] == "letter"


def test_verified_letter_is_not_movement():
    prev = snap("2026-05", coll())
    curr = snap("2026-06", coll())
    letters = [LetterOutcome(bureau="EQ", item_type="collection",
                             creditor="ABC Collections", account_mask="1234",
                             outcome="verified")]
    v = compute_verdict("c1", "2026-06", curr, prev, letters, set())
    assert v.moved is False


# --- the critical dedup case ------------------------------------------------ #

def test_same_deletion_not_billed_twice_letter_then_snapshot():
    # Cycle 2: letter confirms deletion -> moved, credit it.
    prev2 = snap("2026-05", coll())
    curr2 = snap("2026-06", coll())  # snapshot lags, still present
    letters = [LetterOutcome(bureau="EQ", item_type="collection",
                             creditor="ABC Collections", account_mask="1234",
                             outcome="deleted")]
    v2 = compute_verdict("c1", "2026-06", curr2, prev2, letters, set())
    assert v2.moved is True
    credited = set(v2.credit_token)  # committed after the charge succeeds

    # Cycle 3: snapshot finally drops the item -> snapshot diff "rediscovers" it,
    # but it's already credited, so it must NOT bill again.
    prev3 = snap("2026-06", coll())
    curr3 = snap("2026-07")  # now gone from report
    v3 = compute_verdict("c1", "2026-07", curr3, prev3, [], credited)
    assert v3.moved is False
    assert v3.changes == []


def test_letter_and_snapshot_same_cycle_merge_to_one_change():
    prev = snap("2026-05", coll())
    curr = snap("2026-06")  # gone in snapshot
    letters = [LetterOutcome(bureau="EQ", item_type="collection",
                             creditor="ABC Collections", account_mask="1234",
                             outcome="deleted")]
    v = compute_verdict("c1", "2026-06", curr, prev, letters, set())
    assert len(v.changes) == 1          # not double-counted
    assert v.changes[0]["source"] == "snapshot+letter"


# --- multiple + per-bureau -------------------------------------------------- #

def test_multiple_favorable_changes():
    prev = snap("2026-05", coll(), tradeline("90 days late"))
    curr = snap("2026-06", tradeline("current"))  # collection deleted + late cured
    v = compute_verdict("c1", "2026-06", curr, prev, [], set())
    assert v.moved is True
    assert len(v.changes) == 2


def test_deletion_on_one_bureau_counts():
    prev = snap("2026-05", coll(bureau="EQ"), coll(bureau="TU"))
    curr = snap("2026-06", coll(bureau="TU"))  # EQ deleted, TU remains
    v = compute_verdict("c1", "2026-06", curr, prev, [], set())
    assert v.moved is True
    assert len(v.changes) == 1
    assert v.changes[0]["bureau"] == "EQ"


def test_first_cycle_no_previous_snapshot():
    curr = snap("2026-06", coll())
    v = compute_verdict("c1", "2026-06", curr, None, [], set())
    assert v.moved is False  # nothing to diff against, no letters
