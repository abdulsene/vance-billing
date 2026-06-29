"""
Snapshot adapter — map a credit-monitoring feed's report JSON into the
`POST /parser/snapshot` payload the verdict service expects.

Monitoring feeds differ, so this maps the COMMON normalized shape and exposes a
`FieldMap` you edit to match your feed's field names. Send me one real sample
payload and I'll tailor this exactly (and add a test for your shape).

Target payload:
    { "client_id": ..., "cycle": "2026-06",
      "items": [ {bureau,item_type,creditor,account_mask,status,balance,past_due}, ... ] }
"""
from __future__ import annotations
from dataclasses import dataclass, field

# Map whatever your feed calls a bureau to EQ / TU / EX.
BUREAU_ALIASES = {
    "transunion": "TU", "trans union": "TU", "tuc": "TU", "tu": "TU",
    "equifax": "EQ", "eqf": "EQ", "eq": "EQ",
    "experian": "EX", "exp": "EX", "ex": "EX",
}


def normalize_bureau(raw: str) -> str:
    return BUREAU_ALIASES.get((raw or "").strip().lower(), (raw or "").upper()[:2])


def mask_account(num) -> str:
    s = "".join(ch for ch in str(num or "") if ch.isalnum())
    return s[-4:] if len(s) >= 4 else s


@dataclass
class FieldMap:
    """Where to find things in your feed. Edit the right-hand strings to match."""
    # Section name -> item_type the verdict service expects.
    sections: dict = field(default_factory=lambda: {
        "tradelines": "tradeline",
        "collections": "collection",
        "inquiries": "inquiry",
        "public_records": "public_record",
    })
    bureau: str = "bureau"
    creditor: str = "creditor"
    account_number: str = "account_number"
    status: str = "status"
    balance: str = "balance"
    past_due: str = "past_due"
    inquiry_date: str = "date"     # inquiries have no account #; date makes them unique


DEFAULT = FieldMap()


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def to_snapshot(client_id: str, cycle: str, report: dict, fields: FieldMap = DEFAULT) -> dict:
    items = []
    for section, item_type in fields.sections.items():
        for row in report.get(section, []) or []:
            if item_type == "inquiry":
                mask = mask_account(row.get(fields.inquiry_date)) or str(row.get(fields.inquiry_date, ""))
                status = "inquiry"
            else:
                mask = mask_account(row.get(fields.account_number))
                status = row.get(fields.status, "")
            items.append({
                "bureau": normalize_bureau(row.get(fields.bureau, "")),
                "item_type": item_type,
                "creditor": row.get(fields.creditor, ""),
                "account_mask": mask,
                "status": status,
                "balance": _num(row.get(fields.balance)),
                "past_due": _num(row.get(fields.past_due)),
            })
    return {"client_id": client_id, "cycle": cycle, "items": items}


# Example of the common normalized shape this maps out of the box.
SAMPLE_REPORT = {
    "tradelines": [
        {"bureau": "TransUnion", "creditor": "Capital One", "account_number": "...9999",
         "status": "Current", "balance": 1200, "past_due": 0},
    ],
    "collections": [
        {"bureau": "Equifax", "creditor": "ABC Collections", "account_number": "XX1234",
         "status": "Collection", "balance": 540},
    ],
    "inquiries": [
        {"bureau": "Experian", "creditor": "XYZ Auto Finance", "date": "2026-01-15"},
    ],
}

if __name__ == "__main__":
    import json
    print(json.dumps(to_snapshot("c1", "2026-06", SAMPLE_REPORT), indent=2))
