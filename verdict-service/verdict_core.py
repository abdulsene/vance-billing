"""
Vance Credit — Movement Verdict (core logic)

Pure-stdlib, no I/O. Decides whether a client's tri-bureau report MOVED in a
billing cycle, by diffing this cycle's snapshot against the previous one,
classifying only *favorable* item-level changes, corroborating with bureau
response letters, and de-duplicating against changes already credited (so the
same deletion is never billed twice — once via letter, again when the snapshot
catches up).

Returns moved=True only when there is >=1 favorable change NOT already credited.
This module never writes the ledger; commit happens after a charge succeeds.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import re

# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #

_NEGATIVE_TYPES = {"collection", "public_record", "inquiry"}

# Lower number = healthier. Used to decide if a status change is an improvement.
SEVERITY = {
    "current": 0, "ok": 0, "paid": 0, "closed": 0, "paid_closed": 0,
    "late_30": 30, "late_60": 60, "late_90": 90, "late_120": 120,
    "settled": 140, "paid_collection": 150,
    "collection": 200, "chargeoff": 200, "repossession": 220,
    "derogatory": 180, "foreclosure": 240, "public_record": 250,
    "bankruptcy": 300,
}

_STATUS_ALIASES = {
    "ok": "current", "pays as agreed": "current", "current": "current",
    "paid as agreed": "current", "never late": "current",
    "30 days late": "late_30", "30": "late_30", "30 day": "late_30",
    "60 days late": "late_60", "60": "late_60",
    "90 days late": "late_90", "90": "late_90",
    "120 days late": "late_120", "120": "late_120", "120+": "late_120",
    "charge off": "chargeoff", "charged off": "chargeoff", "charge-off": "chargeoff",
    "collection": "collection", "in collection": "collection", "collections": "collection",
    "paid collection": "paid_collection", "paid/closed": "paid_closed",
    "repo": "repossession", "repossession": "repossession",
    "settled": "settled", "paid": "paid", "closed": "closed",
    "derogatory": "derogatory", "derog": "derogatory",
    "bankruptcy": "bankruptcy", "foreclosure": "foreclosure",
}


def normalize_creditor(name: str) -> str:
    s = (name or "").lower().strip()
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    s = re.sub(r"\b(llc|inc|na|corp|company|co|services|svcs|bank)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_status(status: str) -> Optional[str]:
    s = (status or "").lower().strip()
    if s in _STATUS_ALIASES:
        return _STATUS_ALIASES[s]
    if s in SEVERITY:
        return s
    return None  # unknown -> not comparable, never credited


def severity(status: Optional[str]) -> Optional[int]:
    return SEVERITY.get(status) if status else None


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

@dataclass
class Item:
    bureau: str                 # "EQ" | "TU" | "EX"
    item_type: str              # "tradeline" | "collection" | "public_record" | "inquiry"
    creditor: str
    account_mask: str = ""
    status: str = ""
    balance: Optional[float] = None
    past_due: Optional[float] = None

    @property
    def fingerprint(self) -> str:
        return f"{self.bureau}|{self.item_type}|{normalize_creditor(self.creditor)}|{self.account_mask}"

    @property
    def is_negative(self) -> bool:
        if self.item_type in _NEGATIVE_TYPES:
            return True
        sev = severity(normalize_status(self.status))
        return sev is not None and sev >= 30


@dataclass
class Snapshot:
    client_id: str
    cycle: str                  # e.g. "2026-06"
    items: list = field(default_factory=list)  # list[Item]

    def by_fp(self) -> dict:
        return {i.fingerprint: i for i in self.items}


@dataclass
class LetterOutcome:
    bureau: str
    item_type: str
    creditor: str
    account_mask: str = ""
    outcome: str = ""           # "deleted" | "updated" | "verified" | "remains"

    @property
    def fingerprint(self) -> str:
        return f"{self.bureau}|{self.item_type}|{normalize_creditor(self.creditor)}|{self.account_mask}"


@dataclass
class Change:
    change_id: str
    fingerprint: str
    bureau: str
    creditor: str
    item_type: str
    kind: str                   # "deletion" | "status_improvement"
    detail: str
    source: str                 # "snapshot" | "letter" | "snapshot+letter"
    target: str                 # "deleted" | new status


@dataclass
class Verdict:
    client_id: str
    cycle: str
    moved: bool
    changes: list                       # list[dict]
    credit_token: list                  # list[change_id] to commit after charge


# --------------------------------------------------------------------------- #
# Change detection
# --------------------------------------------------------------------------- #

def _deletion_id(fp: str) -> str:
    # Same id whether discovered by letter or snapshot, so they de-dup against each other.
    return f"{fp}|deletion|deleted"


def _improvement_id(fp: str, new_status: str) -> str:
    return f"{fp}|status_improvement|{new_status}"


def diff_snapshots(previous: Optional[Snapshot], current: Snapshot) -> list:
    """Favorable changes visible on the report itself."""
    out: list = []
    if previous is None:
        return out
    prev = previous.by_fp()
    curr = current.by_fp()

    # Deletions: present before, gone now — only credit if the deleted item was negative.
    for fp, item in prev.items():
        if fp not in curr and item.is_negative:
            out.append(Change(
                change_id=_deletion_id(fp), fingerprint=fp, bureau=item.bureau,
                creditor=item.creditor, item_type=item.item_type, kind="deletion",
                detail=f"{item.item_type.replace('_', ' ')} '{item.creditor}' deleted",
                source="snapshot", target="deleted"))

    # Status improvements: same item, strictly healthier status.
    for fp, citem in curr.items():
        pitem = prev.get(fp)
        if not pitem:
            continue
        ps, cs = normalize_status(pitem.status), normalize_status(citem.status)
        psev, csev = severity(ps), severity(cs)
        if psev is not None and csev is not None and csev < psev:
            out.append(Change(
                change_id=_improvement_id(fp, cs), fingerprint=fp, bureau=citem.bureau,
                creditor=citem.creditor, item_type=citem.item_type, kind="status_improvement",
                detail=f"'{citem.creditor}': {ps} \u2192 {cs}",
                source="snapshot", target=cs))
    return out


def changes_from_letters(letters: list) -> list:
    """Bureau response letters confirming deletions (early signal + attribution)."""
    out: list = []
    for lo in letters or []:
        if (lo.outcome or "").lower() == "deleted":
            fp = lo.fingerprint
            out.append(Change(
                change_id=_deletion_id(fp), fingerprint=fp, bureau=lo.bureau,
                creditor=lo.creditor, item_type=lo.item_type, kind="deletion",
                detail=f"{lo.item_type.replace('_', ' ')} '{lo.creditor}' deleted (bureau response)",
                source="letter", target="deleted"))
    return out


def _merge(snapshot_changes: list, letter_changes: list) -> list:
    """One entry per change_id; if both snapshot and letter saw it, mark source as both."""
    merged: dict = {}
    for c in snapshot_changes + letter_changes:
        if c.change_id in merged:
            existing = merged[c.change_id]
            if existing.source != c.source:
                existing.source = "snapshot+letter"
        else:
            merged[c.change_id] = c
    return list(merged.values())


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #

def compute_verdict(client_id: str, cycle: str, current: Snapshot,
                    previous: Optional[Snapshot], letters: list,
                    credited: set) -> Verdict:
    """
    moved = there is >=1 favorable change this cycle that has NOT already been
    credited (billed) for this client. credited is a set of change_ids.
    """
    candidates = _merge(diff_snapshots(previous, current), changes_from_letters(letters))
    fresh = [c for c in candidates if c.change_id not in credited]
    return Verdict(
        client_id=client_id, cycle=cycle, moved=len(fresh) > 0,
        changes=[asdict(c) for c in fresh],
        credit_token=[c.change_id for c in fresh],
    )
