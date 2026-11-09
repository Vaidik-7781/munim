from datetime import datetime, timedelta

import pytest

from munim.audit.auditor import BLOCK, AuditContext, audit
from munim.config import Settings
from munim.insights import engine as E
from munim.insights.reports import report_kind, render, morning_briefing
from munim.ledger.chain import Ledger
from munim.scribe.extract import extract
from munim.scribe.names import Customer
from munim.service import MunimService
from munim.timeutil import IST

CFG = Settings()
RS = 100
CUSTS = [Customer(1, "Ramesh Sharma"), Customer(2, "Sunita Verma")]
MON = datetime(2026, 11, 2, 11, 0, tzinfo=IST)


class Clk:
    def __init__(self):
        self.t = MON.timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clk = Clk()
    led = Ledger(str(tmp_path / "f.db"), "k", clock=clk)
    a = led.add_customer("Ramesh Sharma")
    b = led.add_customer("Sunita Verma")
    return MunimService(led, CFG, "Gupta Kirana", clock=clk), led, clk, a, b


# ---------------- extraction of new intents
@pytest.mark.parametrize("text,kind", [
    ("sabse zyada udhaar kiska hai", "top"), ("aaj ka hisaab batao", "today"), ("kul kitna udhaar baaki hai", "total"),
    ("is hafte kitna gaya", "week"), ("kaun bahut din se baaki hai", "overdue"), ("kaun bhagega risk dikhao", "risk"),
])
def test_report_kind(text, kind):
    assert report_kind(text) == kind
    assert extract(text, CUSTS).intent == "report_query"


def test_balance_query_not_hijacked_by_reports():
    assert extract("Sharma ji ka kitna baaki hai", CUSTS).intent == "balance_query"


def test_promise_extraction_amount_not_polluted_by_date():
    e = extract("Sharma ji 15 tarikh tak dedh sau denge", CUSTS, MON.date())
    assert e.intent == "promise_to_pay" and e.when.day.isoformat() == "2026-11-15"
    assert e.amount_paise == 150 * RS
    e2 = extract("Sharma ji teen din mein denge", CUSTS, MON.date())
    assert e2.when.day.isoformat() == "2026-11-05" and e2.amount_paise is None


def test_promise_without_date_asks(env):
    svc, led, clk, a, b = env
    r = svc.on_typed(1, "Sharma ji denge")
    assert "Kab tak" in r.text


# ---------------- promise flow end to end
def test_promise_flow_blocks_reminders_until_due(env):
    svc, led, clk, a, b = env
    svc.on_typed(1, "Sharma ji ko paanch sau udhaar")           # typed => soft write
    clk.t += 200
    led.grant_consent(a.id)
    r = svc.on_typed(1, "Sharma ji shukravar tak dedh sau denge")
    assert "Shukravar 6 Nov" in r.text and "Sahi hai" in r.text and led.promises(a.id) == []
    r = svc.on_typed(1, "haan")
    assert "vaada darj" in r.text and len(led.promises(a.id)) == 1
    r = svc.on_typed(1, "Sharma ji ko reminder bhejo")
    assert "vaada" in r.text and "taiyaar" not in r.text           # Governor: promise_pending
    # after due + grace, with no payment, reminders are allowed again and the promise is 'broken'
    clk.t = datetime(2026, 11, 10, 11, 0, tzinfo=IST).timestamp()
    r = svc.on_typed(1, "Sharma ji ko reminder bhejo")
    assert "taiyaar" in r.text
    assert E.promise_statuses(led, a.id, clk.t)[0][1] == "broken"
    assert led.verify()[0]


def test_promise_no_changes_balance_and_nahi_discards(env):
    svc, led, clk, a, b = env
    svc.on_typed(1, "Sharma ji ko 500 udhaar")
    before = led.balance(a.id)
    svc.on_typed(1, "Sharma ji kal denge")
    svc.on_typed(1, "nahi")
    assert led.promises(a.id) == [] and led.balance(a.id) == before


# ---------------- reports via the service
def test_voice_report_top_and_total(env):
    svc, led, clk, a, b = env
    svc.on_typed(1, "Sharma ji ko paanch sau udhaar")
    clk.t += 200
    svc.on_typed(1, "Verma ji ko do sau udhaar")
    r = svc.on_typed(1, "sabse zyada udhaar kiska hai")
    assert "Ramesh Sharma" in r.text and "₹500" in r.text and "Ramesh Sharma" in r.voice_text
    assert not any(ch.isdigit() for ch in r.voice_text)
    r = svc.on_typed(1, "kul kitna udhaar baaki hai")
    assert "₹700" in r.text


def test_morning_briefing_voice_has_no_digits(env):
    svc, led, clk, a, b = env
    led.add_udhaar(a.id, 123400)
    rep = morning_briefing(led, clk.t, "Gupta Kirana")
    assert "Suprabhaat" in rep.text and "₹1,234" in rep.text
    assert not any(ch.isdigit() for ch in rep.voice)


def test_render_every_kind_on_empty_ledger(tmp_path):
    led = Ledger(str(tmp_path / "e.db"), "k")
    for k in ("total", "top", "today", "week", "overdue", "risk"):
        r = render(k, led, MON.timestamp())
        assert r.text and r.voice


# ---------------- credit limit in Auditor
def test_over_limit_blocks_and_is_enforced_only_when_set(env):
    svc, led, clk, a, b = env
    svc.on_typed(1, "Sharma ji ko paanch sau udhaar")
    clk.t += 200
    led.set_limit(a.id, 60000)                                  # Rs 600
    r = svc.on_typed(1, "Sharma ji ko dedh sau udhaar")
    assert not r.wrote and "tay seema" in r.text                # 500+150 > 600 -> asks, says why
    r2 = svc.on_typed(1, "haan")
    assert r2.wrote                                             # shopkeeper may override explicitly
    ex = extract("Sunita Verma ko dedh sau udhaar", CUSTS)
    res = audit(AuditContext(a=ex, b=ex, snr_db=30, duration_s=3, balance_paise=10 ** 9), CFG)
    assert "over_limit" not in res.codes                        # no limit set => never flagged
