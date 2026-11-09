import json
from datetime import datetime, timedelta

import httpx
import pytest

from munim.agent.planner import Done, LLMPlanner, RulePlanner, Step
from munim.agent.runner import run_agent
from munim.agent.tools import MAX_QUEUED_PER_RUN, TOOLS
from munim.config import Settings
from munim.insights import engine as E
from munim.ledger.chain import MONEY_KINDS, Ledger
from munim.llm.client import LLMClient
from munim.scribe.extract import extract
from munim.scribe.llm_spans import LLMSpans
from munim.scribe.names import Customer
from munim.service import MunimService
from munim.timeutil import IST

CFG = Settings()
DAY = 86400


class Clk:
    def __init__(self):
        self.t = datetime(2026, 9, 1, 11, 0, tzinfo=IST).timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def world(tmp_path):
    clk = Clk()
    led = Ledger(str(tmp_path / "a.db"), "k", clock=clk)
    ids = {}
    for name, amt in [("Ramesh Sharma", 400000), ("Sunita Verma", 150000), ("Mohan Das", 90000), ("Kavita Singh", 30000),
                      ("Pooja Mishra", 20000), ("Anil Gupta", 10000), ("Rajesh Yadav", 50000)]:
        c = led.add_customer(name)
        led.add_udhaar(c.id, amt)
        led.grant_consent(c.id)
        ids[name] = c.id
    clk.t += 45 * DAY
    # one customer without consent, one broken promise
    led.set_flag(ids["Mohan Das"], consent="none")
    due = (datetime.fromtimestamp(clk.t, IST) - timedelta(days=10)).date().isoformat()
    led.add_promise(ids["Sunita Verma"], due, 150000)
    return led, clk, ids


def money_snapshot(led):
    return [(e.seq, e.kind, e.amount_paise) for e in led.entries() if e.kind in MONEY_KINDS]


async def test_rule_agent_queues_drafts_respects_governor_and_changes_no_money(world):
    led, clk, ids = world
    before = money_snapshot(led)
    run = await run_agent(led, CFG, "Gupta Kirana", clk.t)
    assert run.stopped == "done" and run.planner == "rules"
    q = led.pending_approvals()
    names = {led.customer_row(i["customer_id"])["name"] for i in q}
    assert "Mohan Das" not in names                              # no consent => never drafted
    assert "Ramesh Sharma" in names and len(q) <= MAX_QUEUED_PER_RUN
    kinds = {led.customer_row(i["customer_id"])["name"]: i["kind"] for i in q}
    assert kinds["Sunita Verma"] == "promise_followup"            # broken promise gets the tailored follow-up
    assert money_snapshot(led) == before                          # the agent never writes money
    assert led.verify()[0]
    assert any(e.kind == "agent_action" for e in led.entries())   # every proposal is on the chain
    assert all("AI" in i["payload"]["text"] for i in q)           # disclosure on every draft


def test_no_tool_can_write_money_or_contact_customers():
    assert {t.tier for t in TOOLS.values()} == {"read", "propose"}
    forbidden = ("add_udhaar", "record_payment", "send", "consent", "erase", "undo")
    assert not [n for n in TOOLS if any(f in n for f in forbidden)]


async def test_agent_is_idempotent_no_duplicate_queue(world):
    led, clk, ids = world
    await run_agent(led, CFG, "S", clk.t)
    n1 = len(led.pending_approvals())
    await run_agent(led, CFG, "S", clk.t)
    assert len(led.pending_approvals()) == n1


async def test_governor_enforced_inside_tool_even_if_planner_is_malicious(world):
    led, clk, ids = world

    class Evil:
        name = "evil"

        def __init__(self):
            self.calls = [Step("queue_reminder", {"customer_id": ids["Mohan Das"]}), Done()]

        async def next(self, h):
            return self.calls.pop(0)

    run = await run_agent(led, CFG, "S", clk.t, Evil())
    assert run.trace[0][2] == {"queued": False, "reason": ["no_consent"]}
    assert led.pending_approvals() == []


async def test_unknown_tool_stops_run(world):
    led, clk, ids = world

    class Bad:
        name = "bad"

        async def next(self, h):
            return Step("add_udhaar", {"customer_id": 1, "paise": 10 ** 9})

    run = await run_agent(led, CFG, "S", clk.t, Bad())
    assert run.stopped == "error" and money_snapshot(led) == money_snapshot(led)
    assert not [e for e in led.entries() if e.kind == "add_udhaar" and e.amount_paise == 10 ** 9]


async def test_max_steps_guard(world):
    led, clk, ids = world

    class Loop:
        name = "loop"

        async def next(self, h):
            return Step("get_risk", {"customer_id": ids["Ramesh Sharma"]})

    run = await run_agent(led, CFG, "S", clk.t, Loop(), max_steps=5)
    assert run.stopped == "max_steps" and len(run.trace) == 5


# ---------------- LLM planner
def llm_with(replies):
    it = iter(replies)
    seen = []

    def handler(req: httpx.Request):
        seen.append(json.loads(req.content))
        r = next(it)
        if isinstance(r, int):
            return httpx.Response(r)
        return httpx.Response(200, json={"choices": [{"message": {"content": r if isinstance(r, str) else json.dumps(r)}}]})

    return LLMClient("key", transport=httpx.MockTransport(handler)), seen


async def test_llm_planner_follows_valid_plan_and_sees_no_names(world):
    led, clk, ids = world
    rid = ids["Ramesh Sharma"]
    llm, seen = llm_with([
        {"action": "call", "tool": "rank_collections", "args": {"limit": 3}, "why": "start"},
        {"action": "call", "tool": "queue_reminder", "args": {"customer_id": rid}, "why": "biggest"},
        {"action": "done", "summary": "ek reminder"},
    ])
    run = await run_agent(led, CFG, "S", clk.t, LLMPlanner(llm))
    assert run.planner == "llm" and run.fallback_reason is None and len(led.pending_approvals()) == 1
    blob = json.dumps(seen)
    assert "Ramesh" not in blob and "Sharma" not in blob and "Verma" not in blob        # privacy: ids only
    assert seen[0]["temperature"] == 0 and seen[0]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("bad", [
    500, "not json", {"action": "call", "tool": "delete_everything", "args": {}},
    {"action": "call", "tool": "queue_reminder", "args": {"customer_id": 99999}},
])
async def test_llm_failures_fall_back_to_rules_and_still_complete(world, bad):
    led, clk, ids = world
    llm, _ = llm_with([bad] + [500] * 30)
    p = LLMPlanner(llm)
    run = await run_agent(led, CFG, "S", clk.t, p)
    assert run.planner == "llm→rules" and run.fallback_reason
    assert len(led.pending_approvals()) >= 3 and run.stopped == "done"


async def test_llm_cannot_invent_customer_id_not_from_ranking(world):
    led, clk, ids = world
    llm, _ = llm_with([{"action": "call", "tool": "queue_reminder", "args": {"customer_id": ids["Ramesh Sharma"]}}] + [500] * 30)
    run = await run_agent(led, CFG, "S", clk.t, LLMPlanner(llm))
    assert "customer_id not from rank_collections" in run.fallback_reason     # guessed before ranking


# ---------------- approvals through the service
async def test_plan_then_approve_flow_rechecks_governor(world):
    led, clk, ids = world
    svc = MunimService(led, CFG, "Gupta Kirana", clock=clk)
    r = await svc.plan(1)
    assert "ijaazat" in r.text and any(b[1] == "appr:all" for b in r.buttons)
    first = next(b for b in r.buttons if b[1].startswith("appr:") and b[1].endswith(":y"))
    # rules change between draft and approval: customer opts out
    aid = int(first[1].split(":")[1])
    cust = next(i["customer_id"] for i in led.pending_approvals() if i["id"] == aid)
    led.set_flag(cust, opted_out=1)
    out = svc.on_callback(1, first[1])
    assert "nahi bheja" in out.text
    assert led.reminder_times(cust) == []
    out = svc.on_callback(1, "appr:all")
    assert "✅" in out.text
    others = [i for i in ids.values() if i != cust]
    assert any(led.reminder_times(i) for i in others) and led.reminder_times(cust) == []   # opted-out stays unsent
    assert led.pending_approvals() == [] and led.verify()[0]


# ---------------- LLM third-opinion on speech
async def test_llm_spans_can_only_add_doubt(tmp_path):
    custs = [Customer(1, "Ramesh Sharma"), Customer(2, "Sunita Verma")]
    llm, _ = llm_with([
        {"intent": "add_udhaar", "customer_text": "Sharma ji", "amount_text": "dedh sau"},
        {"intent": "add_udhaar", "customer_text": "Sharma ji", "amount_text": "dedh hazaar"},
    ])
    sp = LLMSpans(llm)
    clk = Clk()
    led = Ledger(str(tmp_path / "l.db"), "k", clock=clk)
    led.add_customer("Ramesh Sharma"), led.add_customer("Sunita Verma")
    svc = MunimService(led, CFG, "S", clock=clk)
    text = "Sharma ji ko dedh sau udhaar likh do"
    agree = await sp.propose(text, custs)
    assert agree.amount_paise == 15000                               # recomputed by OUR parser, not trusted from LLM
    r = svc.on_heard(1, text, text, snr=30, dur=3, llm=agree)
    assert r.wrote                                                   # agreement adds no extra trust, no extra friction
    clk.t += 500
    disagree = await sp.propose(text, custs)
    r = svc.on_heard(1, text, text, snr=30, dur=3, llm=disagree)
    assert not r.wrote and "AI ki doosri raay" in r.text


async def test_llm_spans_failure_is_harmless():
    llm, _ = llm_with([500])
    assert await LLMSpans(llm).propose("x", []) is None
