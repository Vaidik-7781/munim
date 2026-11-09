"""Tone linter: blocks collection-style pressure before a message can ever be shown for approval.

Aligned with RBI's fair-practices direction on recovery agents: no threats, no shaming, no
third-party disclosure, no false urgency. Deterministic rules so the result is explainable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_BANNED = [
    (r"\b(police|thana|court|kachehri|fir|legal\s*action|notice|vakil|lawyer|jail|case\s*kar)\b", "legal_threat"),
    (r"\b(badnaam|sharam|beizzat|izzat|defaulter|chor|dhokha|fraud|cheat)\b", "shaming"),
    (r"\b(dhamki|dekh\s*lena|anjaam|barbaad|bura\s*hoga|warna|nahi\s*to)\b", "threat"),
    (r"\b(last\s*warning|aakhri\s*mauka|final\s*notice|abhi\s*ke\s*abhi|turant|immediately|urgent)\b", "false_urgency"),
    (r"\b(padosi|neighbou?r|parivar|ghar\s*wale|family|rishtedar|boss|office\s*mein)\b", "third_party"),
    (r"\b(byaaj|interest|penalty|jurmana|late\s*fee)\b", "unagreed_charges"),
]
_DISCLOSURE = re.compile(r"\bAI\b|\bMunim\b", re.I)


@dataclass
class LintResult:
    ok: bool
    violations: list[str]


def lint(text: str, *, require_disclosure: bool = True) -> LintResult:
    v: list[str] = []
    low = text.lower()
    for pat, code in _BANNED:
        if re.search(pat, low):
            v.append(code)
    letters = [c for c in text if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.4 and len(letters) > 12:
        v.append("shouting")
    if text.count("!") > 1:
        v.append("excess_exclamation")
    if require_disclosure and not _DISCLOSURE.search(text):
        v.append("missing_ai_disclosure")
    return LintResult(not v, sorted(set(v)))
