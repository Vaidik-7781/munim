"""Deterministic analytics over the ledger. Code computes every number; nothing here calls an LLM.

* FIFO aging (payments clear the oldest udhaar first), explainable risk tiers, weekly flow,
  promise-to-pay tracking, anomaly detection (robust MAD), priority ranking for the collections agent.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from munim.timeutil import IST
from munim.ledger.chain import MONEY_KINDS, Entry, Ledger

BUCKETS = [("0-7", 0, 7), ("8-30", 8, 30), ("31-60", 31, 60), ("61-90", 61, 90), ("90+", 91, 10 ** 6)]
DAY = 86400.0


def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, IST)


def live_money_entries(ledger: Ledger, cid: int) -> list[Entry]:
    rev = ledger._reversed_seqs()
    return [e for e in ledger.entries(cid) if e.kind in MONEY_KINDS and e.seq not in rev]


@dataclass
class OpenItem:
    ts: float
    remaining: int


def open_items(ledger: Ledger, cid: int) -> list[OpenItem]:
    """FIFO: each payment reduces the oldest unpaid udhaar first."""
    items: list[OpenItem] = []
    for e in live_money_entries(ledger, cid):
        if e.kind == "add_udhaar":
            items.append(OpenItem(e.ts, e.amount_paise))
        else:
            left = e.amount_paise
            for it in items:
                if left <= 0:
                    break
                take = min(it.remaining, left)
                it.remaining -= take
                left -= take
    return [i for i in items if i.remaining > 0]


def aging(ledger: Ledger, cid: int, now: float) -> dict[str, int]:
    out = {name: 0 for name, _, _ in BUCKETS}
    for it in open_items(ledger, cid):
        days = int((now - it.ts) // DAY)
        for name, lo, hi in BUCKETS:
            if lo <= days <= hi:
                out[name] += it.remaining
                break
    return out


def oldest_open_days(ledger: Ledger, cid: int, now: float) -> int:
    its = open_items(ledger, cid)
    return int((now - min(i.ts for i in its)) // DAY) if its else 0


def last_payment_ts(ledger: Ledger, cid: int) -> float | None:
    pays = [e.ts for e in live_money_entries(ledger, cid) if e.kind == "record_payment"]
    return max(pays) if pays else None


# ------------------------------------------------------------------ promises
def _due_end_ts(due_iso: str) -> float:
    d = date.fromisoformat(due_iso)
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=IST).timestamp()


GRACE_DAYS = 2


def promise_statuses(ledger: Ledger, cid: int, now: float) -> list[tuple[Entry, str]]:
    """pending (due not passed) | kept | broken. A payment after the promise counts toward it."""
    pays = [e for e in live_money_entries(ledger, cid) if e.kind == "record_payment"]
    out = []
    for p in ledger.promises(cid):
        due_ts = _due_end_ts(p.meta["due"])
        want = p.meta.get("promised_paise") or 1
        paid = sum(e.amount_paise for e in pays if p.ts <= e.ts <= due_ts + GRACE_DAYS * DAY)
        if paid >= want:
            out.append((p, "kept"))
        elif now <= due_ts:
            out.append((p, "pending"))
        elif now <= due_ts + GRACE_DAYS * DAY:
            out.append((p, "grace"))
        else:
            out.append((p, "broken"))
    return out


def active_promise(ledger: Ledger, cid: int, now: float) -> Entry | None:
    for p, st in promise_statuses(ledger, cid, now):
        if st in ("pending", "grace") and ledger.balance(cid) > 0:
            return p
    return None


# ------------------------------------------------------------------ risk
@dataclass
class RiskReport:
    score: int
    tier: str
    reasons: list[tuple[str, str]] = field(default_factory=list)   # (code, Hinglish text)

    @property
    def codes(self) -> list[str]:
        return [c for c, _ in self.reasons]


def risk(ledger: Ledger, cid: int, now: float) -> RiskReport:
    score, why = 0, []
    bal = ledger.balance(cid)
    if bal <= 0:
        return RiskReport(0, "low", [])
    od = oldest_open_days(ledger, cid, now)
    for thr, pts in ((90, 40), (60, 30), (30, 20), (14, 8)):
        if od > thr:
            score += pts
            why.append(("old_balance", f"sabse purana baaki {od} din ka"))
            break
    lp = last_payment_ts(ledger, cid)
    entries = live_money_entries(ledger, cid)
    first = min((e.ts for e in entries), default=now)
    if lp is None and (now - first) > 14 * DAY:
        score += 25
        why.append(("never_paid", "ab tak koi payment nahi"))
    elif lp is not None:
        since = (now - lp) / DAY
        for thr, pts in ((45, 20), (21, 10)):
            if since > thr:
                score += pts
                why.append(("stale_payment", f"{int(since)} din se payment nahi"))
                break
    win = now - 60 * DAY
    given = sum(e.amount_paise for e in entries if e.kind == "add_udhaar" and e.ts >= win)
    paid = sum(e.amount_paise for e in entries if e.kind == "record_payment" and e.ts >= win)
    if given > 0:
        ratio = paid / given
        if ratio < 0.2:
            score += 20
            why.append(("low_collection", "pichle 60 din mein udhaar ka 20% bhi nahi chukaya"))
        elif ratio < 0.5:
            score += 10
            why.append(("weak_collection", "pichle 60 din mein aadha bhi nahi chukaya"))
    broken = sum(1 for _, st in promise_statuses(ledger, cid, now) if st == "broken")
    if broken:
        score += min(30, 15 * broken)
        why.append(("broken_promise", f"{broken} vaada tooTa"))
    lim = ledger.customer_row(cid)["credit_limit"]
    if lim and bal > lim:
        score += 15
        why.append(("over_limit", "tay seema se zyada baaki"))
    tier = "high" if score >= 55 else "medium" if score >= 25 else "low"
    return RiskReport(score, tier, why)


def suggested_limit(ledger: Ledger, cid: int, now: float) -> int:
    """1.25 × median of the customer's recent monthly peak balances, to the nearest ₹500; floor ₹1,000."""
    entries = live_money_entries(ledger, cid)
    if not entries:
        return 100_000
    peaks: dict[tuple[int, int], int] = {}
    run = 0
    for e in entries:
        run += e.amount_paise if e.kind == "add_udhaar" else -e.amount_paise
        k = (_local(e.ts).year, _local(e.ts).month)
        peaks[k] = max(peaks.get(k, 0), run)
    recent = [v for _, v in sorted(peaks.items())[-3:] if v > 0]
    if not recent:
        return 100_000
    raw = 1.25 * statistics.median(recent)
    step = 50_000
    return max(100_000, int(round(raw / step) * step))


# ------------------------------------------------------------------ portfolio views
def outstanding(ledger: Ledger) -> list[tuple[Any, int]]:
    rows = [(c, ledger.balance(c.id)) for c in ledger.customers()]
    return sorted([r for r in rows if r[1] > 0], key=lambda r: -r[1])


def top_debtors(ledger: Ledger, n: int = 5):
    return outstanding(ledger)[:n]


def total_outstanding(ledger: Ledger) -> int:
    return sum(b for _, b in outstanding(ledger))


def weekly_flow(ledger: Ledger, now: float, weeks: int = 4) -> list[dict[str, int | str]]:
    end = _local(now).date()
    monday = end - timedelta(days=end.weekday())
    rows = []
    for w in range(weeks - 1, -1, -1):
        start = monday - timedelta(weeks=w)
        stop = start + timedelta(days=7)
        given = collected = 0
        for e in ledger.entries():
            if e.kind in MONEY_KINDS and e.seq not in ledger._reversed_seqs():
                d = _local(e.ts).date()
                if start <= d < stop:
                    if e.kind == "add_udhaar":
                        given += e.amount_paise
                    else:
                        collected += e.amount_paise
        rows.append({"week": start.isoformat(), "given": given, "collected": collected, "net": given - collected})
    return rows


def day_summary(ledger: Ledger, now: float) -> dict[str, int]:
    today = _local(now).date()
    rev = ledger._reversed_seqs()
    g = c = n = 0
    for e in ledger.entries():
        if e.kind in MONEY_KINDS and e.seq not in rev and _local(e.ts).date() == today:
            n += 1
            if e.kind == "add_udhaar":
                g += e.amount_paise
            else:
                c += e.amount_paise
    return {"given": g, "collected": c, "entries": n}


def overdue(ledger: Ledger, now: float, days: int = 30):
    out = []
    for c, bal in outstanding(ledger):
        od = oldest_open_days(ledger, c.id, now)
        if od >= days:
            out.append((c, bal, od))
    return sorted(out, key=lambda r: -r[2])


# ------------------------------------------------------------------ anomalies
@dataclass
class Anomaly:
    seq: int
    customer_id: int
    code: str
    detail: str


def anomalies(ledger: Ledger, now: float, window_days: int = 30) -> list[Anomaly]:
    rev = ledger._reversed_seqs()
    out: list[Anomaly] = []
    for c in ledger.customers():
        es = [e for e in live_money_entries(ledger, c.id) if e.kind == "add_udhaar"]
        for i, e in enumerate(es):
            if e.ts < now - window_days * DAY:
                continue
            prior = [x.amount_paise for x in es[:i]]
            if len(prior) >= 5:
                med = statistics.median(prior)
                mad = statistics.median([abs(x - med) for x in prior]) or max(med * 0.1, 100)
                if e.amount_paise > med + 6 * 1.4826 * mad and e.amount_paise > 2 * med:
                    out.append(Anomaly(e.seq, c.id, "unusual_amount", f"median {int(med)} vs {e.amount_paise}"))
            h = _local(e.ts).hour
            if h >= 23 or h < 5:
                out.append(Anomaly(e.seq, c.id, "odd_hour", f"{h}:00"))
        by_day: dict[tuple[date, int], list[int]] = {}
        for e in es:
            by_day.setdefault((_local(e.ts).date(), e.amount_paise), []).append(e.seq)
        for (d, amt), seqs in by_day.items():
            if len(seqs) >= 3 and _local(now).date() - d <= timedelta(days=window_days):
                out.append(Anomaly(seqs[-1], c.id, "repeat_same_amount", f"{len(seqs)}× {amt} on {d}"))
    return sorted(out, key=lambda a: a.seq)


# ------------------------------------------------------------------ agent priority
TIER_MULT = {"low": 0.6, "medium": 1.0, "high": 1.4}


def collection_priority(ledger: Ledger, cid: int, now: float) -> float:
    bal = ledger.balance(cid)
    if bal <= 0:
        return 0.0
    rk = risk(ledger, cid, now)
    od = oldest_open_days(ledger, cid, now)
    return round(((bal / 100) ** 0.5) * (1 + od / 30) * TIER_MULT[rk.tier], 2)
