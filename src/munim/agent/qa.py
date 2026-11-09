"""Ask-anything agent over the khata — READ-ONLY.

Design: the LLM (optional, Groq) only picks WHICH query tool answers the question. It never sees customer names
(we swap them for C<id> placeholders first), never writes a number, and cannot call anything that changes state.
Every figure in the answer comes from the ledger through deterministic code. No LLM key / bad LLM output -> rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from munim.insights import engine as E
from munim.insights.forecast import forecast
from munim.insights.reports import render, report_kind
from munim.ledger.chain import Ledger
from munim.llm.client import LLMClient, LLMError
from munim.scribe.indic_numbers import format_inr, paise_to_words, rupees_to_words
from munim.scribe.names import Customer, resolve
from munim.timeutil import IST

DAY = 86400.0
TOOLS = ("balance", "history", "last_payment", "aging", "risk", "top", "overdue", "total", "today", "week", "forecast")
NEEDS_CUSTOMER = {"balance", "history", "last_payment", "aging"}

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("last_payment", re.compile(r"\b(kab\s+(diya|chukaya|payment|aaya|aayi)|last\s+payment|aakhri\s+payment|akhri\s+payment|pichli\s+payment|kab\s+se\s+payment)\b", re.I)),
    ("history", re.compile(r"\b(history|entries|entry\s+dikhao|kya\s+kya\s+(liya|likha)|pichli\s+entries|khata\s+dikhao)\b", re.I)),
    ("aging", re.compile(r"\b(kitna\s+purana|kab\s+se\s+baaki|ageing|aging|umr)\b", re.I)),
    ("forecast", re.compile(r"\b(forecast|kitna\s+(aayega|aa\s+sakta)|vasooli\s+kitni|agle\s+(hafte|din)|expected\s+collection|andaza)\b", re.I)),
]
_BAL = re.compile(r"\b(kitna|baaki|balance|hisaab|hisab|bakaya)\b", re.I)


@dataclass
class QA:
    text: str
    voice: str | None = None
    tool: str | None = None
    via: str = "rules"            # rules | llm


def rule_tool(text: str) -> str | None:
    for tool, pat in _RULES:
        if pat.search(text):
            return tool
    k = report_kind(text)
    if k and k != "plan":
        return k
    if _BAL.search(text):
        return "balance"
    return None


def is_new_query(text: str) -> bool:
    """True for the question kinds service.on_typed/on_heard should answer directly (not the older intents)."""
    return any(p.search(text) for _, p in _RULES)


def _redact(question: str, customers: list[Customer]) -> tuple[str, set[int]]:
    seen: set[int] = set()
    out = question
    for c in customers:
        parts = sorted({c.name, *[p for p in c.name.split() if len(p) >= 3], *c.aliases}, key=len, reverse=True)
        for p in parts:
            new = re.sub(rf"(?<!\w){re.escape(p)}(?!\w)", f"C{c.id}", out, flags=re.I)
            if new != out:
                seen.add(c.id)
                out = new
    return out, seen


SYSTEM = (
    "You route a shopkeeper's question about their credit ledger to ONE read-only query tool. "
    "Tools: balance(customer) history(customer) last_payment(customer) aging(customer) risk(customer?) top overdue "
    "total today week forecast. Customers appear only as C<number>. Never answer the question yourself and never output "
    'numbers of money. Reply JSON only: {"tool": "<name>", "customer": "C<number>" or null}.'
)


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%d %b")


def _days_words(n: int) -> str:
    return rupees_to_words(max(n, 0))


def run_tool(ledger: Ledger, now: float, tool: str, cid: int | None) -> QA:
    if tool in ("top", "overdue", "total", "today", "week"):
        r = render(tool, ledger, now)
        return QA(r.text, r.voice, tool)
    if tool == "forecast":
        f = forecast(ledger, now, 14)
        return QA(f"Agle 14 din mein vasooli ka andaza: {format_inr(f['expected_paise'])} "
                  f"(sambhav {format_inr(f['low_paise'])} se {format_inr(f['high_paise'])}). Yeh statistical andaza hai, guarantee nahi.",
                  f"Agle chaudah din mein lagbhag {paise_to_words(f['expected_paise'])} vasooli ka andaza hai.", tool)
    if tool == "risk" and cid is None:
        r = render("risk", ledger, now)
        return QA(r.text, r.voice, tool)
    if cid is None:
        return QA("Kiske baare mein? Naam bhi bataiye.", None, tool)
    name = ledger.customer_row(cid)["name"]
    first = name.split()[0]
    bal = ledger.balance(cid)
    if tool == "balance":
        return QA(f"{name} ka baaki: {format_inr(bal)}", f"{first} ka baaki {paise_to_words(max(bal, 0))} hai.", tool)
    if tool == "risk":
        rk = E.risk(ledger, cid, now)
        return QA(f"{name}: {rk.tier.upper()} (score {rk.score})\n" + "\n".join(f"• {t}" for _, t in rk.reasons),
                  None, tool)
    if tool == "aging":
        ag = E.aging(ledger, cid, now)
        body = ", ".join(f"{k} din: {format_inr(v)}" for k, v in ag.items() if v) or "kuch baaki nahi"
        return QA(f"{name} ka baaki umr ke hisaab se — {body}", None, tool)
    ents = E.live_money_entries(ledger, cid)
    if tool == "last_payment":
        pays = [e for e in ents if e.kind == "record_payment"]
        if not pays:
            return QA(f"{name} ki abhi tak koi payment darj nahi hai.", f"{first} ki abhi tak koi payment darj nahi hai.", tool)
        e = pays[-1]
        d = int((now - e.ts) // DAY)
        return QA(f"{name} ne aakhri baar {_fmt(e.ts)} ko {format_inr(e.amount_paise)} diye ({d} din pehle).",
                  f"{first} ne aakhri baar {_days_words(d)} din pehle {paise_to_words(e.amount_paise)} diye.", tool)
    if tool == "history":
        rows = ents[-8:]
        if not rows:
            return QA(f"{name} ke khate mein abhi koi entry nahi.", None, tool)
        lines = [f"{_fmt(e.ts)}  {'udhaar' if e.kind == 'add_udhaar' else 'jama  '}  {format_inr(e.amount_paise)}" for e in rows]
        return QA(f"{name} — pichli {len(rows)} entry:\n" + "\n".join(lines) + f"\nBaaki: {format_inr(bal)}",
                  f"{first} ka kul baaki {paise_to_words(max(bal, 0))} hai.", tool)
    return QA("Samajh nahi aaya.")


async def answer(ledger: Ledger, now: float, question: str, llm: LLMClient | None = None) -> QA:
    customers = ledger.customers()
    r = resolve(question, customers)
    if r.status == "ask":
        names = ", ".join(c.name for c, _ in r.candidates[:4])
        return QA(f"Kaun sa customer? {names}", None, None)
    cid = r.customer.id if r.status == "match" else None
    tool, via = rule_tool(question), "rules"
    if tool is None and llm is not None and llm.enabled:
        red, seen = _redact(question, customers)
        try:
            raw = await llm.chat_json(SYSTEM, red, max_tokens=60)
            t = str(raw.get("tool", ""))
            c = raw.get("customer")
            if t in TOOLS:
                if isinstance(c, str) and re.fullmatch(r"C\d+", c):
                    if int(c[1:]) in seen:
                        cid = int(c[1:])
                    else:
                        t = ""                  # the model invented a customer we never showed it
                if t:
                    tool, via = t, "llm"
        except (LLMError, AttributeError, TypeError, ValueError):
            pass
    if tool is None:
        return QA("Yeh sawaal samajh nahi aaya. Aise puchhein: “Sharma ji ka kitna baaki?”, “Sharma ne last payment kab di?”, "
                  "“Sabse zyada udhaar kiska?”, “Agle hafte kitni vasooli?”")
    if tool in NEEDS_CUSTOMER and cid is None:
        return QA("Kiske baare mein? Naam bhi bataiye.", None, tool)
    out = run_tool(ledger, now, tool, cid)
    out.via = via
    return out


def answer_rules(ledger: Ledger, now: float, question: str) -> tuple[QA | None, int | None]:
    """Synchronous, rules-only version used inline by the conversation service. Returns (QA|None, customer_id)."""
    r = resolve(question, ledger.customers())
    if r.status == "ask":
        return QA("Kaun sa customer? " + ", ".join(c.name for c, _ in r.candidates[:4])), None
    cid = r.customer.id if r.status == "match" else None
    tool = rule_tool(question)
    if tool is None:
        return None, cid
    if tool in NEEDS_CUSTOMER and cid is None:
        return QA("Kiske baare mein? Naam bhi bataiye.", None, tool), None
    return run_tool(ledger, now, tool, cid), cid
