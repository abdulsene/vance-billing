"""
Vance Credit — Movement Verdict (core logic)  [Path B update]

Decides whether a client's tri-bureau report MOVED in a cycle. Three input
sources, all deduped by a shared change_id so the same favorable change bills
ONCE no matter how it was detected:

  1. snapshot diff   — automated, when a report snapshot is available
  2. response letters — bureau "deleted" confirmations
  3. manual movement  — reviewer-confirmed changes captured at CRC re-import (Path B)

A deletion logged by a reviewer this month shares a change_id with the same
deletion if a snapshot later sees it — so going from Path B to Path A never
double-bills. Read-only: the ledger is committed only after a charge clears.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import re

_NEGATIVE_TYPES = {"collection", "public_record", "inquiry"}

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
    return None


def severity(status: Optional[str]) -> Optional[int]:
    return SEVERITY.get(status) if status else None


@dataclass
class Item:
    bureau: str
    item_type: str
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
    cycle: str
    items: list = field(default_factory=list)

    def by_fp(self) -> dict:
        return {i.fingerprint: i for i in self.items}


@dataclass
class LetterOutcome:
    bureau: str
    item_type: str
    creditor: str
    account_mask: str = ""
    outcome: str = ""

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
    kind: str
    detail: str
    source: str
    target: str


@dataclass
class Verdict:
    client_id: str
    cycle: str
    moved: bool
    changes: list
    credit_token: list


def _fp(bureau, item_type, creditor, account_mask) -> str:
    return f"{bureau}|{item_type}|{normalize_creditor(creditor)}|{account_mask}"


def _deletion_id(fp: str) -> str:
    return f"{fp}|deletion|deleted"


def _improvement_id(fp: str, new_status: str) -> str:
    return f"{fp}|status_improvement|{new_status}"


def diff_snapshots(previous: Optional[Snapshot], current: Optional[Snapshot]) -> list:
    out: list = []
    if previous is None or current is None:
        return out
    prev, curr = previous.by_fp(), current.by_fp()
    for fp, item in prev.items():
        if fp not in curr and item.is_negative:
            out.append(Change(_deletion_id(fp), fp, item.bureau, item.creditor,
                              item.item_type, "deletion",
                              f"{item.item_type.replace('_', ' ')} '{item.creditor}' deleted",
                              "snapshot", "deleted"))
    for fp, citem in curr.items():
        pitem = prev.get(fp)
        if not pitem:
            continue
        ps, cs = normalize_status(pitem.status), normalize_status(citem.status)
        psev, csev = severity(ps), severity(cs)
        if psev is not None and csev is not None and csev < psev:
            out.append(Change(_improvement_id(fp, cs), fp, citem.bureau, citem.creditor,
                              citem.item_type, "status_improvement",
                              f"'{citem.creditor}': {ps} \u2192 {cs}", "snapshot", cs))
    return out


def changes_from_letters(letters: list) -> list:
    out: list = []
    for lo in letters or []:
        if (lo.outcome or "").lower() == "deleted":
            fp = lo.fingerprint
            out.append(Change(_deletion_id(fp), fp, lo.bureau, lo.creditor, lo.item_type,
                              "deletion",
                              f"{lo.item_type.replace('_', ' ')} '{lo.creditor}' deleted (bureau response)",
                              "letter", "deleted"))
    return out


def changes_from_manual(entries: list) -> list:
    """Reviewer-confirmed favorable changes captured at CRC re-import (Path B)."""
    out: list = []
    for e in entries or []:
        bureau = e.get("bureau", ""); item_type = e.get("item_type", "")
        creditor = e.get("creditor", ""); mask = e.get("account_mask", "")
        fp = _fp(bureau, item_type, creditor, mask)
        kind = (e.get("kind") or "").lower()
        if kind == "deletion":
            out.append(Change(_deletion_id(fp), fp, bureau, creditor, item_type, "deletion",
                              f"{item_type.replace('_', ' ')} '{creditor}' deleted (reviewer-confirmed)",
                              "manual", "deleted"))
        elif kind in ("status_improvement", "improvement"):
            target = normalize_status(e.get("target", "")) or (e.get("target") or "improved")
            out.append(Change(_improvement_id(fp, target), fp, bureau, creditor, item_type,
                              "status_improvement",
                              f"'{creditor}': improved to {target} (reviewer-confirmed)",
                              "manual", target))
    return out


def _merge(*change_lists) -> list:
    """One entry per change_id; record every source that saw it, in encounter order."""
    merged: dict = {}
    for lst in change_lists:
        for c in lst:
            if c.change_id in merged:
                ex = merged[c.change_id]
                if c.source not in ex.source.split("+"):
                    ex.source = ex.source + "+" + c.source
            else:
                merged[c.change_id] = c
    return list(merged.values())


def compute_verdict(client_id: str, cycle: str, current: Optional[Snapshot],
                    previous: Optional[Snapshot], letters: list, credited: set,
                    manual: Optional[list] = None) -> Verdict:
    candidates = _merge(diff_snapshots(previous, current),
                        changes_from_letters(letters),
                        changes_from_manual(manual or []))
    fresh = [c for c in candidates if c.change_id not in credited]
    return Verdict(client_id=client_id, cycle=cycle, moved=len(fresh) > 0,
                   changes=[asdict(c) for c in fresh],
                   credit_token=[c.change_id for c in fresh])
