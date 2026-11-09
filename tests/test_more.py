import random
from datetime import datetime, timedelta

import pytest

from munim.agent.critic import verify_item
from munim.agent.runner import run_agent
from munim.agent.timing import SLOTS, slot_of, slot_stats, suggest_slot, thompson_pick
from munim.config import Settings
from munim.govern.governor import draft_reminder
from munim.insights.forecast import customer_forecast, forecast
from munim.ledger.chain import Ledger
from munim.scribe.indic_numbers import paise_to_words_en, rupees_to_words_en
from munim.service import MunimService
from munim.timeutil import IST

CFG = Settings()
DAY = 86400


class Clk:
    def __init__(self, dt=None):
        self.t = (dt or datetime(2026, 11, 2, 11, 0, tzinfo=IST)).timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clk = Clk()
    led = Ledger(str(tmp_path / "m.db"), "k", clock=clk)
    a, b = led.add_customer("Ramesh Sharma"), led.add_customer("Sunita Verma")
    return MunimService(led, CFG, "Gupta Kirana", clock=clk), led, clk, a, b


# ---------------- conversation memory
def test_pronoun_resolves_to_last_customer_but_asks(env):
    svc, led, clk, a, b = env
    svc.on_typed(1, "Sharma ji ko 100 udhaar")
    clk.t += 60
    r = svc.on_typed(1, "unhi ko 50 aur jod do")
    assert not r.wrote and "pichli baat" in r.text and "Ramesh Sharma" in r.text
    r2 = svc.on_typed(1, "haan")
    assert r2.wrote and led.balance(a.id) == 15000


def test_pronoun_expires_after_10_min_and_needs_memory(env):
    svc, led, clk, a, b = env
    r = svc.on_typed(1, "unhi ko 50 jod do")
    assert not r.wrote and "/add" in r.text                      # no memory => treated as unknown customer
    svc.on_typed(1, "Sharma ji ko 100 udhaar")
    clk.t += 700
    r = svc.on_typed(1, "unhi ko 50 jod do")
    assert not r.wrote and "/add" in r.text


def test_memory_is_per_chat(env):
    svc, led, clk, a, b = env
    svc.on_typed(1, "Sharma ji ko 100 udhaar")
    r = svc.on_typed(2, "unhi ko 50 jod do")
    assert "pichli baat" not in r.text


# ---------------- inbound customer voice triage
def test_customer_optout_and_dispute_apply_immediately(env):
    svc, led, clk, a, b = env
    led.add_udhaar(a.id, 50000)
    led.grant_consent(a.id)
    r = svc.on_customer_message(1, a.id, "mujhe reminder mat bhejo")
    assert "turant" in r.text and led.customer_row(a.id)["opted_out"] == 1
    r = svc.on_customer_message(1, b.id, "yeh hisaab galat hai")
    assert led.is_disputed(b.id) and "vivaad" in r.text


def test_customer_payment_claim_never_writes_without_shopkeeper(env):
    svc, led, clk, a, b = env
    led.add_udhaar(a.id, 50000)
    r = svc.on_customer_message(1, a.id, "maine paanch sau de diye")
    assert not r.wrote and "dava" in r.text and led.balance(a.id) == 50000
    r = svc.on_typed(1, "haan")
    assert r.wrote and led.balance(a.id) == 0
    assert led.entries(a.id)[-1].meta["src"] == "customer_voice"


def test_customer_promise_needs_shopkeeper_confirm_and_date(env):
    svc, led, clk, a, b = env
    led.add_udhaar(a.id, 50000)
    r = svc.on_customer_message(1, a.id, "main shukravar tak dedh sau dunga")
    assert "Shukravar 6 Nov" in r.text and led.promises(a.id) == []
    svc.on_typed(1, "haan")
    assert len(led.promises(a.id)) == 1
    r = svc.on_customer_message(1, a.id, "main dunga")
    assert "Kab tak" in r.text


def test_customer_unknown_message_is_just_reported(env):
    svc, led, clk, a, b = env
    r = svc.on_customer_message(1, a.id, "namaste bhaiya")
    assert "namaste bhaiya" in r.text and led.balance(a.id) == 0


# ---------------- english reminders + voice notes
def test_english_number_words():
    assert rupees_to_words_en(125000) == "one lakh twenty five thousand"
    assert rupees_to_words_en(75440) == "seventy five thousand four hundred forty"
    assert rupees_to_words_en(100) == "one hundred" and rupees_to_words_en(0) == "zero"
    assert paise_to_words_en(15050) == "one hundred fifty rupees fifty paise"


def test_english_customer_gets_english_text_and_en_IN_voice(env):
    svc, led, clk, a, b = env
    led.add_udhaar(a.id, 150000)
    led.grant_consent(a.id)
    led.set_flag(a.id, lang="en")
    d, draft = draft_reminder(led, CFG, a.id, "Gupta Kirana", now=clk.t)
    assert draft.lang == "en" and draft.tts_language == "en-IN"
    assert "₹1,500" in draft.text and "AI assistant" in draft.text
    assert "one thousand five hundred rupees" in draft.voice_text
    assert not any(ch.isdigit() for ch in draft.voice_text)


async def test_plan_approval_returns_voice_notes_with_language_and_slot(env):
    svc, led, clk, a, b = env
    led.add_udhaar(a.id, 150000)
    led.grant_consent(a.id)
    led.set_flag(a.id, lang="en")
    clk.t += 20 * DAY
    await svc.plan(1)
    r = svc.on_callback(1, "appr:all")
    assert r.voices and r.voices[0][1] == "en-IN" and r.voices[0][2] == 0.95
    assert "behtar samay" in r.text


# ---------------- critic
async def test_critic_rejects_stale_and_tampered_drafts(env):
    svc, led, clk, a, b = env
    led.add_udhaar(a.id, 150000)
    led.grant_consent(a.id)
    clk.t += 20 * DAY
    run = await run_agent(led, CFG, "S", clk.t)
    assert len(led.pending_approvals()) == 1 and not run.critic_rejected
    item = led.pending_approvals()[0]
    assert verify_item(led, CFG, item, clk.t) == []
    led.record_payment(a.id, 50000)                                        # balance changed after drafting
    assert "stale_amount" in verify_item(led, CFG, item, clk.t)
    bad = {**item, "payload": {**item["payload"], "text": "Pay NOW or police will come"}}
    iss = verify_item(led, CFG, bad, clk.t)
    assert any(i.startswith("lint:") for i in iss) and "name_mismatch" in iss


async def test_critic_blocks_in_runner_and_logs_by_critic(env):
    svc, led, clk, a, b = env
    led.add_udhaar(a.id, 150000)
    led.grant_consent(a.id)
    clk.t += 20 * DAY

    class Sneaky:                       # a planner that mutates state after queuing, so the draft goes stale
        name = "sneaky"

        def __init__(self):
            self.n = 0

        async def next(self, h):
            from munim.agent.planner import Done, Step

            self.n += 1
            if self.n == 1:
                return Step("queue_reminder", {"customer_id": a.id})
            led.record_payment(a.id, 1000)
            return Done()

    run = await run_agent(led, CFG, "S", clk.t, Sneaky())
    assert run.critic_rejected and led.pending_approvals() == []
    ev = [e for e in led.entries() if e.kind == "agent_decision"]
    assert ev and ev[-1].meta["by"] == "critic" and ev[-1].meta["approved"] is False


# ---------------- timing bandit
def test_slot_of_and_stats(env):
    svc, led, clk, a, b = env
    assert slot_of(datetime(2026, 11, 2, 9, 0, tzinfo=IST).timestamp()) == "morning"
    assert slot_of(datetime(2026, 11, 2, 13, 0, tzinfo=IST).timestamp()) == "midday"
    assert slot_of(datetime(2026, 11, 2, 17, 0, tzinfo=IST).timestamp()) == "evening"
    assert slot_of(datetime(2026, 11, 2, 21, 0, tzinfo=IST).timestamp()) is None
    clk.t = datetime(2026, 11, 2, 9, 0, tzinfo=IST).timestamp()
    led.add_udhaar(a.id, 1000)
    led.log_reminder(a.id)
    clk.t += DAY
    led.record_payment(a.id, 500)
    assert slot_stats(led)["morning"] == (1, 1) and slot_stats(led)["evening"] == (0, 0)


def test_thompson_converges_to_best_slot():
    rng = random.Random(0)
    truth = {"morning": 0.55, "midday": 0.25, "evening": 0.30}
    stats = {s: (0, 0) for s in SLOTS}
    picks = []
    for _ in range(400):
        s = thompson_pick(stats, rng)
        w, n = stats[s]
        stats[s] = (w + (rng.random() < truth[s]), n + 1)
        picks.append(s)
    assert picks[-100:].count("morning") > 70                                   # exploits the true best
    assert all(stats[s][1] > 0 for s in SLOTS)                                  # but did explore every slot


def test_suggest_slot_is_stable_within_a_day(env):
    svc, led, clk, a, b = env
    assert suggest_slot(led, clk.t) == suggest_slot(led, clk.t + 3600)


# ---------------- forecast
def test_forecast_pure_function_behaviour():
    now = 100 * DAY
    pays = [now - d * DAY for d in (70, 56, 42, 28, 14)]       # every 14 days, last paid exactly 14 days ago
    f = customer_forecast(1, pays, [50000] * 5, 200000, now, 14)
    assert f.p >= 0.9 and f.amount == 50000
    f_overdue = customer_forecast(1, pays[:-1] + [now - 60 * DAY + 1], [50000] * 5, 200000, now, 14)
    assert f_overdue.p <= 0.25
    assert customer_forecast(1, [], [], 100000, now).p == 0.15                  # no history -> low prior
    assert customer_forecast(1, pays, [1] * 5, 0, now).expected == 0
    promised = customer_forecast(1, [], [], 100000, now, promise_pending=True)
    assert promised.p == 0.6 and customer_forecast(1, [], [], 1000, now, promise_pending=True, kept_rate=0.9).p == 0.9


def test_forecast_over_ledger_has_interval(env):
    svc, led, clk, a, b = env
    for i in range(5):
        led.add_udhaar(a.id, 40000)
        clk.t += 14 * DAY
        led.record_payment(a.id, 30000)
    led.add_udhaar(b.id, 90000)
    f = forecast(led, clk.t, 14)
    assert f["low_paise"] <= f["expected_paise"] <= f["high_paise"] and f["expected_paise"] > 0
    assert {c.customer_id for c in f["customers"]} == {a.id, b.id}


def test_backtest_script_runs_and_has_no_future_leakage():
    import importlib

    mod = importlib.import_module("bench.backtest_forecast")
    import random as r
    from data.real.build_realistic_ledger import build, load_names
    import tempfile, os

    led = build([20, 45, 60, 99, 120, 35, 250, 18, 75, 140], load_names(None, r.Random(1), 25),
                os.path.join(tempfile.mkdtemp(), "b.db"), days=200)
    res = mod.backtest(led, weeks=6)
    assert res["n_cutoffs"] == 6 and 0 <= res["brier_model"] < res["brier_constant"] + 0.05
