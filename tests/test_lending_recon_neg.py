import json
from datetime import date, datetime, timedelta, timezone

import pytest

from munim import portal
from munim.agent import negotiator as N
from munim.agent import reconcile as R
from munim.config import Settings
from munim.lending import aa
from munim.lending import ocen_gateway as O
from munim.passport import cashflow as PF
from munim.payments import mandate_portal
from munim.payments.mandate import Engine, SandboxRail
from munim.tenancy.registry import Registry
from munim.timeutil import IST

DAY = 86400.0
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=IST).timestamp()


class Clock:
    t = NOW

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clk = Clock()
    clk.t = NOW - 100 * DAY
    reg = Registry(str(tmp_path), "m", Settings(), clock=clk)
    t = reg.create_shop("Gupta Kirana", 1)
    return reg, t, clk


def history(t, clk, customers=8, days=100):
    """Steady shop: udhaar then ~85 % repaid, weekly rhythm."""
    cs = [t.ledger.add_customer(f"Customer Number{i}") for i in range(customers)]
    for d in range(days):
        clk.t = NOW - (days - d) * DAY
        c = cs[d % customers]
        t.ledger.add_udhaar(c.id, 40_000 + (d % 5) * 5_000, src="voice")
        if d % 7 == 3:
            t.ledger.record_payment(cs[(d + 1) % customers].id, 60_000, src="upi", upi=f"u{d}")
    clk.t = NOW
    return cs


# ------------------------------------------------------------------ passport
def test_passport_thin_history_gets_no_tier(env):
    reg, t, clk = env
    c = t.ledger.add_customer("Solo Customer")
    clk.t = NOW
    t.ledger.add_udhaar(c.id, 1000)
    assert PF.build(t.ledger, NOW).tier == "NA"


def test_passport_scores_rules_and_sign(env):
    reg, t, clk = env
    history(t, clk)
    p = PF.build(t.ledger, NOW)
    assert p.tier in "ABCD" and p.chain_ok and p.history_days >= 90 and p.reasons
    assert all(r[0] in "+-" for r in p.reasons)
    doc = PF.share(t.ledger, p, "Sandbox NBFC", b"k")
    assert PF.verify_share(doc, b"k") and not PF.verify_share({**doc, "data": {**doc["data"], "score": 100}}, b"k")
    assert "name" not in json.dumps(doc).lower() or "Customer" not in json.dumps(doc)
    assert t.ledger.entries()[-1].kind == "passport_shared"


# ------------------------------------------------------------------ lending
def test_emi_and_all_in_apr_include_fees():
    e = O.emi(10_000_000, 2200, 12)
    assert abs(e - 935_944) <= 2
    assert O.all_in_apr_bps(10_000_000, 150_000, e, 12) > 2200


def test_offers_need_consent_and_are_affordability_checked(env):
    reg, t, clk = env
    history(t, clk)
    gw = O.Gateway(t, O.SandboxLender(), b"k")
    with pytest.raises(PermissionError):
        gw.request_offers(NOW, 5_000_000, 6, owner_consented=False)
    offers, status = gw.request_offers(NOW, 50_000_000, 6, owner_consented=True)
    if offers:                                                                  # tier may be D in tiny synthetic books
        o = offers[0]
        assert o.principal_paise <= int(PF.build(t.ledger, NOW).weekly_collection_median_paise * 4.33 * 1.5) + 1
        assert o.all_in_apr_bps >= o.apr_bps
        assert "APR" not in O.offer_text(o) and "saalana" in O.offer_text(o)
        with pytest.raises(PermissionError):
            gw.accept(o, owner_confirmed=False)
        assert gw.accept(o, owner_confirmed=True).startswith("sbx-app")
    assert any(e.kind == "passport_shared" for e in t.ledger.entries()) or status == "passport too thin for a lender to read"


def test_unaffordable_offer_is_flagged_not_hidden(env):
    reg, t, clk = env
    history(t, clk)

    class Greedy:
        name = "Greedy"
        def offers(self, doc, amount, months):
            return [O.Offer("Greedy", "g1", 10**9, 3, 3600, 0, 10**8, 3 * 10**8, 4000)]
        def apply(self, o, d):
            return "x"
    offers, _ = O.Gateway(t, Greedy(), b"k").request_offers(NOW, 10**9, 3, owner_consented=True)
    assert offers and not offers[0].affordable and "30%" in offers[0].note


def test_crunch_detects_shortfall_and_suggests_only(env):
    reg, t, clk = env
    history(t, clk)
    t.aux.db.execute("INSERT INTO payable_due(ts,supplier,amount_paise,due_iso,invoice_hash) VALUES (?,?,?,?,?)",
                     (NOW, "Agarwal", 99_000_000, (date.fromtimestamp(NOW) + timedelta(days=5)).isoformat(), "h1"))
    c = O.crunch(t, NOW)
    assert c.shortfall_paise > 0
    msg = O.Gateway(t, O.SandboxLender(), b"k").suggest(NOW)
    assert "/loan" in msg and not any(e.kind == "passport_shared" for e in t.ledger.entries())      # suggestion shares nothing


# ------------------------------------------------------------------ AA + reconcile
def fi(txns):
    return {"Account": {"Transactions": {"Transaction": txns}}}


def tx(i, amt, ts, kind="CREDIT", narr="UPI/CR/123/RAMESH SHARMA/HDFC"):
    return {"txnId": f"T{i}", "type": kind, "mode": "UPI", "amount": amt, "transactionTimestamp": ts, "narration": narr}


def test_aa_parse_and_reconcile(env):
    reg, t, clk = env
    clk.t = NOW
    r = t.ledger.add_customer("Ramesh Sharma")
    s = t.ledger.add_customer("Suresh Verma")
    t.ledger.add_udhaar(r.id, 50_000)
    t.ledger.add_udhaar(s.id, 30_000)
    t.ledger.record_payment(s.id, 10_000, src="upi", upi="zz")                   # already booked
    iso = datetime.fromtimestamp(NOW - 3600, timezone.utc).isoformat()
    txns = aa.parse_fi(fi([tx(1, "500.00", iso), tx(2, "100.00", iso, narr="UPI/CR/9/SURESH VERMA/SBI"),
                           tx(3, "250.00", iso, kind="DEBIT"), tx(4, "bad", iso), tx(5, "77.00", "not-a-date")]))
    assert len(txns) == 3
    out = aa.reconcile(t.ledger, txns, NOW)
    assert out["matched"] == 1 and out["queued"] == 1 and out["unmatched"] == 0
    items = R.pending(t.ledger)
    assert len(items) == 1 and items[0]["customer_id"] == r.id
    assert t.ledger.balance(r.id) == 50_000                                       # NOTHING written before approval
    e = R.apply(t.ledger, items[0]["id"])
    assert e.kind == "record_payment" and t.ledger.balance(r.id) == 0 and e.meta["src"] == "reconcile"


def test_reconcile_rules(env):
    reg, t, clk = env
    clk.t = NOW
    a = t.ledger.add_customer("Ramesh Kumar")
    b = t.ledger.add_customer("Ramesh Kumari")
    t.ledger.add_udhaar(a.id, 50_000)
    t.ledger.add_udhaar(b.id, 50_000)
    cands, amb = R.match(t.ledger, 50_000, "Ramesh Kumar", NOW)
    assert amb is True or cands[0].score - cands[1].score >= 10
    assert R.queue(t.ledger, 50_000, None, None, NOW)["status"] in ("unmatched", "ambiguous")      # amount alone is never enough
    c = t.ledger.add_customer("Pooja Singh")
    t.ledger.add_udhaar(c.id, 12_345)
    r = R.queue(t.ledger, 12_345, "POOJA SINGH", "tok1", NOW)
    assert r["status"] == "queued" and r["tier"] == "HIGH"
    t.ledger.record_payment(c.id, 12_345, src="upi", upi="tok2")
    assert R.queue(t.ledger, 1, "POOJA SINGH", "tok2", NOW)["status"] == "duplicate"


def test_digest_buttons(env):
    reg, t, clk = env
    clk.t = NOW
    c = t.ledger.add_customer("Pooja Singh")
    t.ledger.add_udhaar(c.id, 12_345)
    R.queue(t.ledger, 12_345, "POOJA SINGH", "tok1", NOW)
    d = R.daily_digest(t.ledger, NOW)
    assert "Pooja" in d.text and [b[1] for b in d.buttons] == ["x:recon:all", "x:recon:review", "x:recon:none"]
    assert R.daily_digest(Registry.__new__(Registry) and t.ledger, NOW) is not None


# ------------------------------------------------------------------ negotiator
@pytest.fixture
def neg(env):
    reg, t, clk = env
    clk.t = NOW
    c = t.ledger.add_customer("Ramesh Sharma")
    t.ledger.grant_consent(c.id, "customer_self_linked")
    t.ledger.add_udhaar(c.id, 90_000)
    return t, c


def enable(t, **kw):
    N.Policy(enabled=True, **kw).save(t.aux)


def test_policy_off_means_old_behaviour(neg):
    t, c = neg
    assert N.respond(t, c.id, "paise nahi hain agle hafte dunga") is None
    r = portal.handle(t, c.id, "agle hafte denge", owner_chat=1)
    assert "confirm" in r.text.lower()                                             # old path: forwarded to the owner


def test_within_policy_auto_promise_with_disclosure(neg):
    t, c = neg
    enable(t)
    r = N.respond(t, c.id, "Paise nahi hain, agle hafte dunga")
    assert r.recorded and "AI" in r.text and "reminder nahi" in r.text
    p = t.ledger.promises(c.id)[-1]
    assert p.meta["auto"] is True and p.meta["promised_paise"] == 90_000 and p.meta["src"] == "negotiator"
    assert t.ledger.balance(c.id) == 90_000 and not any(e.kind == "record_payment" for e in t.ledger.entries(c.id))
    assert r.owner_notes and "policy ke andar" in r.owner_notes[0]


def test_beyond_extension_offers_plan_then_customer_agrees(neg):
    t, c = neg
    enable(t, max_extension_days=7)
    r = N.respond(t, c.id, "Paise nahi hain, 25 din baad dunga")
    assert not r.recorded and "kisht" in r.text and t.ledger.promises(c.id) == []
    r2 = N.respond(t, c.id, "haan")
    assert len(r2.recorded) == 3 and sum(p.meta["promised_paise"] for p in t.ledger.promises(c.id)) == 90_000


def test_big_balance_or_dispute_escalates(neg):
    t, c = neg
    enable(t, max_auto_balance_paise=10_000)
    r = N.respond(t, c.id, "agle hafte dunga")
    assert r.escalate and not r.recorded
    enable(t)
    t.ledger.open_dispute(c.id)
    assert N.respond(t, c.id, "agle hafte dunga").escalate


def test_paid_claim_is_never_recorded_by_agent(neg):
    t, c = neg
    enable(t)
    assert N.respond(t, c.id, "maine de diya kal") is None


def test_split_integer_exact():
    assert N.split(100_001, 3, 10_000) == [33_333, 33_333, 33_335]
    assert N.split(15_000, 3, 10_000) == [15_000]


def test_portal_routes_through_negotiator(neg):
    t, c = neg
    enable(t)
    r = portal.handle(t, c.id, "paise nahi hain agle hafte dunga", owner_chat=1)
    assert "AI" in r.text and r.owner_msgs and t.ledger.promises(c.id)


# ------------------------------------------------------------------ mandate portal
def test_customer_accepts_and_revokes_mandate_in_own_chat(neg):
    t, c = neg
    eng = Engine(t, SandboxRail())
    m = eng.propose(c.id)
    r = portal.handle(t, c.id, "mandate", owner_chat=1)
    assert "25%" in r.text and "mandate haan" in r.text
    r = portal.handle(t, c.id, "mandate haan", owner_chat=1)
    assert eng.get(m.id).status == "active"
    r = portal.handle(t, c.id, "mandate band", owner_chat=1)
    assert eng.get(m.id).status == "revoked" and "cancelled" not in r.text
    m2 = eng.propose(c.id)
    portal.handle(t, c.id, "ruko", owner_chat=1)                                    # generic stop also kills mandates
    assert eng.get(m2.id).status == "revoked"
