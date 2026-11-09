"""Batch dictation: one transcript -> several ledger entries ("Sharma ko sau udhaar, Verma ne paanch sau diye").

Safety model: a batch is NEVER written silently. Each line is audited on its own; clean lines go to ONE
review list the shopkeeper confirms with a single tap; flagged lines are held back with the reason.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .extract import _MONEY_INTENTS, Extraction, extract
from .names import Customer

# Split on clear entry boundaries only. A bare "aur" also splits ("Sharma ko 100 aur Verma ko 200"); orphan
# fragments with no customer and no amount are merged back into the previous segment.
_SPLIT = re.compile(
    r"\s*(?:[,;।|\n]+|\.(?=\s+[A-Za-zऀ-ॿ])|\b(?:aur\s+phir|uske\s+baad|phir|fir|then|aur|and)\b)\s*",
    re.I)


@dataclass
class Segment:
    text: str
    ex: Extraction
    inherited: bool = False       # intent borrowed from the other lines (all explicit lines agreed)

    @property
    def usable(self) -> bool:
        return self.ex.customer is not None and self.ex.intent in _MONEY_INTENTS and bool(self.ex.amount_paise)


def split_text(text: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT.split(text) if p and p.strip()]
    return parts


def segments(text: str, customers: list[Customer], today: date | None = None) -> list[Segment]:
    raw = split_text(text)
    segs = [Segment(t, extract(t, customers, today)) for t in raw]
    # merge orphans (no customer, no amount) into the previous segment so verbs like "likh do" don't get stranded
    merged: list[Segment] = []
    for s in segs:
        if merged and s.ex.customer is None and not s.ex.spans:
            joined = merged[-1].text + " " + s.text
            merged[-1] = Segment(joined, extract(joined, customers, today))
        else:
            merged.append(s)
    # intent inheritance: only when every explicitly-classified money line agrees (udhaar vs payment is never guessed)
    explicit = {s.ex.intent for s in merged if s.ex.intent in _MONEY_INTENTS}
    if len(explicit) == 1:
        (only,) = explicit
        for s in merged:
            if s.ex.intent not in _MONEY_INTENTS and s.ex.customer is not None and s.ex.spans \
                    and s.ex.intent in ("unknown", "balance_query"):
                s.ex.intent, s.inherited = only, True
    return merged


def is_batch(segs: list[Segment]) -> bool:
    """>=2 lines that each carry a customer AND an amount AND a money intent."""
    return sum(1 for s in segs if s.usable) >= 2
