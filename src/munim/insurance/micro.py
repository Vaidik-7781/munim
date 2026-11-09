"""Micro-insurance: passport-based INDICATION, per-transaction premium FUNDING, claim intake drafts. Munim is not an insurer.

Honest scope (read first):
  * Underwriting is the insurer's. We produce an indication from the Cash-Flow Passport tier and a deterministic, ILLUSTRATIVE
    rate card (not any insurer's real tariff). The LLM never sets price or coverage; it may only narrate facts we computed
    (`narrate` rejects any number that is not in the fact list and falls back to a template).
  * Selling insurance in India needs a licensed insurer / corporate agent / web aggregator (IRDAI). `Insurer` is an adapter;
    `SandboxInsurer` simulates one. No Digit/Acko (or any) integration exists or is claimed.
  * "Rs 0.50 per digital transaction" is a premium FUNDING mechanism: it accrues in a pot the owner remits to the insurer.
    Cover is ACTIVE only when the insurer confirms the bind after the premium it requires is paid. Accrual alone is not cover;
    `funding_plan` tells the owner when 50 paise per transaction cannot reach the annual premium at their volume.
  * Customers are never charged: the accrual is a cost-allocation inside the shop's own books (adding a hidden fee to a
    customer's payment would be deceptive pricing).
Munim holds no money: remittance is a UPI payment from the owner to the insurer's VPA.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

DAY = 86400.0
RATE_BPS = {"A": 40, "B": 50, "C": 65, "D": 85}              # basis points of sum insured per year — illustrative only
PRODUCTS = {"STOCK_FIRE": "Dukaan ka maal: aag / chori / kharab hona", "PERSONAL_ACCIDENT": "Maalik ka accident cover"}
MIN_PREMIUM = 30_000                                          # Rs 300
QUALIFYING_SRC = {"upi", "reconcile", "mandate"}              # digital settlements only
INCIDENTS = {"fire": r"\b(aag|fire|jal)\b", "theft": r"\b(chori|theft|stolen)\b", "spoilage": r"\b(kharab|spoil|sad\s*gaya|expire|seelan|baarish|paani)\b",
             "accident": r"\b(accident|chot|hospital)\b"}


@dataclass
class Quote:
    product: str
    tier: str
    sum_insured_paise: int
    annual_premium_paise: int
    insurer_ref: str
    illustrative: bool = True


class Insurer(Protocol):
    def quote(self, product: str, tier: str, sum_insured_paise: int) -> Quote: ...
    def bind(self, q: Quote, paid_paise: int) -> bool: ...
    def submit_claim(self, policy_ref: str, summary: str, est_loss_paise: int) -> str: ...


class SandboxInsurer:
    VPA = "sandbox-insurer@upi"

    def quote(self, product: str, tier: str, sum_insured_paise: int) -> Quote:
        return indicate(product, tier, sum_insured_paise)

    def bind(self, q: Quote, paid_paise: int) -> bool:
        return paid_paise >= q.annual_premium_paise

    def submit_claim(self, policy_ref: str, summary: str, est_loss_paise: int) -> str:
        return "sbx-claim-" + hashlib.sha256(f"{policy_ref}{summary}".encode()).hexdigest()[:8]


def indicate(product: str, tier: str, sum_insured_paise: int) -> Quote:
    if product not in PRODUCTS:
        raise ValueError("unknown product")
    if tier not in RATE_BPS:
        raise ValueError("no tier: the passport needs more history before an indication can be made")
    prem = max(MIN_PREMIUM, sum_insured_paise * RATE_BPS[tier] // 10_000)
    return Quote(product, tier, sum_insured_paise, prem, "ind-" + hashlib.sha256(f"{product}{tier}{sum_insured_paise}".encode()).hexdigest()[:8])


def suggest_sum_insured(stock_value_paise: int) -> int:
    step = 1_000_000                                          # Rs 10,000 steps, 10 % headroom, min Rs 50,000, max Rs 5 lakh
    v = stock_value_paise * 110 // 100
    return max(5_000_000, min(50_000_000, (v + step - 1) // step * step))


def stock_value(inv) -> int:
    return sum(max(0, s.stock) * s.price_paise // max(1, s.price_per) for s in inv.skus() if s.price_paise)


def funding_plan(annual_premium_paise: int, events_per_month: int, per_event_paise: int = 50) -> dict:
    yearly = events_per_month * 12 * per_event_paise
    need = -(-annual_premium_paise // max(1, events_per_month * 12))
    return {"accrual_per_year_paise": yearly, "shortfall_paise": max(0, annual_premium_paise - yearly),
            "per_event_needed_paise": need, "covers_premium": yearly >= annual_premium_paise}


class Book:
    def __init__(self, tenant, insurer: Insurer):
        self.t, self.insurer, self.db, self.L = tenant, insurer, tenant.aux.db, tenant.ledger

    def per_event(self) -> int:
        return int(self.t.aux.get("ins_per_event", 50))

    def set_per_event(self, paise: int) -> None:
        if not (25 <= paise <= 200):
            raise ValueError("per-transaction amount must be 25–200 paise")
        self.t.aux.put("ins_per_event", paise)

    def accept_quote(self, q: Quote) -> int:
        cur = self.db.execute(
            "INSERT INTO policies(product,tier,sum_insured_paise,annual_premium_paise,status,insurer_ref,created) VALUES (?,?,?,?,?,?,?)",
            (q.product, q.tier, q.sum_insured_paise, q.annual_premium_paise, "accepted_unpaid", q.insurer_ref, self.t._clock()))
        return cur.lastrowid

    def policy(self, pid: int):
        return self.db.execute("SELECT * FROM policies WHERE id=?", (pid,)).fetchone()

    def accrue(self, pid: int) -> int:
        """Scan new qualifying digital settlements; accrue per_event each, idempotent per ledger entry. Never beyond the premium."""
        p = self.policy(pid)
        if p is None or p["status"] in ("cancelled",):
            return 0
        rev = self.L._reversed_seqs()
        room = p["annual_premium_paise"] - p["accrued_paise"] - p["paid_paise"]
        added = 0
        for e in self.L.entries():
            if e.kind != "record_payment" or e.seq in rev or e.meta.get("src") not in QUALIFYING_SRC or e.ts < (p["created"] or 0):
                continue
            h = hashlib.sha256(f"{pid}:{e.seq}".encode()).hexdigest()[:20]
            amt = min(self.per_event(), room - added)
            if amt <= 0:
                break
            try:
                self.db.execute("INSERT INTO accruals(ts,policy_id,ref_hash,paise) VALUES (?,?,?,?)", (self.t._clock(), pid, h, amt))
            except Exception:
                continue                                     # already accrued (UNIQUE)
            added += amt
        if added:
            self.db.execute("UPDATE policies SET accrued_paise=accrued_paise+? WHERE id=?", (added, pid))
        return added

    def remit(self, pid: int, paid_paise: int) -> str:
        """Owner paid the insurer (outside Munim). Move accrual -> paid; ask the insurer to bind. Status comes from the insurer."""
        p = self.policy(pid)
        if p is None or paid_paise <= 0:
            raise ValueError("nothing to remit")
        self.db.execute("UPDATE policies SET paid_paise=paid_paise+?, accrued_paise=MAX(0, accrued_paise-?) WHERE id=?", (paid_paise, paid_paise, pid))
        p = self.policy(pid)
        q = Quote(p["product"], p["tier"], p["sum_insured_paise"], p["annual_premium_paise"], p["insurer_ref"])
        if self.insurer.bind(q, p["paid_paise"]):
            self.db.execute("UPDATE policies SET status='active', start_ts=? WHERE id=?", (self.t._clock(), pid))
            return "active"
        return "accepted_unpaid"

    def status_text(self, pid: int) -> str:
        from munim.scribe.indic_numbers import format_inr
        p = self.policy(pid)
        left = max(0, p["annual_premium_paise"] - p["paid_paise"])
        act = p["status"] == "active"
        return (f"{PRODUCTS[p['product']]}: {'✅ cover CHALU' if act else '⏳ cover abhi shuru NAHI hua'} • bima raashi {format_inr(p['sum_insured_paise'])} • "
                f"saalana premium {format_inr(p['annual_premium_paise'])} • jama pot {format_inr(p['accrued_paise'])} • insurer ko diya {format_inr(p['paid_paise'])}"
                + ("" if act else f" • insurer ko abhi dena baaki {format_inr(left)}. Pot sirf bachat hai; cover tab shuru hoga jab insurer confirm kare."))

    # ---- claims ---------------------------------------------------------------
    @staticmethod
    def classify_incident(text: str) -> str | None:
        for k, pat in INCIDENTS.items():
            if re.search(pat, text.lower()):
                return k
        return None

    def claim_draft(self, pid: int, text: str, est_loss_paise: int) -> int:
        kind = self.classify_incident(text)
        if kind is None or est_loss_paise <= 0:
            raise ValueError("incident type or loss amount unclear")
        cur = self.db.execute("INSERT INTO claims(ts,policy_id,summary,est_loss_paise) VALUES (?,?,?,?)",
                              (self.t._clock(), pid, f"{kind}: {text[:160]}", est_loss_paise))
        return cur.lastrowid

    def claim_submit(self, claim_id: int) -> str:
        c = self.db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        p = self.policy(c["policy_id"])
        if p["status"] != "active":
            raise ValueError("policy is not active: claim cannot be filed")
        ref = self.insurer.submit_claim(p["insurer_ref"], c["summary"], c["est_loss_paise"])
        self.db.execute("UPDATE claims SET status='submitted', insurer_ref=? WHERE id=?", (ref, claim_id))
        return ref


def narrate(llm_json: dict | None, facts: dict[str, str], template: str) -> str:
    """LLM text is accepted only if every number in it appears in `facts`, and it promises nothing ('guarantee', 'pakka cover')."""
    if not llm_json or not isinstance(llm_json.get("summary"), str):
        return template
    s = llm_json["summary"].strip()
    allowed = set(re.findall(r"\d[\d,]*", " ".join(facts.values())))
    if (len(s) > 400 or any(n not in allowed for n in re.findall(r"\d[\d,]*", s))
            or re.search(r"guarantee|pakka\s*cover|zaroor\s*milega|100\s*%", s, re.I)):
        return template
    return s
