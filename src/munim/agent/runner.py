from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from munim.config import Settings
from munim.ledger.chain import Ledger

from .critic import verify_item
from .planner import Done, Obs, Planner, RulePlanner, Step
from .tools import TOOLS, ToolCtx

MAX_STEPS = 16


@dataclass
class AgentRun:
    planner: str
    trace: list[tuple[str, dict[str, Any], dict[str, Any], str]] = field(default_factory=list)
    queued_ids: list[int] = field(default_factory=list)
    summary: str = ""
    fallback_reason: str | None = None
    stopped: str = "done"          # done | max_steps | error
    critic_rejected: list[tuple[int, list[str]]] = field(default_factory=list)


async def run_agent(ledger: Ledger, cfg: Settings, shop: str, now: float, planner: Planner | None = None,
                    max_steps: int = MAX_STEPS) -> AgentRun:
    planner = planner or RulePlanner()
    ctx = ToolCtx(ledger, cfg, shop, now)
    hist: list[Obs] = []
    run = AgentRun(planner.name)
    for _ in range(max_steps):
        step = await planner.next(hist)
        if isinstance(step, Done):
            run.summary = step.summary
            break
        tool = TOOLS.get(step.tool)
        if tool is None:                      # defence in depth: the planner may not invent tools
            run.stopped = "error"
            run.summary = f"unknown tool {step.tool}"
            break
        try:
            res = tool.fn(ctx, step.args)
        except (ValueError, KeyError) as exc:
            res = {"error": str(exc)}
        cid = step.args.get("customer_id")
        if isinstance(cid, int) and tool.tier == "propose":
            ledger.log_agent_action(cid, step.tool, queued=bool(res.get("queued")), planner=planner.name)
        if res.get("queued") and "approval_id" in res:
            run.queued_ids.append(res["approval_id"])
        hist.append(Obs(step, res))
        run.trace.append((step.tool, step.args, res, step.why))
    else:
        run.stopped = "max_steps"
    # Critic pass: independent verification of every queued draft against the live ledger.
    for it in ledger.pending_approvals():
        if it["id"] in run.queued_ids:
            issues = verify_item(ledger, cfg, it, now)
            if issues:
                ledger.decide_approval(it["id"], False, by="critic")
                run.critic_rejected.append((it["id"], issues))
    run.queued_ids = [i for i in run.queued_ids if i not in {r[0] for r in run.critic_rejected}]
    run.planner = planner.name
    run.fallback_reason = getattr(planner, "fallback_reason", None)
    return run
