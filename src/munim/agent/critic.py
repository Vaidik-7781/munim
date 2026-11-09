"""Critic: a second, independent check on every drafted action BEFORE it reaches the shopkeeper.

Planner proposes -> Critic verifies against the live ledger (not against the planner's own claims) -> shopkeeper decides.
Any issue rejects the draft and is recorded on the chain (by="critic")."""
from __future__ import annotations

from typing import Any

from munim.config import Settings
from munim.govern.governor import check
from munim.govern.tone_lint import lint
from munim.ledger.chain import Ledger
from munim.scribe.indic_numbers import format_inr


def verify_item(ledger: Ledger, cfg: Settings, item: dict[str, Any], now: float) -> list[str]:
    cid, pl = item["customer_id"], item["payload"]
    issues: list[str] = []
    d = check(ledger, cfg, cid, now)
    issues += [f"governor:{r}" for r in d.reasons if r != "outside_hours"]   # sending happens after approval
    bal = ledger.balance(cid)
    if pl.get("amount_paise") != bal:
        issues.append("stale_amount")
    text = pl.get("text", "")
    if format_inr(bal) not in text and pl.get("lang", "hi") == "hi":
        issues.append("text_amount_mismatch")
    first = ledger.customer_row(cid)["name"].split()[0]
    if first.lower() not in text.lower():
        issues.append("name_mismatch")
    issues += [f"lint:{v}" for v in lint(text).violations]
    return issues
