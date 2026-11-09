from datetime import datetime, timedelta

import pytest

from munim.govern.governor import IST
from munim.insights import engine as E
from munim.ledger.chain import Ledger

DAY = 86400


class Clk:
    def __init__(self, dt):
        self.t = dt.timestamp()

    def __call__(self):
        return self.t

    def go(self, days=0, hours=0):
        self.t += days * DAY + hours * 3600


@pytest.fixture
def env(tmp_path):
    clk = Clk(datetime(2026, 8, 1, 11, 0, tzinfo=IST))
    led = Ledger(str(tmp_path / "i.db"), "k", clock=clk)
    return led, clk


def test_fifo_payment_clears_oldest_first(env):
    led, clk = env
    c = led.add_customer("A")
    led.add_udhaar(c.id, 10000)           # day 0
    clk.go(days=40)
    led.add_udhaar(c.id, 20000)           # day 40
    clk.go(days=1)
    led.record_payment(c.id, 12000)       # clears the 100 fully, 20 of the 200
    items = E.open_items(led, c.id)
    assert [i.remaining for i in items] == [18000]
    now = clk.t + 10 * DAY
    assert E.aging(led, c.id, now)["8-30"] == 18000        # 200-item is ~11 days old
    assert E.oldest_open_days(led, c.id, now) == 11


def test_reversed_entries_ignored_in_aging(env):
    led, clk = env
    c = led.add_customer("A")
    led.add_udhaar(c.id, 5000)
    led.undo_last(c.id)
    assert E.open_items(led, c.id) == []


def build_bad_payer(led, clk, c):
    led.add_udhaar(c.id, 50000)
    clk.go(days=70)


def test_risk_tiers_and_reasons(env):
    led, clk = env
    bad, good, none = led.add_customer("Bad"), led.add_customer("Good"), led.add_customer("None")
    build_bad_payer(led, clk, bad)
    led.add_udhaar(good.id, 20000)
    clk.go(days=3)
    led.record_payment(good.id, 15000)
    r = E.risk(led, bad.id, clk.t)
    assert r.tier == "high" and {"old_balance", "never_paid"} <= set(r.codes)
    assert E.risk(led, good.id, clk.t).tier == "low"
    assert E.risk(led, none.id, clk.t).score == 0


def test_broken_promise_raises_risk_and_kept_does_not(env):
    led, clk = env
    c = led.add_customer("P")
    led.add_udhaar(c.id, 30000)
    clk.go(days=1)
    due = (datetime.fromtimestamp(clk.t, IST) + timedelta(days=2)).date().isoformat()
    led.add_promise(c.id, due, 30000)
    assert E.promise_statuses(led, c.id, clk.t)[0][1] == "pending"
    assert E.active_promise(led, c.id, clk.t) is not None
    clk.go(days=3)
    assert E.promise_statuses(led, c.id, clk.t)[0][1] == "grace"
    clk.go(days=3)
    assert E.promise_statuses(led, c.id, clk.t)[0][1] == "broken"
    assert "broken_promise" in E.risk(led, c.id, clk.t).codes
    assert E.active_promise(led, c.id, clk.t) is None


def test_promise_kept_by_partial_payment_if_no_amount(env):
    led, clk = env
    c = led.add_customer("P")
    led.add_udhaar(c.id, 30000)
    due = (datetime.fromtimestamp(clk.t, IST) + timedelta(days=3)).date().isoformat()
    led.add_promise(c.id, due)
    clk.go(days=2)
    led.record_payment(c.id, 5000)
    assert E.promise_statuses(led, c.id, clk.t)[0][1] == "kept"


def test_over_limit_adds_risk(env):
    led, clk = env
    c = led.add_customer("L")
    led.add_udhaar(c.id, 300000)
    base = E.risk(led, c.id, clk.t).score
    led.set_limit(c.id, 100000)
    assert E.risk(led, c.id, clk.t).score == base + 15


def test_suggested_limit_floor_and_growth(env):
    led, clk = env
    c = led.add_customer("S")
    assert E.suggested_limit(led, c.id, clk.t) == 100000
    for _ in range(3):
        led.add_udhaar(c.id, 400000)
    assert E.suggested_limit(led, c.id, clk.t) == 1_500_000      # 1.25 x peak 12,000 rupees


def test_top_total_overdue_and_summaries(env):
    led, clk = env
    a, b = led.add_customer("A"), led.add_customer("B")
    led.add_udhaar(a.id, 50000)
    led.add_udhaar(b.id, 90000)
    clk.go(days=40)
    led.add_udhaar(a.id, 1000)
    assert [c.name for c, _ in E.top_debtors(led, 2)] == ["B", "A"]
    assert E.total_outstanding(led) == 141000
    od = E.overdue(led, clk.t, 30)
    assert {c.name for c, _, _ in od} == {"A", "B"}
    ds = E.day_summary(led, clk.t)
    assert ds == {"given": 1000, "collected": 0, "entries": 1}


def test_weekly_flow_buckets(env):
    led, clk = env
    c = led.add_customer("W")
    led.add_udhaar(c.id, 10000)
    clk.go(days=8)
    led.record_payment(c.id, 4000)
    rows = E.weekly_flow(led, clk.t, 3)
    assert sum(r["given"] for r in rows) == 10000 and sum(r["collected"] for r in rows) == 4000
    assert len(rows) == 3


def test_anomalies(env):
    led, clk = env
    c = led.add_customer("An")
    for _ in range(8):
        led.add_udhaar(c.id, 10000 + 100 * _)
        clk.go(hours=5)
    led.add_udhaar(c.id, 500000)                       # huge vs history
    clk.t = datetime(2026, 8, 3, 2, 30, tzinfo=IST).timestamp()
    led.add_udhaar(c.id, 10000)                        # 02:30 IST
    for _ in range(3):
        led.add_udhaar(c.id, 7700)
    codes = {a.code for a in E.anomalies(led, clk.t)}
    assert {"unusual_amount", "odd_hour", "repeat_same_amount"} <= codes


def test_priority_orders_bigger_older_riskier_first(env):
    led, clk = env
    big_old, small_new = led.add_customer("BO"), led.add_customer("SN")
    led.add_udhaar(big_old.id, 400000)
    clk.go(days=50)
    led.add_udhaar(small_new.id, 20000)
    assert E.collection_priority(led, big_old.id, clk.t) > E.collection_priority(led, small_new.id, clk.t) > 0
    clean = led.add_customer("Clean")
    assert E.collection_priority(led, clean.id, clk.t) == 0
