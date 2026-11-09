"""PII filter for the public 'Break Munim' mode: never echo or log phone / Aadhaar / email / UPI ids."""
from __future__ import annotations

import re

_PATTERNS = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
    (re.compile(r"\b[\w.-]{2,}@(?:ok\w+|ybl|ibl|axl|paytm|upi|apl|sbi|hdfcbank|icici)\b", re.I), "[upi]"),
    (re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)"), "[phone]"),
    (re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)"), "[id-number]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[pan]"),
]


def redact(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def contains_pii(text: str) -> bool:
    return redact(text) != text
