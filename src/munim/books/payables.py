"""Payables book: what the SHOP owes its suppliers/wholesalers — the other half of a kirana's cash picture.

Reuses the same hash-chained Ledger in a second file (customers = suppliers; add_udhaar = goods taken on credit;
record_payment = paid the supplier). `position()` nets receivables against payables so the owner sees the real cash gap.
Typed commands only (explicit amounts are unambiguous); undo is a reversal like everywhere else.
"""
from __future__ import annotations

from munim.insights import engine as E
from munim.ledger.chain import Ledger
from munim.scribe.indic_numbers import format_inr, parse_amount
from munim.scribe.names import resolve


def parse_dena(args: str) -> tuple[str, str, int] | None:
    """'Agarwal Traders 5000' -> (name, 'bought', 500000);  'Agarwal Traders diya 2000' -> (name, 'paid', 200000)."""
    low = args.lower()
    paid = any(w in low.split() for w in ("diya", "paid", "chukaya", "jama"))
    amt = parse_amount(args)
    if not amt:
        return None
    name = " ".join(w for w in args.split() if w.lower() not in ("diya", "paid", "chukaya", "jama") and not any(ch.isdigit() for ch in w)
                    and parse_amount(w) is None).strip()
    return (name, "paid" if paid else "bought", amt) if len(name) >= 2 else None


def record(pay: Ledger, name: str, kind: str, paise: int, by: str | None = None) -> str:
    sup = next((c for c in pay.customers() if c.name.lower() == name.lower()), None)
    created = False
    if sup is None:
        r = resolve(name, pay.customers())
        if r.status == "match" and r.score >= 100:
            sup = r.customer
        elif r.status == "ask":
            return "Kaun sa supplier? " + ", ".join(c.name for c, _ in r.candidates[:3])
        else:
            sup, created = pay.add_customer(name.title()), True
    extra = {"by": by} if by else {}
    if kind == "bought":
        pay.add_udhaar(sup.id, paise, src="payables", **extra)
        verb = "maal udhaar liya"
    else:
        pay.record_payment(sup.id, paise, src="payables", **extra)
        verb = "payment kiya"
    return (f"✅ {sup.name}: {verb} {format_inr(paise)}{' (naya supplier)' if created else ''}. "
            f"Ab aapko dena hai: {format_inr(max(pay.balance(sup.id), 0))}.")


def position(recv: Ledger, pay: Ledger) -> str:
    lena = E.total_outstanding(recv)
    dena = E.total_outstanding(pay)
    top = E.top_debtors(pay, 3)
    lines = [f"💰 Lena (customers se): {format_inr(lena)}", f"💸 Dena (suppliers ko): {format_inr(dena)}",
             f"{'➕' if lena >= dena else '➖'} Net: {format_inr(lena - dena)}"]
    if top:
        lines.append("Sabse zyada dena: " + ", ".join(f"{c.name} {format_inr(b)}" for c, b in top))
    if dena > lena:
        lines.append("Dhyaan: dena lena se zyada hai — vasooli tez karne ya supplier se samay maangne ka vichar karein.")
    return "\n".join(lines)
