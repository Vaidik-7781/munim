"""Planners decide the NEXT tool call. Two implementations, same interface:

RulePlanner — deterministic collections policy (always available, fully testable).
LLMPlanner  — an LLM proposes the next step as JSON; every step is validated against the tool whitelist, and
              ANY failure (network, bad JSON, unknown tool, loop) hands control to the RulePlanner.
The LLM sees pseudonymous customer ids and numbers only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from munim.llm.client import LLMClient, LLMError

from .tools import TOOLS


@dataclass
class Step:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    why: str = ""


@dataclass
class Done:
    summary: str = ""


@dataclass
class Obs:
    step: Step
    result: dict[str, Any]


class Planner(Protocol):
    name: str

    async def next(self, history: list[Obs]) -> Step | Done: ...


class RulePlanner:
    name = "rules"

    def __init__(self, max_customers: int = 5):
        self.max_customers = max_customers

    async def next(self, history: list[Obs]) -> Step | Done:
        if not history:
            return Step("rank_collections", {"limit": self.max_customers}, "sabse zaroori customers dhoondho")
        ranked = next((o.result["ranked"] for o in history if o.step.tool == "rank_collections"), [])
        done_ids = {o.step.args.get("customer_id") for o in history if o.step.tool.startswith("queue_")}
        for row in ranked:
            cid = row["customer_id"]
            if cid in done_ids:
                continue
            prom = next((o for o in history if o.step.tool == "get_promises" and o.step.args.get("customer_id") == cid), None)
            if prom is None:
                return Step("get_promises", {"customer_id": cid}, "vaade ki sthiti dekho")
            broken = any(p["status"] == "broken" for p in prom.result["promises"])
            return Step("queue_promise_followup" if broken else "queue_reminder", {"customer_id": cid},
                        "tooTe vaade par naram yaad-dihaani" if broken else "reminder ka draft banao")
        return Done("plan taiyaar")


class _Action(BaseModel):
    action: str                    # "call" | "done"
    tool: str | None = None
    args: dict[str, Any] = {}
    why: str = ""
    summary: str = ""


SYSTEM = (
    "You are the planning step of a shopkeeper's credit-collections assistant. You may only call the listed tools. "
    "Goal: queue at most 5 polite reminders for the customers who most need follow-up. Never invent customer ids; "
    "use ids returned by rank_collections. Prefer queue_promise_followup when get_promises shows a broken promise. "
    "Reply with ONE JSON object: {\"action\":\"call\",\"tool\":NAME,\"args\":{...},\"why\":SHORT} or "
    "{\"action\":\"done\",\"summary\":SHORT}.\nTools:\n" + "\n".join(f"- {t.name} [{t.tier}]: {t.doc}" for t in TOOLS.values())
)


class LLMPlanner:
    def __init__(self, llm: LLMClient, fallback: RulePlanner | None = None):
        self.llm = llm
        self.fallback = fallback or RulePlanner()
        self.name = "llm"
        self.fallback_reason: str | None = None

    async def next(self, history: list[Obs]) -> Step | Done:
        if self.fallback_reason is not None:
            return await self.fallback.next(history)
        known_ids = {r["customer_id"] for o in history if o.step.tool == "rank_collections" for r in o.result.get("ranked", [])}
        try:
            user = json.dumps([{"tool": o.step.tool, "args": o.step.args, "result": o.result} for o in history], ensure_ascii=False)
            raw = await self.llm.chat_json(SYSTEM, user or "[]")
            act = _Action.model_validate(raw)
            if act.action == "done":
                return Done(act.summary[:200])
            if act.action != "call" or act.tool not in TOOLS:
                raise ValueError(f"bad tool {act.tool!r}")
            if "customer_id" in act.args and act.tool != "rank_collections":
                if act.args["customer_id"] not in known_ids:
                    raise ValueError("customer_id not from rank_collections")
            if any(o.step.tool == act.tool and o.step.args == act.args for o in history):
                raise ValueError("repeated call")
            return Step(act.tool, act.args, act.why[:120])
        except (LLMError, ValidationError, ValueError, TypeError) as exc:
            self.fallback_reason = f"{type(exc).__name__}: {exc}"[:160]
            self.name = "llm→rules"
            return await self.fallback.next(history)
