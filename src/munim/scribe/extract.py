"""Scribe: transcript -> structured Extraction.

Principle: code computes money. Intent and name come from rules (an optional, disclosed LLM may
only *propose spans*; see llm.py). The amount is ALWAYS computed by indic_numbers from the text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from datetime import date

from munim.insights.reports import REPORT_ANY, report_kind

from .dates_hi import When, parse_when, strip_date_phrases
from .indic_numbers import AmountSpan, parse_amounts
from .names import Customer, Resolution, resolve

INTENTS = (
    "add_udhaar", "record_payment", "balance_query", "send_reminder", "pause_customer",
    "resume_customer", "dispute_entry", "opt_out_customer", "undo_last", "help", "promise_to_pay",
    "report_query", "unknown",
)

# (intent, keyword regex) in PRIORITY order — first hit wins.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("undo_last", re.compile(r"\b(undo|cancel|galti\s*se|pichla\s*(hata|delete)|last\s*(wala)?\s*(hata|delete|cancel)|hata\s*do)\b|गलती से|पिछला", re.I)),
    ("help", re.compile(r"\b(help|madad|kya\s*kar\s*sakte|kaise\s*use)\b|मदद", re.I)),
    ("opt_out_customer", re.compile(r"\b(opt\s*out|band\s*karo\s*(reminder|message)|reminder\s*(mat|band)|mana\s*kiya|nahi\s*chahiye\s*(reminder|message))\b", re.I)),
    ("resume_customer", re.compile(r"\b(resume|dobara\s*shuru|phir\s*se\s*shuru|chalu\s*karo|shuru\s*karo)\b", re.I)),
    ("pause_customer", re.compile(r"\b(pause|ruko|mat\s*bhejo|reminder\s*roko|rok\s*do|thehro)\b|रुको", re.I)),
    ("dispute_entry", re.compile(r"\b(dispute|galat\s*(entry|hisaab|hisab)|maine\s*nahi\s*(liya|diya)|nahi\s*liya)\b", re.I)),
    ("send_reminder", re.compile(r"\b(reminder|yaad\s*dila|yaad\s*dilao|yaad\s*dilana|msg\s*bhejo|message\s*bhejo|bhej\s*do\s*(reminder|message))\b|याद दिला", re.I)),
    ("report_query", REPORT_ANY),
    ("balance_query", re.compile(r"\b(kitna|kitne|baaki\s*(kitna|kitne|hai)|hisaab|hisab|balance|total\s*kitna|bakaya)\b|कितना|हिसाब", re.I)),
    ("promise_to_pay", re.compile(r"\b(denge|dega|degi|dunga|dungi|de\s*denge|chuka\s*denge|chukayenge|karenge|bhejenge|laayenge|layenge|wada|vaada|promise)\b|देंगे|दूंगा|वादा", re.I)),
    ("record_payment", re.compile(r"\b(diye|diya|de\s*gaye|de\s*gaya|de\s*gayi|jama|chuka|chukaye|chuke|payment|paid|wapas\s*(kiye|kiya|diye)|laut(a|aye)|received|mile|mila|aaye)\b|दिए|दिया|जमा|चुका", re.I)),
    ("add_udhaar", re.compile(r"\b(udhaar|udhar|ud(h)?ar|likh|likho|jod|baaki|le\s*gaye|le\s*gaya|le\s*gayi|liya|liye|khata|naam\s*(pe|par|mein|me))\b|उधार|लिख|जोड़", re.I)),
]

PRONOUN = re.compile(r"\b(unhi|usi|inhi|isi|wahi|vahi|unko|usko|inko|isko|unka|uska)\b|उन्हीं|उसी|वही", re.I)

_MONEY_INTENTS = {"add_udhaar", "record_payment"}


@dataclass
class Extraction:
    intent: str
    transcript: str
    resolution: Resolution
    spans: list[AmountSpan] = field(default_factory=list)   # valid, non-quantity money spans
    invalid_spans: list[AmountSpan] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    when: When | None = None
    report: str | None = None
    rescued_customer: bool = False      # customer found only by the transliteration pass (Gnani format=transliterate)
    inferred_customer: bool = False     # customer taken from conversation context ("unhi ko"), never silently trusted

    @property
    def amount_paise(self) -> int | None:
        """Sum of money spans; the Auditor flags multi_amount so a sum is never silently trusted."""
        if not self.spans:
            return None
        return sum(s.paise for s in self.spans if s.paise is not None)

    @property
    def customer(self) -> Customer | None:
        return self.resolution.customer


def classify_intent(text: str) -> tuple[str, list[str]]:
    for intent, pat in _RULES:
        hits = pat.findall(text)
        if pat.search(text):
            m = pat.search(text)
            return intent, [m.group(0)] if m else []
    return "unknown", []


def _mask_name_tokens(text: str, resolution: Resolution) -> str:
    """Blank out the customer-name tokens so 'Das ji' is not read as ₹10."""
    if resolution.span is None:
        return text
    raw = [t for t in text.split()]
    cleaned = [t.strip(".,!?;:\"'()।") for t in raw]
    idx = [i for i, t in enumerate(cleaned) if t]
    s, e = resolution.span
    kill = {idx[k] for k in range(s, min(e, len(idx)))}
    return " ".join("_" if i in kill else t for i, t in enumerate(raw))


def extract(transcript: str, customers: list[Customer], today: date | None = None) -> Extraction:
    text = transcript.strip()
    intent, kws = classify_intent(text)
    res = resolve(text, customers)
    masked = _mask_name_tokens(text, res) if res.status in ("match", "ask") else text
    when = None
    if intent == "promise_to_pay":
        if today is not None:
            when = parse_when(masked, today)
        masked = strip_date_phrases(masked)
    spans = parse_amounts(masked)
    good = [s for s in spans if s.valid and not s.is_quantity]
    if any(s.strong for s in good):
        good = [s for s in good if s.strong]
    bad = [s for s in spans if not s.valid and not s.is_quantity]
    # money verbs with no verb keywords but a name + amount is still 'unknown' — never guess intent.
    return Extraction(intent, text, res, good, bad, kws, when, report_kind(text) if intent == "report_query" else None)
