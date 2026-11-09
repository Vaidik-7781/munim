"""Collections negotiator: two-way, policy-bounded, deterministic. The LLM may CLASSIFY a hard-to-read reply; it never sets a date,
an amount or an instalment, never replies, and never writes. All numbers come from the owner's policy and the ledger.

Opt-in per shop (`/policy on`). Inside the owner's policy the agent may (a) accept a promise date, (b) offer a split into equal
weekly instalments, and (c) record the agreed dates as `promise` entries (non-money, voidable, visible in /hisaab). OUTSIDE the
policy (long extension, big balance, disputed account, "I already paid", anything money-moving) it escalates to the owner as a
question — it never records a payment. A customer saying stop/wrong is handled first by the portal (instant). Every reply
discloses that it is an AI assistant, is Hindi/English by the customer's language, and is under the Governor's tone rules.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from munim.scribe.dates_hi import parse_when
from munim.scribe.extract import extract
from munim.scribe.indic_numbers import format_inr, paise_to_words, paise_to_words_en
from munim.scribe.names import Resolution

CANNOT = re.compile(r"\b(paise\s*nahi|paisa\s*nahi|nahi\s*hain?|abhi\s*nahi|tight|mushkil|thoda\s*ruk|baad\s*mein|can'?t\s*pay|no\s*money|not\s*now)\b", re.I)
AGREE = {"haan", "han", "ha", "yes", "ok", "okay", "theek", "thik", "manzoor", "sahi", "done", "ji", "ji haan"}
PAID = re.compile(r"\b(de\s*diya|de\s*diye|bhej\s*diya|already\s*paid|paid|kar\s*diya)\b", re.I)
ENUM = ("cannot_pay", "promise", "partial", "paid_claim", "other")


@dataclass
class Policy:
    enabled: bool = False
    max_extension_days: int = 14
    max_installments: int = 3
    max_auto_balance_paise: int = 300_000          # above this the owner decides
    min_installment_paise: int = 10_000

    @staticmethod
    def load(aux) -> "Policy":
        d = aux.get("neg_policy") or {}
        return Policy(**{k: v for k, v in d.items() if k in Policy.__dataclass_fields__})

    def save(self, aux) -> None:
        aux.put("neg_policy", asdict(self))


@dataclass
class NegResult:
    text: str
    voice: str | None = None
    tts_language: str = "hi-en"
    owner_notes: list[str] = field(default_factory=list)       # FYI lines for the owner
    escalate: str | None = None                                # a question the owner must answer
    recorded: list[str] = field(default_factory=list)          # ISO due dates written as promises


def classify(text: str, llm_label: str | None = None) -> str:
    if PAID.search(text):
        return "paid_claim"
    t = text.lower()
    has_when = parse_when(text, __import__("datetime").date.today()) is not None
    amt = bool(re.search(r"\d|sau|hazaar|hazar|paanch|das|pachas", t))
    if CANNOT.search(text) and has_when:
        return "promise"
    if CANNOT.search(text):
        return "cannot_pay"
    if has_when and re.search(r"\b(dunga|dungi|denge|dega|degi|de\s*dunga|bhejunga|bhejenge)\b", t):
        return "partial" if amt and re.search(r"baaki|abhi", t) else "promise"
    return llm_label if llm_label in ENUM else "other"


def split(balance: int, n: int, min_each: int) -> list[int]:
    n = max(1, n)
    while n > 1 and balance // n < min_each:
        n -= 1
    base = balance // n
    parts = [base] * n
    parts[-1] += balance - base * n
    return parts


def _t(en: bool, hi: str, e: str) -> str:
    return e if en else hi


DISCLOSE_HI, DISCLOSE_EN = "— Munim (AI sahayak)", "— Munim (AI assistant)"


def respond(tenant, cid: int, text: str, *, lang: str = "hi", llm_label: str | None = None) -> NegResult | None:
    """None = not handled here (policy off, or nothing negotiable): the caller falls back to the plain portal behaviour."""
    pol = Policy.load(tenant.aux)
    if not pol.enabled:
        return None
    L, svc = tenant.ledger, tenant.service
    en = lang == "en"
    tts = "en-IN" if en else "hi-en"
    today = svc._today()
    name = L.customer_row(cid)["name"]
    sig = DISCLOSE_EN if en else DISCLOSE_HI
    key = f"neg_offer:{cid}"
    offer = tenant.aux.get(key)
    low = " ".join(text.lower().replace("!", " ").replace(".", " ").split())

    if offer and low in AGREE and offer["exp"] > tenant._clock():       # customer accepts an instalment plan we offered
        tenant.aux.put(key, None)
        done = []
        for d, amt in offer["plan"]:
            L.add_promise(cid, d, amt, src="negotiator", auto=True, plan=True)
            done.append(d)
        msg = _t(en, f"Theek hai, {len(done)} kisht pakki. Har kisht ke din ek yaad dila dunga. Shukriya {sig}",
                 f"Agreed — {len(done)} instalments are set. I'll remind you on each date. Thank you {sig}")
        return NegResult(msg, msg, tts, [f"🤝 {name}: {len(done)} kisht ka plan maana (policy ke andar): "
                                         + ", ".join(f"{d} {format_inr(a)}" for d, a in offer["plan"])], recorded=done)

    kind = classify(text, llm_label)
    if kind == "paid_claim":
        return None                                                      # money claim -> portal path asks the owner
    if kind not in ("cannot_pay", "promise", "partial"):
        return None
    bal = L.balance(cid)
    if bal <= 0:
        m = _t(en, "Aapka koi baaki nahi hai. Shukriya!", "You have nothing outstanding. Thank you!")
        return NegResult(m, m, tts)
    if L.is_disputed(cid) or bal > pol.max_auto_balance_paise:
        why = "vivaad chal raha hai" if L.is_disputed(cid) else f"baaki {format_inr(bal)} policy se zyada hai"
        m = _t(en, "Maine aapki baat dukaan ko bata di hai; woh aapse seedha baat karenge.",
               "I've passed your message to the shop; they will talk to you directly.")
        return NegResult(m, m, tts, escalate=f"{name} ne kaha: “{text[:120]}”. Maine kuch tay nahi kiya ({why}).")

    cust = next(c for c in L.customers() if c.id == cid)
    ex = extract(text, [cust], today)
    ex.resolution = Resolution("match", cust, [(cust, 100.0)], None, 100.0)
    when = parse_when(text, today)
    paid_now = ex.amount_paise if kind == "partial" and ex.amount_paise and ex.amount_paise < bal else None

    if when is not None and today <= when.day <= today + timedelta(days=pol.max_extension_days):
        amt = bal if paid_now is None else bal - paid_now
        L.add_promise(cid, when.day.isoformat(), amt, src="negotiator", auto=True)
        m = _t(en, f"Theek hai, {when.day.strftime('%d-%m')} tak {format_inr(amt)} dene ka vaada likh liya. Tab tak reminder nahi aayega. {sig}",
               f"Okay — noted {format_inr(amt)} by {when.day.strftime('%d-%m')}. No reminders until then. {sig}")
        v = _t(en, f"Theek hai, {when.day.strftime('%d')} tarikh tak {paise_to_words(amt)} dene ka vaada likh liya. {sig}",
               f"Okay, noted {paise_to_words_en(amt)} by the {when.day.strftime('%d')}. {sig}")
        note = f"🤝 {name}: {when.day.isoformat()} tak {format_inr(amt)} ka vaada (auto, policy ke andar)"
        if paid_now:
            note += f"; abhi {format_inr(paid_now)} dene ko bola — mile to khud likhein"
        return NegResult(m, v, tts, [note], recorded=[when.day.isoformat()])

    if when is not None:                                                # beyond policy -> counter with instalments, ask customer
        pass
    parts = split(bal, pol.max_installments, pol.min_installment_paise)
    if len(parts) == 1 and when is None:
        parts = [bal]
    plan = [((today + timedelta(days=7 * (i + 1))).isoformat(), a) for i, a in enumerate(parts)]
    tenant.aux.put(key, {"plan": plan, "exp": tenant._clock() + 86400})
    lines = ", ".join(f"{d[8:]}-{d[5:7]}: {format_inr(a)}" for d, a in plan)
    m = _t(en, f"Samajh gaya. Kya aap {len(plan)} kisht mein de sakte hain? {lines}. Theek ho to “haan” likhein. {sig}",
           f"Understood. Could you pay in {len(plan)} instalments? {lines}. Reply “yes” if that works. {sig}")
    return NegResult(m, None, tts, [f"💬 {name} ko {len(plan)} kisht ka plan offer kiya (policy ke andar): {lines}. Abhi kuch likha nahi gaya."])
