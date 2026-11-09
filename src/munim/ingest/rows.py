"""Shared plumbing for bringing existing records into the khata (CSV, or a scanned paper page).

Nothing is written until the shopkeeper confirms ONE review list. Opening balances are ordinary chained entries
(meta.src = import|scan) — never a silent bulk write. Names are matched to existing customers only on an exact
full-name match; anything fuzzier becomes a NEW customer flagged for the owner (merging two real people is worse than
duplicating one).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from munim.scribe.names import resolve

MAX_ROWS = 500
MAX_RUPEES = Decimal("10000000")          # ₹1 crore: anything above is almost certainly a mistake
_NAME_OK = re.compile(r"^[A-Za-zऀ-ॿ][A-Za-zऀ-ॿ .'\-]{1,59}$")


@dataclass
class Row:
    name: str
    amount_paise: int
    kind: str = "add_udhaar"            # add_udhaar | record_payment
    as_of: str | None = None
    flags: list[str] = field(default_factory=list)
    customer_id: int | None = None


@dataclass
class Plan:
    rows: list[Row] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)       # (what we saw, why we refused)


def to_paise(value) -> int | None:
    """Exact decimal parse. Rejects negative/zero/NaN/inf/absurd values and anything with garbage around it."""
    s = str(value).strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", s):
        return None
    d = Decimal(s)
    if d <= 0 or d > MAX_RUPEES:
        return None
    try:
        return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None


def clean_name(raw) -> str | None:
    s = " ".join(str(raw).split())
    return s if _NAME_OK.fullmatch(s) else None


def attach_customers(ledger, plan: Plan) -> Plan:
    customers = ledger.customers()
    seen_new: set[str] = set()
    for r in plan.rows:
        res = resolve(r.name, customers)
        if res.status == "match" and res.score >= 100:
            r.customer_id = res.customer.id
        else:
            key = r.name.lower()
            r.flags.append("naya customer")
            if res.status in ("match", "ask"):
                r.flags.append("milta-julta: " + ", ".join(c.name for c, _ in (res.candidates or [(res.customer, 0)])[:2] if c))
            if key in seen_new:
                r.flags.append("same naam dobara")
            seen_new.add(key)
    return plan
