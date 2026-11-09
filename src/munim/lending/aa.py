"""Account Aggregator (AA) reader: parse ReBIT-style deposit FI JSON and reconcile bank credits against the udhaar book.

UNVERIFIED / SCOPE: a live AA flow needs registration as a Financial Information User (FIU) or a tie-up with one, a consent
artefact, and encrypted FI delivery (the FIP -> AA -> FIU hop uses ECDH + AES). None of that is done here. This module only
reads the DECRYPTED FI JSON (as a sandbox/demo file the owner uploads) with a tolerant parser. No account number is kept: only
amount, timestamp, mode and a keyed token of the transaction id go forward.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from munim.agent import reconcile as R
from munim.ledger.chain import Ledger

DAY = 86400.0


@dataclass
class Txn:
    ts: float
    paise: int
    credit: bool
    mode: str
    token: str
    narration: str


def _paise(v) -> int | None:
    try:
        d = Decimal(str(v).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return int((d * 100).to_integral_value()) if d >= 0 else None


def parse_fi(doc: str | dict) -> list[Txn]:
    d = json.loads(doc) if isinstance(doc, str) else doc
    acct = d.get("Account") or (d.get("Accounts") or [{}])[0].get("Account") or {}
    tx = ((acct.get("Transactions") or {}).get("Transaction")) or []
    out: list[Txn] = []
    for t in tx:
        p = _paise(t.get("amount"))
        raw_ts = t.get("transactionTimestamp") or t.get("valueDate")
        try:
            ts = datetime.fromisoformat(str(raw_ts)).timestamp()
        except ValueError:
            continue
        if p is None or p == 0 or str(t.get("type", "")).upper() not in ("CREDIT", "DEBIT"):
            continue
        out.append(Txn(ts, p, str(t["type"]).upper() == "CREDIT", str(t.get("mode", "")).upper(),
                       hashlib.sha256(str(t.get("txnId", "")).encode()).hexdigest()[:20], str(t.get("narration", ""))[:80]))
    return out


def payer_from_narration(n: str) -> str | None:
    parts = [p for p in n.replace("-", "/").split("/") if p]
    if len(parts) >= 4 and parts[0].upper() == "UPI":
        return parts[3].title()                              # UPI/CR/<ref>/<name>/<bank>
    return None


def reconcile(ledger: Ledger, txns: list[Txn], now: float, window_days: int = 2) -> dict:
    """Bank credits vs ledger payments. Matched credits are left alone; unmatched ones go to the approval queue."""
    rev = ledger._reversed_seqs()
    pays = [e for e in ledger.entries() if e.kind == "record_payment" and e.seq not in rev]
    used: set[int] = set()
    matched = queued = unmatched = 0
    for t in sorted((x for x in txns if x.credit), key=lambda x: x.ts):
        hit = next((p for p in pays if p.seq not in used and p.amount_paise == t.paise and abs(p.ts - t.ts) <= window_days * DAY), None)
        if hit:
            used.add(hit.seq)
            matched += 1
            continue
        r = R.queue(ledger, t.paise, payer_from_narration(t.narration), t.token, now, src="aa")
        if r["status"] == "queued":
            queued += 1
        else:
            unmatched += 1
    cash_only = [p for p in pays if p.seq not in used and p.meta.get("src") in ("upi", "reconcile")]
    return {"credits": sum(1 for x in txns if x.credit), "matched": matched, "queued": queued, "unmatched": unmatched,
            "ledger_digital_without_bank_credit": len(cash_only)}


def monthly_summary(txns: list[Txn]) -> dict:
    cr = [t for t in txns if t.credit]
    return {"inflow_paise": sum(t.paise for t in cr), "upi_share_pct": int(100 * sum(t.paise for t in cr if t.mode == "UPI") / max(1, sum(t.paise for t in cr))),
            "n_credits": len(cr)}
