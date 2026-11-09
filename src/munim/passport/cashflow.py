"""Cash-Flow Passport: a transparent, rule-based summary of a shop's udhaar book that a lender or insurer can read.

NOT an ML score and NOT a credit decision. Every point is traceable to a stated rule (`reasons`), the inputs are the
hash-chained ledger (verified), and sharing is consent-scoped: `share()` appends a `passport_shared` entry (recipient
label + field list + hash) to the chain, and the exported document is HMAC-signed so a recipient can detect edits.
It carries aggregates only — no customer names, phones or refs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import statistics
from dataclasses import asdict, dataclass, field

from munim.insights import engine as E
from munim.ledger.chain import Ledger

DAY = 86400.0
WINDOW_DAYS = 90
SHARE_FIELDS = ("score", "tier", "confidence", "window_days", "udhaar_given_paise", "collected_paise",
                "collection_ratio_pct", "outstanding_paise", "overdue_30d_pct", "top5_share_pct",
                "weekly_collection_median_paise", "weekly_collection_cv_pct", "promise_keep_pct",
                "reversal_rate_pct", "history_days", "chain_ok", "active_customers")


@dataclass
class Passport:
    score: int
    tier: str                                  # A (strong) .. D (weak) | "NA" if not enough history
    confidence: str                            # low | medium | high  (by amount of history, NOT by score)
    window_days: int
    udhaar_given_paise: int
    collected_paise: int
    collection_ratio_pct: int
    outstanding_paise: int
    overdue_30d_pct: int
    top5_share_pct: int
    weekly_collection_median_paise: int
    weekly_collection_cv_pct: int
    promise_keep_pct: int | None
    reversal_rate_pct: int
    history_days: int
    chain_ok: bool
    active_customers: int
    reasons: list[str] = field(default_factory=list)
    generated_ts: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


def _pct(a: int, b: int) -> int:
    return int(round(100 * a / b)) if b > 0 else 0


def build(ledger: Ledger, now: float, window_days: int = WINDOW_DAYS) -> Passport:
    t0 = now - window_days * DAY
    given = collected = outstanding = overdue30 = 0
    first_ts = now
    weekly: dict[int, int] = {}
    kept = broken = 0
    balances: list[int] = []
    active = 0
    all_entries = ledger.entries()
    n_money = sum(1 for e in all_entries if e.kind in ("add_udhaar", "record_payment"))
    n_rev = sum(1 for e in all_entries if e.kind == "reversal")
    for c in ledger.customers():
        live = E.live_money_entries(ledger, c.id)
        if live:
            first_ts = min(first_ts, live[0].ts)
        recent = [e for e in live if e.ts >= t0]
        if recent:
            active += 1
        for e in recent:
            if e.kind == "add_udhaar":
                given += e.amount_paise
            else:
                collected += e.amount_paise
                weekly[int((now - e.ts) // (7 * DAY))] = weekly.get(int((now - e.ts) // (7 * DAY)), 0) + e.amount_paise
        bal = ledger.balance(c.id)
        if bal > 0:
            outstanding += bal
            balances.append(bal)
        for _, st in E.promise_statuses(ledger, c.id, now):
            kept += st == "kept"
            broken += st == "broken"
    for c in ledger.customers():
        for it in E.open_items(ledger, c.id):
            if (now - it.ts) >= 30 * DAY:
                overdue30 += it.remaining
    weeks = max(1, min(window_days // 7, int((now - first_ts) // (7 * DAY)) + 1))
    series = [weekly.get(w, 0) for w in range(weeks)]
    med = int(statistics.median(series)) if series else 0
    cv = int(round(100 * statistics.pstdev(series) / (sum(series) / len(series)))) if len(series) > 1 and sum(series) else 0
    for c in ledger.customers():
        for it in E.open_items(ledger, c.id):
            if (now - it.ts) >= 30 * DAY:
                overdue30 += it.remaining
    top5 = sum(sorted(balances, reverse=True)[:5])
    ok, _, _ = ledger.verify()
    hist_days = int((now - first_ts) // DAY) if given or collected else 0
    ratio = _pct(collected, given)
    p = Passport(0, "NA", "low", window_days, given, collected, ratio, outstanding, _pct(overdue30, outstanding),
                 _pct(top5, outstanding), med, cv, _pct(kept, kept + broken) if (kept + broken) else None,
                 _pct(n_rev, n_money), hist_days, ok, active, [], now)
    p.confidence = "high" if hist_days >= 120 and n_money >= 150 else "medium" if hist_days >= 45 and n_money >= 40 else "low"
    if hist_days < 21 or n_money < 15:
        p.reasons.append("history too short (<21 days or <15 entries): no tier is assigned")
        return p
    score, why = 50, []

    def add(points: int, text: str) -> None:
        nonlocal score
        score += points
        why.append(f"{'+' if points >= 0 else ''}{points}  {text}")

    add(15 if ratio >= 85 else 8 if ratio >= 65 else -5 if ratio >= 40 else -15, f"collected {ratio}% of what was given on udhaar (90 d)")
    od = p.overdue_30d_pct
    add(10 if od <= 10 else 0 if od <= 30 else -10 if od <= 50 else -18, f"{od}% of outstanding is older than 30 days")
    add(6 if p.top5_share_pct <= 40 else 0 if p.top5_share_pct <= 70 else -8, f"top-5 debtors hold {p.top5_share_pct}% of outstanding")
    add(8 if cv <= 40 else 0 if cv <= 80 else -8, f"weekly collections vary by {cv}% (lower = steadier)")
    if p.promise_keep_pct is not None:
        add(8 if p.promise_keep_pct >= 70 else -6 if p.promise_keep_pct < 40 else 0, f"{p.promise_keep_pct}% of promises kept")
    add(-6 if p.reversal_rate_pct > 15 else 0, f"{p.reversal_rate_pct}% of money entries were reversed")
    add(5 if ok else -30, "hash chain verifies" if ok else "hash chain FAILED verification")
    p.score = max(0, min(100, score))
    p.tier = "A" if p.score >= 75 else "B" if p.score >= 60 else "C" if p.score >= 45 else "D"
    p.reasons = why
    return p


def share(ledger: Ledger, p: Passport, recipient: str, secret: bytes, fields: tuple[str, ...] = SHARE_FIELDS) -> dict:
    """Signed, field-limited export. Appends a chain entry so the owner can always see who got what."""
    body = {k: v for k, v in p.as_dict().items() if k in fields}
    doc = {"kind": "munim_cashflow_passport", "v": 1, "recipient": recipient[:60], "generated_ts": p.generated_ts, "data": body}
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    doc["sig"] = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()
    anchor = ledger.customers(include_erased=True)
    if anchor:                                           # chain entries are per-customer-ref; anchor to the first for audit
        ledger.append("passport_shared", anchor[0].id, 0,
                      meta={"to": recipient[:60], "fields": sorted(body), "sha": hashlib.sha256(raw.encode()).hexdigest()[:16]})
    return doc


def verify_share(doc: dict, secret: bytes) -> bool:
    sig = doc.get("sig", "")
    raw = json.dumps({k: v for k, v in doc.items() if k != "sig"}, sort_keys=True, separators=(",", ":"))
    return hmac.compare_digest(sig, hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest())


def text(p: Passport) -> str:
    if p.tier == "NA":
        return "📒 Cash-Flow Passport abhi nahi ban sakta — " + p.reasons[0]
    from munim.scribe.indic_numbers import format_inr
    return (f"📒 Cash-Flow Passport: tier {p.tier} (score {p.score}/100, bharosa: {p.confidence})\n"
            f"Diya {format_inr(p.udhaar_given_paise)} • Vasool {format_inr(p.collected_paise)} • Baaki {format_inr(p.outstanding_paise)}\n"
            + "\n".join(p.reasons) + "\nYeh sirf rules ka hisaab hai, loan ya bima ka faisla nahi.")
