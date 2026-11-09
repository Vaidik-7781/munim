"""Auditor: independent verification before a single paisa is written.

Disagreement between the two STT passes is treated as UNCERTAINTY, never averaged away.
Any flag -> BLOCK (explicit haan/nahi from the shopkeeper). No flags -> SOFT (write now, 30 s undo).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from munim.config import Settings
from munim.scribe.extract import Extraction, _MONEY_INTENTS
from munim.scribe.names import Customer

NEEDS_CUSTOMER = {
    "add_udhaar", "record_payment", "balance_query", "send_reminder", "pause_customer",
    "resume_customer", "dispute_entry", "opt_out_customer", "promise_to_pay",
}
OK_LANGS = {None, "", "hi", "hi-IN", "en", "en-IN", "hi-en", "hinglish"}

SOFT, BLOCK, ASK, ACT = "SOFT", "BLOCK", "ASK", "ACT"


@dataclass
class Flag:
    code: str
    detail: str = ""


@dataclass
class AuditContext:
    a: Extraction
    b: Extraction | None            # None = second pass failed/missing
    snr_db: float | None = None
    duration_s: float | None = None
    lang_a: str | None = None
    history_paise: list[int] = field(default_factory=list)   # recent entries for this customer
    recent: list[tuple[int, str, int, float]] = field(default_factory=list)
    """recent writes as (customer_id, intent, paise, age_seconds)"""
    require_b: bool = True
    llm: Extraction | None = None              # optional third opinion; can only ADD flags
    balance_paise: int = 0
    credit_limit_paise: int | None = None      # only enforced if the shopkeeper set one


@dataclass
class AuditResult:
    decision: str
    flags: list[Flag]
    intent: str
    customer: Customer | None
    amount_paise: int | None
    alt_amount_paise: int | None = None
    candidates: list[Customer] = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return [f.code for f in self.flags]


def _power_of_ten_ratio(x: int, y: int) -> bool:
    if x <= 0 or y <= 0 or x == y:
        return False
    hi, lo = max(x, y), min(x, y)
    if hi % lo:
        return False
    q = hi // lo
    while q % 10 == 0:
        q //= 10
    return q == 1


def audit(ctx: AuditContext, cfg: Settings) -> AuditResult:
    a, b = ctx.a, ctx.b
    flags: list[Flag] = []
    intent = a.intent
    money = intent in _MONEY_INTENTS

    # ---- non-money, no-customer intents -----------------------------------------------
    if intent in ("undo_last", "help"):
        return AuditResult(ACT, [], intent, None, None)
    if intent == "unknown":
        return AuditResult(ASK, [Flag("unknown_intent")], intent, None, None)

    # ---- customer resolution ----------------------------------------------------------
    res = a.resolution
    cust = res.customer
    cands: list[Customer] = []
    if intent in NEEDS_CUSTOMER:
        if res.status == "ask":
            flags.append(Flag("ambiguous_name", ", ".join(c.name for c, _ in res.candidates)))
            cands = [c for c, _ in res.candidates]
        elif res.status == "none":
            flags.append(Flag("missing_customer"))

    if intent == "promise_to_pay" and a.when is None:
        flags.append(Flag("missing_date"))
    if a.rescued_customer:
        flags.append(Flag("name_rescued", "name found only by the transliteration pass"))
    if a.inferred_customer:
        flags.append(Flag("customer_inferred", "from conversation context"))
    if not money:
        decision = ASK if flags else ACT
        return AuditResult(decision, flags, intent, cust, None, None, cands)

    # ---- money checks -----------------------------------------------------------------
    amt = a.amount_paise
    alt: int | None = None
    if amt is None:
        flags.append(Flag("missing_amount", "no valid amount heard" if not a.invalid_spans else "garbled amount"))

    if b is None:
        if ctx.require_b:
            flags.append(Flag("b_missing", "second STT pass unavailable"))
    else:
        if b.intent != intent:
            flags.append(Flag("intent_disagreement", f"{intent} vs {b.intent}"))
        if b.amount_paise != amt:
            flags.append(Flag("amount_disagreement", f"{amt} vs {b.amount_paise}"))
            alt = b.amount_paise
            if amt is not None and b.amount_paise is not None and _power_of_ten_ratio(amt, b.amount_paise):
                flags.append(Flag("scale_risk", f"{amt} vs {b.amount_paise}"))
        if res.status == "match" and b.resolution.status == "match" and b.customer and cust and b.customer.id != cust.id:
            flags.append(Flag("name_disagreement", f"{cust.name} vs {b.customer.name}"))

    if ctx.llm is not None:
        L = ctx.llm
        if L.intent != intent:
            flags.append(Flag("llm_disagreement", f"intent {intent} vs {L.intent}"))
        elif money and L.amount_paise != amt:
            flags.append(Flag("llm_disagreement", f"amount {amt} vs {L.amount_paise}"))
        elif L.customer is not None and cust is not None and L.customer.id != cust.id:
            flags.append(Flag("llm_disagreement", "customer"))

    if len(a.spans) > 1:
        flags.append(Flag("multi_amount", "+".join(str(s.paise) for s in a.spans)))

    if ctx.snr_db is not None and ctx.snr_db < cfg.low_snr_db:
        flags.append(Flag("low_snr", f"{ctx.snr_db:.1f} dB"))
    if ctx.duration_s is not None and ctx.duration_s < cfg.short_clip_seconds:
        flags.append(Flag("short_clip", f"{ctx.duration_s:.2f}s"))
    if ctx.lang_a not in OK_LANGS:
        flags.append(Flag("language_switch", str(ctx.lang_a)))

    if amt is not None:
        hist = ctx.history_paise
        if len(hist) >= 3:
            limit = max(cfg.outlier_multiple * statistics.median(hist), cfg.outlier_floor_paise)
        else:
            limit = cfg.outlier_floor_no_history_paise if not hist else max(
                cfg.outlier_multiple * statistics.median(hist), cfg.outlier_floor_paise)
        if amt > limit:
            flags.append(Flag("outlier", f"{amt} > {int(limit)}"))
        if intent == "add_udhaar" and ctx.credit_limit_paise and ctx.balance_paise + amt > ctx.credit_limit_paise:
            flags.append(Flag("over_limit", f"{ctx.balance_paise + amt} > {ctx.credit_limit_paise}"))
        if cust is not None:
            for cid, it, p, age in ctx.recent:
                if cid == cust.id and it == intent and p == amt and age <= cfg.duplicate_window_seconds:
                    flags.append(Flag("duplicate", f"{age:.0f}s ago"))
                    break

    decision = BLOCK if flags else SOFT
    return AuditResult(decision, flags, intent, cust, amt, alt, cands)
