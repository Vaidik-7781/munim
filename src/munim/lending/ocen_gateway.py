"""Loan-Service-Provider (LSP) side of OCEN-style working-capital offers. Munim is not a lender and decides nothing.

UNVERIFIED: real OCEN needs LSP onboarding, signed (JWS) API calls, AA consent for lender-side data and a lender integration;
`Lender` is an adapter and `SandboxLender` a deterministic stand-in. What this module does that matters:

  * CASH-CRUNCH DETECTOR (deterministic): expected collections over the next 14 days (steady weekly median with a 20 % haircut
    plus promises falling due) vs supplier dues in the same window. It SUGGESTS to the owner; it never contacts a lender.
  * CONSENT PER REQUEST: sharing the (signed, aggregate-only) passport requires an explicit owner confirmation each time and
    leaves a `loan_consent` entry in the hash chain (recipient, fields, hash).
  * KEY FACTS: every offer is shown with EMI, total repayable and the all-in APR (fees included, computed by us, not taken from the
    lender), plus an affordability check (EMI vs the shop's own collections). Offers failing affordability are flagged, not hidden.
  * Munim never disburses, collects, or touches the money; the regulated lender does. Accepting only forwards the owner's intent.
"""
from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from munim.insights import engine as E
from munim.passport import cashflow as PF

DAY = 86400.0


def emi(principal: int, apr_bps: int, months: int) -> int:
    r = Decimal(apr_bps) / Decimal(10_000) / 12
    if r == 0:
        return -(-principal // months)
    f = (1 + r) ** months
    return int((Decimal(principal) * r * f / (f - 1)).to_integral_value())


def all_in_apr_bps(principal: int, fee: int, emi_paise: int, months: int) -> int:
    """Annualised IRR (monthly compounding x12) of: +net disbursed at t0, -EMI each month. Bisection; fees included."""
    net = principal - fee
    lo, hi = Decimal(0), Decimal(5)
    for _ in range(80):
        m = (lo + hi) / 2
        pv = sum(Decimal(emi_paise) / (1 + m) ** k for k in range(1, months + 1))
        lo, hi = (m, hi) if pv > net else (lo, m)
    return int(((lo + hi) / 2 * 12 * 10_000).to_integral_value())


@dataclass
class Offer:
    lender: str
    offer_id: str
    principal_paise: int
    months: int
    apr_bps: int
    fee_paise: int
    emi_paise: int
    total_paise: int
    all_in_apr_bps: int
    affordable: bool = True
    note: str = ""


class Lender(Protocol):
    name: str
    def offers(self, doc: dict, amount_paise: int, months: int) -> list[Offer]: ...
    def apply(self, offer_id: str, doc_sha: str) -> str: ...


class SandboxLender:
    name = "Sandbox NBFC"
    RATE = {"A": 1800, "B": 2200, "C": 2800}
    FEE_BPS = 150

    def offers(self, doc: dict, amount_paise: int, months: int) -> list[Offer]:
        d = doc["data"]
        if d.get("tier") not in self.RATE or d.get("confidence") == "low":
            return []
        monthly = d["weekly_collection_median_paise"] * 433 // 100
        limit = int(monthly * 1.5)
        p = min(amount_paise, limit)
        if p < 1_000_000:
            return []
        apr = self.RATE[d["tier"]]
        fee = p * self.FEE_BPS // 10_000
        e = emi(p, apr, months)
        return [Offer(self.name, f"sbx-{d['tier']}-{p}-{months}", p, months, apr, fee, e, e * months, all_in_apr_bps(p, fee, e, months))]

    def apply(self, offer_id: str, doc_sha: str) -> str:
        return "sbx-app-" + hashlib.sha256((offer_id + doc_sha).encode()).hexdigest()[:8]


@dataclass
class Crunch:
    expected_in_paise: int
    due_out_paise: int
    shortfall_paise: int
    horizon_days: int


def crunch(tenant, now: float, horizon_days: int = 14) -> Crunch:
    L = tenant.ledger
    p = PF.build(L, now)
    weekly = p.weekly_collection_median_paise * 80 // 100 * horizon_days // 7
    promised = 0
    for c in L.customers():
        for e, st in E.promise_statuses(L, c.id, now):
            if st == "pending" and (e.meta.get("promised_paise") or 0) > 0 and 0 <= (E._due_end_ts(e.meta["due"]) - now) <= horizon_days * DAY:
                promised += e.meta["promised_paise"]
    out = tenant.aux.db.execute("SELECT COALESCE(SUM(amount_paise),0) s FROM payable_due WHERE status='open' AND due_iso<=?",
                                (date.fromtimestamp(now + horizon_days * DAY).isoformat(),)).fetchone()["s"]
    exp = weekly + promised
    return Crunch(exp, out, max(0, out - exp), horizon_days)


class Gateway:
    def __init__(self, tenant, lender: Lender, secret: bytes):
        self.t, self.lender, self.secret = tenant, lender, secret

    def suggest(self, now: float) -> str | None:
        from munim.scribe.indic_numbers import format_inr
        c = crunch(self.t, now)
        if c.shortfall_paise <= 0:
            return None
        return (f"⚠️ Agle {c.horizon_days} din mein supplier ko {format_inr(c.due_out_paise)} dena hai, jabki vasooli ka andaza {format_inr(c.expected_in_paise)} hai. "
                f"{format_inr(c.shortfall_paise)} ki kami ho sakti hai. Chhota loan dekhna ho to /loan — pehle sirf offer dikhte hain, kuch bheja nahi jata.")

    def request_offers(self, now: float, amount_paise: int, months: int, *, owner_consented: bool) -> tuple[list[Offer], str]:
        if not owner_consented:
            raise PermissionError("owner consent required for sharing the passport")
        p = PF.build(self.t.ledger, now)
        if p.tier == "NA":
            return [], "passport too thin for a lender to read"
        doc = PF.share(self.t.ledger, p, self.lender.name, self.secret)
        monthly = p.weekly_collection_median_paise * 433 // 100
        self.t.aux.put("loan_doc_sha", hashlib.sha256(doc["sig"].encode()).hexdigest()[:16])
        offers = self.lender.offers(doc, amount_paise, months)
        for o in offers:
            o.affordable = monthly > 0 and o.emi_paise * 100 <= monthly * 30          # EMI <= 30 % of monthly collections
            o.note = "" if o.affordable else "EMI aapki vasooli ke 30% se zyada hai — dhyaan se sochein"
        return offers, "ok" if offers else "no offer"

    def accept(self, offer: Offer, *, owner_confirmed: bool) -> str:
        if not owner_confirmed:
            raise PermissionError("owner must confirm")
        return self.lender.apply(offer.offer_id, self.t.aux.get("loan_doc_sha", ""))


def offer_text(o: Offer) -> str:
    from munim.scribe.indic_numbers import format_inr
    return (f"{o.lender}: {format_inr(o.principal_paise)} • {o.months} mahine • EMI {format_inr(o.emi_paise)} • kul wapsi {format_inr(o.total_paise)} • "
            f"byaaj {o.apr_bps / 100:.1f}% • sab milake saalana (fee ke saath) {o.all_in_apr_bps / 100:.1f}% • fee {format_inr(o.fee_paise)}"
            + (f"\n⚠️ {o.note}" if o.note else ""))
