"""Parse a pasted UPI / bank credit notification into (amount, payer, reference).

Deterministic regexes only; no LLM. Shopkeepers get payments by UPI all day and forget to tick them off the khata —
this turns the SMS / app notification they already receive into a *proposed* record_payment.
Credits only: a debit message ("debited", "paid to", "sent to") is refused so money going OUT is never booked as a collection.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_AMT = re.compile(r"(?:rs\.?|inr|₹|rupees)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)
_AMT_POST = re.compile(r"\b([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:rs\.?|rupees|inr)\b", re.I)
_CREDIT = re.compile(r"\b(credited|received|paid\s+you|sent\s+you|deposited|has\s+sent\s+you|received\s+from)\b", re.I)
_DEBIT = re.compile(r"\b(debited|paid\s+to|sent\s+to|spent|withdrawn|purchase|payment\s+of\s+.*\s+to)\b", re.I)
_REF = re.compile(r"(?:ref(?:erence)?(?:\s*no\.?)?|upi(?:\s*ref)?|rrn|utr|txn(?:\s*id)?)\D{0,4}(\d{9,18})\b", re.I)
_REF_BARE = re.compile(r"\b(\d{12})\b")
_STOP = r"(?=\s+(?:on|via|ref|upi|vpa|to|in|a/c|ac|acct|account|bank|using|at|for|is|has|successfully)\b|\s*[(.,;:\n]|\s*$)"
_FROM = re.compile(r"\b(?:from|by)\s+(?:vpa\s+)?([A-Za-z][A-Za-z .'@_-]{1,40}?)" + _STOP, re.I)
_PAID_YOU = re.compile(r"([A-Za-z][A-Za-z .'-]{1,40}?)\s+(?:has\s+)?(?:paid|sent)\s+you", re.I)
_VPA = re.compile(r"\b([a-z0-9._-]{3,30})@([a-z]{2,15})\b", re.I)
_NOISE = {"upi", "bank", "neft", "imps", "rtgs", "paytm", "phonepe", "gpay", "google", "pay", "your", "the"}


@dataclass
class UpiCredit:
    amount_paise: int
    payer: str | None        # best-effort display name, to be fuzzy-matched by the caller
    ref: str | None          # transaction reference (used ONLY as a keyed pseudonym for de-duplication)
    vpa: str | None = None


def _paise(s: str) -> int:
    s = s.replace(",", "")
    whole, _, frac = s.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2]) if frac else int(whole) * 100


def _clean_name(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip(" .-_")
    if "@" in raw:
        raw = raw.split("@")[0]
    raw = re.sub(r"[._-]+", " ", raw)
    toks = [t for t in raw.split() if t.lower() not in _NOISE and not t.isdigit()]
    return " ".join(toks).title() or None


def looks_like_upi(text: str) -> bool:
    return bool((_AMT.search(text) or _AMT_POST.search(text)) and (_CREDIT.search(text) or _DEBIT.search(text)))


def parse_upi(text: str) -> UpiCredit | None:
    """Returns None when the text is not a credit, or is ambiguous (several different amounts, a debit)."""
    if not looks_like_upi(text) or _DEBIT.search(text) and not _CREDIT.search(text):
        return None
    if _DEBIT.search(text):                               # mixed credit+debit wording: refuse rather than guess
        return None
    amts = {_paise(m) for m in _AMT.findall(text)} | {_paise(m) for m in _AMT_POST.findall(text)}
    amts.discard(0)
    # a "Bal: Rs 12,000" / "Avl Bal" figure is not the payment: drop numbers that follow a balance label
    for bm in re.finditer(r"(?:avl\.?\s*bal|available\s*bal(?:ance)?|bal(?:ance)?)\D{0,6}(?:rs\.?|inr|₹)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text, re.I):
        amts.discard(_paise(bm.group(1)))
    if len(amts) != 1:
        return None
    ref_m = _REF.search(text) or _REF_BARE.search(text)
    vpa_m = _VPA.search(text)
    payer = None
    if vpa_m and re.search(r"\bvpa\b", text, re.I):
        payer = _clean_name(vpa_m.group(1))
    m = None if payer else (_PAID_YOU.search(text) or _FROM.search(text))
    if m:
        payer = _clean_name(m.group(1))
    if payer is None and vpa_m:
        payer = _clean_name(vpa_m.group(1))
    return UpiCredit(amts.pop(), payer, ref_m.group(1) if ref_m else None, vpa_m.group(0) if vpa_m else None)
