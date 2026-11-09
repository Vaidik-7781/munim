"""Reconciliation agent: match an incoming credit (UPI SMS / AA bank line) to the udhaar book. It PROPOSES; the owner approves.

Deterministic scoring (no LLM): name similarity + amount relationships + promise context, with an explicit ambiguity rule.
An amount match alone can never reach HIGH confidence. Nothing is written to the money chain here: `queue` puts a
`reconcile_payment` approval in the ledger's approval queue, and only `apply` (called after the owner's one tap) records the payment.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from munim.insights import engine as E
from munim.ledger.chain import Ledger

DAY = 86400.0
KIND = "reconcile_payment"


@dataclass
class Candidate:
    customer_id: int
    name: str
    score: int
    tier: str
    reasons: list[str] = field(default_factory=list)


def tier(score: int) -> str:
    return "HIGH" if score >= 80 else "MEDIUM" if score >= 55 else "LOW"


def match(ledger: Ledger, amount_paise: int, payer: str | None, now: float) -> tuple[list[Candidate], bool]:
    """-> (candidates best-first, ambiguous). Only customers with money outstanding are considered."""
    out: list[Candidate] = []
    for c, bal in E.outstanding(ledger):
        score, why = 0, []
        ns = 0
        if payer:
            ns = max(fuzz.token_set_ratio(payer.lower(), c.name.lower()), fuzz.partial_ratio(payer.lower(), c.name.lower()) - 10)
            if ns >= 85:
                score += 50; why.append(f"naam milta hai ({ns}%)")
            elif ns >= 70:
                score += 30; why.append(f"naam kuch milta hai ({ns}%)")
        if amount_paise == bal:
            score += 35; why.append("raqam poore baaki ke barabar")
        elif amount_paise in {i.remaining for i in E.open_items(ledger, c.id)}:
            score += 25; why.append("raqam ek purani entry ke barabar")
        elif amount_paise < bal:
            score += 8; why.append("raqam baaki se kam (hissa)")
        else:
            score -= 15; why.append("raqam baaki se zyada")
        p = E.active_promise(ledger, c.id, now)
        if p is not None and (p.meta.get("promised_paise") in (None, amount_paise)):
            score += 12; why.append("vaada chal raha tha")
        if ledger.is_disputed(c.id):
            score -= 40; why.append("vivaad chal raha hai")
        if ns < 70:
            score = min(score, 45)                          # amount-only evidence can never be HIGH
        if score > 0:
            out.append(Candidate(c.id, c.name, min(score, 100), tier(min(score, 100)), why))
    out.sort(key=lambda x: -x.score)
    amb = len(out) >= 2 and out[0].score - out[1].score < 10 and out[1].score >= 55
    return out, amb


def queue(ledger: Ledger, amount_paise: int, payer: str | None, token: str | None, now: float, src: str = "sms") -> dict:
    if token and ledger.meta_token_seen("upi", token):
        return {"status": "duplicate"}
    cands, amb = match(ledger, amount_paise, payer, now)
    if not cands or cands[0].tier == "LOW":
        return {"status": "unmatched"}
    if amb:
        return {"status": "ambiguous", "candidates": [c.name for c in cands[:3]]}
    top = cands[0]
    aid = ledger.queue_approval(top.customer_id, KIND, {"amount_paise": amount_paise, "upi": token, "src": src, "payer": (payer or "")[:40]},
                                "; ".join(top.reasons), priority=top.score)
    return {"status": "queued", "approval": aid, "tier": top.tier, "customer_id": top.customer_id}


def pending(ledger: Ledger) -> list[dict]:
    return [a for a in ledger.pending_approvals() if a["kind"] == KIND]


def apply(ledger: Ledger, approval_id: int, by: str = "shopkeeper"):
    item = ledger.decide_approval(approval_id, True, by)
    if item is None or item["kind"] != KIND:
        return None
    pl = item["payload"]
    meta = {"src": "reconcile", "confirmed": True, "via": pl.get("src", "sms")}
    if pl.get("upi"):
        if ledger.meta_token_seen("upi", pl["upi"]):
            return None
        meta["upi"] = pl["upi"]
    return ledger.record_payment(item["customer_id"], pl["amount_paise"], **meta)


def reject(ledger: Ledger, approval_id: int) -> None:
    ledger.decide_approval(approval_id, False)


@dataclass
class Digest:
    text: str
    ids: list[int]
    buttons: list[tuple[str, str]]


def daily_digest(ledger: Ledger, now: float) -> Digest | None:
    from munim.scribe.indic_numbers import format_inr
    items = pending(ledger)
    if not items:
        return None
    names = {c.id: c.name for c in ledger.customers()}
    lines = [f"• {names.get(i['customer_id'], '?')}: {format_inr(i['payload']['amount_paise'])} ({i['reason'][:50]})" for i in items[:10]]
    return Digest("Kal ke UPI/bank credit — yeh payments inke naam likhni hain?\n" + "\n".join(lines), [i["id"] for i in items],
                  [("Sab sahi ✅", "x:recon:all"), ("Dekhein 👀", "x:recon:review"), ("Chhodo ❌", "x:recon:none")])
