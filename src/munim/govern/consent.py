"""Consent. Reminders go ONLY to customers who agreed, and can stop at any time.

Hackathon scope: the shopkeeper attests that the customer agreed in person (logged in the chain
with how='shopkeeper_attested'). A customer-side opt-in link is a roadmap item.
"""
from __future__ import annotations

from munim.ledger.chain import Ledger

CONSENT_NOTE_HI = (
    "Kya {name} ji ne reminder lene ke liye haan kaha hai? "
    "Sirf unki sehmati ke baad hi Munim reminder bhej sakta hai. Woh kabhi bhi 'ruko' keh sakte hain."
)


def attest_consent(ledger: Ledger, customer_id: int) -> None:
    ledger.grant_consent(customer_id, how="shopkeeper_attested")


def has_consent(ledger: Ledger, customer_id: int) -> bool:
    row = ledger.customer_row(customer_id)
    return row["consent"] == "granted" and not row["opted_out"] and not row["erased"]
