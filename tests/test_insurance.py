import pytest

from munim.config import Settings
from munim.insurance import micro as M
from munim.tenancy.registry import Registry

NOW = 1_800_000_000.0


@pytest.fixture
def env(tmp_path):
    reg = Registry(str(tmp_path), "m", Settings(), clock=lambda: NOW)
    t = reg.create_shop("Gupta Kirana", 1)
    c = t.ledger.add_customer("Ramesh")
    return t, c, M.Book(t, M.SandboxInsurer())


def test_indication_is_deterministic_and_needs_a_tier():
    q = M.indicate("STOCK_FIRE", "A", 10_000_000)
    assert q.annual_premium_paise == 40_000 and q.illustrative
    assert M.indicate("STOCK_FIRE", "D", 10_000_000).annual_premium_paise > q.annual_premium_paise
    assert M.indicate("STOCK_FIRE", "A", 1_000_000).annual_premium_paise == M.MIN_PREMIUM
    with pytest.raises(ValueError):
        M.indicate("STOCK_FIRE", "NA", 1)


def test_sum_insured_bounds():
    assert M.suggest_sum_insured(0) == 5_000_000
    assert M.suggest_sum_insured(10**12) == 50_000_000


def test_accrual_only_digital_idempotent_capped(env):
    t, c, book = env
    pid = book.accept_quote(M.indicate("STOCK_FIRE", "B", 6_000_000))          # premium Rs 300 (min) = 30,000 paise
    t.ledger.add_udhaar(c.id, 1_000_000)
    t.ledger.record_payment(c.id, 100, src="voice")                            # cash/voice: not qualifying
    for i in range(3):
        t.ledger.record_payment(c.id, 100 + i, src="upi", upi=f"u{i}")
    assert book.accrue(pid) == 150
    assert book.accrue(pid) == 0                                               # idempotent
    t.ledger.record_payment(c.id, 500, src="mandate")
    assert book.accrue(pid) == 50
    assert book.policy(pid)["accrued_paise"] == 200


def test_reversed_payment_not_counted(env):
    t, c, book = env
    pid = book.accept_quote(M.indicate("STOCK_FIRE", "B", 6_000_000))
    t.ledger.add_udhaar(c.id, 10_000)
    t.ledger.record_payment(c.id, 500, src="upi", upi="x")
    t.ledger.undo_last()
    assert book.accrue(pid) == 0


def test_accrual_never_exceeds_premium(env):
    t, c, book = env
    book.set_per_event(200)
    pid = book.accept_quote(M.Quote("STOCK_FIRE", "A", 100, 300, "r"))
    t.ledger.add_udhaar(c.id, 1_000_000)
    for i in range(5):
        t.ledger.record_payment(c.id, 100, src="upi", upi=str(i))
    assert book.accrue(pid) == 300


def test_cover_is_not_active_until_insurer_binds(env):
    t, c, book = env
    pid = book.accept_quote(M.indicate("STOCK_FIRE", "B", 6_000_000))
    assert book.policy(pid)["status"] == "accepted_unpaid"
    assert "NAHI" in book.status_text(pid)
    assert book.remit(pid, 10_000) == "accepted_unpaid"
    assert book.remit(pid, 20_000) == "active"
    assert "CHALU" in book.status_text(pid)


def test_funding_plan_shows_when_50_paise_is_not_enough():
    p = M.funding_plan(annual_premium_paise=120_000, events_per_month=200)      # 200*12*50 = 1,20,000 -> exactly enough
    assert p["covers_premium"] and p["shortfall_paise"] == 0
    p = M.funding_plan(annual_premium_paise=120_000, events_per_month=40)
    assert not p["covers_premium"] and p["per_event_needed_paise"] == 250


def test_per_event_bounds(env):
    t, c, book = env
    with pytest.raises(ValueError):
        book.set_per_event(5)


def test_claims_need_active_policy_and_clear_incident(env):
    t, c, book = env
    pid = book.accept_quote(M.indicate("STOCK_FIRE", "B", 6_000_000))
    with pytest.raises(ValueError):
        book.claim_draft(pid, "kuch ho gaya", 1000)
    cid = book.claim_draft(pid, "baarish ka paani ghus gaya maal kharab", 500_000)
    with pytest.raises(ValueError):
        book.claim_submit(cid)                                                  # not active -> cannot file
    book.remit(pid, 30_000)
    assert book.claim_submit(cid).startswith("sbx-claim")


def test_narrate_rejects_invented_numbers_and_promises():
    facts = {"p": "Rs 300", "s": "50,000"}
    tmpl = "template"
    assert M.narrate({"summary": "Premium 300 rupaye, bima 50,000."}, facts, tmpl).startswith("Premium")
    assert M.narrate({"summary": "Premium 250 rupaye."}, facts, tmpl) == tmpl
    assert M.narrate({"summary": "Guaranteed cover, 300."}, facts, tmpl) == tmpl
    assert M.narrate(None, facts, tmpl) == tmpl
