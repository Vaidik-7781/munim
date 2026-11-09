"""Agent tools. TIERS ARE THE SAFETY MODEL:

  read    — compute facts from the ledger. Free.
  propose — put a DRAFT into the approval queue. The shopkeeper decides. Governor rules are enforced
            INSIDE the tool, so neither a rule-planner nor a hallucinating LLM planner can bypass them.

There is deliberately NO tool that writes money, changes consent, or contacts a customer.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from munim.config import Settings
from munim.govern.governor import check, draft_reminder
from munim.insights import engine as E
from munim.ledger.chain import Ledger
from munim.govern.tone_lint import lint
from munim.scribe.indic_numbers import format_inr

MAX_QUEUED_PER_RUN = 5


@dataclass
class ToolCtx:
    ledger: Ledger
    cfg: Settings
    shop: str
    now: float
    queued: int = 0


@dataclass
class Tool:
    name: str
    tier: str                         # read | propose
    doc: str
    fn: Callable[[ToolCtx, dict[str, Any]], dict[str, Any]]


def _cid(args: dict[str, Any]) -> int:
    try:
        return int(args["customer_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("customer_id (int) required") from exc


def rank_collections(ctx: ToolCtx, args: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(args.get("limit", 5)), 10))
    rows = []
    for c, bal in E.outstanding(ctx.ledger):
        rk = E.risk(ctx.ledger, c.id, ctx.now)
        rows.append({"customer_id": c.id, "balance_paise": bal, "tier": rk.tier, "score": rk.score,
                     "reasons": rk.codes, "priority": E.collection_priority(ctx.ledger, c.id, ctx.now)})
    rows.sort(key=lambda r: -r["priority"])
    return {"ranked": rows[:limit]}


def get_risk(ctx: ToolCtx, args: dict[str, Any]) -> dict[str, Any]:
    cid = _cid(args)
    rk = E.risk(ctx.ledger, cid, ctx.now)
    return {"customer_id": cid, "tier": rk.tier, "score": rk.score, "reasons": rk.codes,
            "oldest_open_days": E.oldest_open_days(ctx.ledger, cid, ctx.now)}


def get_promises(ctx: ToolCtx, args: dict[str, Any]) -> dict[str, Any]:
    cid = _cid(args)
    return {"customer_id": cid, "promises": [{"due": e.meta["due"], "status": st}
                                             for e, st in E.promise_statuses(ctx.ledger, cid, ctx.now)]}


def check_governor(ctx: ToolCtx, args: dict[str, Any]) -> dict[str, Any]:
    cid = _cid(args)
    d = check(ctx.ledger, ctx.cfg, cid, ctx.now)
    return {"customer_id": cid, "allowed": d.allowed, "reasons": d.reasons}


def _budget(ctx: ToolCtx) -> dict[str, Any] | None:
    if ctx.queued >= MAX_QUEUED_PER_RUN:
        return {"queued": False, "reason": "run_budget_exhausted"}
    return None


def queue_reminder(ctx: ToolCtx, args: dict[str, Any]) -> dict[str, Any]:
    cid = _cid(args)
    if (b := _budget(ctx)) is not None:
        return b
    d, draft = draft_reminder(ctx.ledger, ctx.cfg, cid, ctx.shop, now=ctx.now)
    if draft is None:
        return {"queued": False, "reason": d.reasons}
    rk = E.risk(ctx.ledger, cid, ctx.now)
    from .timing import suggest_slot

    slot = suggest_slot(ctx.ledger, ctx.now)
    aid = ctx.ledger.queue_approval(cid, "reminder", {"text": draft.text, "voice": draft.voice_text,
                                                      "amount_paise": draft.amount_paise, "lang": draft.lang,
                                                      "tts_language": draft.tts_language, "slot": slot},
                                    reason=";".join(rk.codes), priority=E.collection_priority(ctx.ledger, cid, ctx.now))
    ctx.queued += 1
    return {"queued": True, "approval_id": aid, "kind": "reminder"}


def queue_promise_followup(ctx: ToolCtx, args: dict[str, Any]) -> dict[str, Any]:
    """Gentle nudge after a broken promise. Same Governor + linter + disclosure as any reminder."""
    cid = _cid(args)
    if (b := _budget(ctx)) is not None:
        return b
    d = check(ctx.ledger, ctx.cfg, cid, ctx.now)
    if not d.allowed:
        return {"queued": False, "reason": d.reasons}
    broken = [e for e, st in E.promise_statuses(ctx.ledger, cid, ctx.now) if st == "broken"]
    if not broken:
        return {"queued": False, "reason": ["no_broken_promise"]}
    row = ctx.ledger.customer_row(cid)
    bal = ctx.ledger.balance(cid)
    text = (f"Namaste {row['name'].split()[0]} ji. Yeh {ctx.shop} ki taraf se Munim (AI sahayak) ka sandesh hai. "
            f"Aapne {broken[-1].meta['due']} tak dene ko kaha tha; abhi {format_inr(bal)} baaki dikh raha hai. "
            f"Agar der ho rahi hai to bata dijiye, nayi tareekh likh lenge. Hisaab galat lage to \"galat\" likh dijiye.")
    if not lint(text).ok:
        return {"queued": False, "reason": ["tone_lint"]}
    aid = ctx.ledger.queue_approval(cid, "promise_followup", {"text": text, "amount_paise": bal},
                                    reason="broken_promise", priority=E.collection_priority(ctx.ledger, cid, ctx.now) + 5)
    ctx.queued += 1
    return {"queued": True, "approval_id": aid, "kind": "promise_followup"}


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("rank_collections", "read", "args: limit(1-10). Customers with balance, ranked by collection priority.", rank_collections),
    Tool("get_risk", "read", "args: customer_id. Risk tier, score, reason codes.", get_risk),
    Tool("get_promises", "read", "args: customer_id. Promise-to-pay statuses.", get_promises),
    Tool("check_governor", "read", "args: customer_id. Is a reminder allowed right now, and if not why.", check_governor),
    Tool("queue_reminder", "propose", "args: customer_id. Draft a reminder into the approval queue (Governor enforced).", queue_reminder),
    Tool("queue_promise_followup", "propose", "args: customer_id. Draft a follow-up for a broken promise.", queue_promise_followup),
]}
