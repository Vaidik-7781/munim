"""Staff audit: spots patterns in what each helper (munshi) records. Deterministic; flags are questions for the owner,
never accusations. Entries carry a pseudonymous label in meta['by'] (owner entries carry none)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from munim.ledger.chain import MONEY_KINDS, Ledger
from munim.timeutil import IST

MIN_ENTRIES = 8


@dataclass
class StaffFlag:
    label: str
    code: str
    detail: str


def audit_staff(ledger: Ledger) -> tuple[dict[str, dict], list[StaffFlag]]:
    ents = ledger.entries()
    by_seq = {e.seq: e for e in ents}
    stats: dict[str, dict] = defaultdict(lambda: {"entries": 0, "payments": 0, "reversed": 0, "after_hours": 0, "round_pay": 0})
    for e in ents:
        if e.kind not in MONEY_KINDS:
            continue
        who = e.meta.get("by", "owner")
        s = stats[who]
        s["entries"] += 1
        h = datetime.fromtimestamp(e.ts, IST).hour
        if h < 7 or h >= 22:
            s["after_hours"] += 1
        if e.kind == "record_payment":
            s["payments"] += 1
            if e.amount_paise % 50000 == 0:
                s["round_pay"] += 1
    for e in ents:
        if e.kind == "reversal" and e.reverses_seq in by_seq:
            stats[by_seq[e.reverses_seq].meta.get("by", "owner")]["reversed"] += 1
    flags: list[StaffFlag] = []
    for who, s in stats.items():
        if who == "owner" or s["entries"] < MIN_ENTRIES:
            continue
        if s["reversed"] / s["entries"] > 0.25:
            flags.append(StaffFlag(who, "high_reversal_rate", f"{s['reversed']} of {s['entries']} entries were later reversed"))
        if s["after_hours"] / s["entries"] > 0.3:
            flags.append(StaffFlag(who, "after_hours_entries", f"{s['after_hours']} of {s['entries']} entries between 22:00 and 07:00"))
        if s["payments"] >= 5 and s["round_pay"] / s["payments"] > 0.8:
            flags.append(StaffFlag(who, "round_payments", f"{s['round_pay']} of {s['payments']} payments are multiples of ₹500"))
    return dict(stats), flags
