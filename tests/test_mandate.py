import pytest

from munim.config import Settings
from munim.payments.mandate import CreditEvent, Engine, MandateError, SandboxRail
from munim.tenancy.registry import Registry

NOW = 1_800_000_000.0


@pytest.fixture
def env(tmp_path):
    reg = Registry(str(tmp_path), "m", Settings(), clock=lambda: NOW)
    t = reg.create_shop("Gupta Kirana", 1)
    c = t.ledger.add_customer("Ramesh Sharma")
    t.ledger.grant_consent(c.id, "customer_self_linked")
    t.ledger.add_udhaar(c.id, 100_000, src="voice")                    # Rs 1,000 owed
    rail = SandboxRail()
    return t, c, Engine(t, rail), rail


def ev(m, credit, ref="r1", src="UPI_INFLOW"):
    return CreditEvent(m.id, ref, credit, src, NOW)


def test_needs_self_linked_consent(env):
    t, c, eng, rail = env
    d = t.ledger.add_customer("Shop Attested")
    t.ledger.grant_consent(d.id)                                       # shopkeeper-attested only
    t.ledger.add_udhaar(d.id, 5000)
    with pytest.raises(MandateError):
        eng.propose(d.id)


def test_only_customer_accepts_and_caps_hold(env):
    t, c, eng, rail = env
    with pytest.raises(MandateError):
        eng.propose(c.id, cap_pct=80)                                  # household must keep the majority
    m = eng.propose(c.id, cap_pct=25, per_sweep_paise=30_000)
    with pytest.raises(MandateError):
        eng.accept(m.id, by_customer=False)
    m = eng.accept(m.id, by_customer=True)
    assert m.status == "active"
    out = eng.on_event(ev(m, 200_000))                                 # 25 % of Rs 2,000 = Rs 500 but per-sweep cap Rs 300
    assert out["status"] == "swept" and out["paise"] == 30_000
    assert t.ledger.balance(c.id) == 70_000
    assert t.ledger.entries(c.id)[-1].meta["src"] == "mandate"


def test_idempotent_and_total_cap_and_exhaustion(env):
    t, c, eng, rail = env
    m = eng.accept(eng.propose(c.id, cap_pct=40, per_sweep_paise=1_000_000).id, True)
    assert eng.on_event(ev(m, 100_000, "a"))["paise"] == 40_000
    assert eng.on_event(ev(m, 100_000, "a"))["why"] == "duplicate_event"
    assert eng.on_event(ev(m, 100_000, "b"))["paise"] == 40_000
    assert eng.on_event(ev(m, 100_000, "c"))["paise"] == 20_000        # only Rs 200 left owed / cap
    assert t.ledger.balance(c.id) == 0
    assert eng.get(m.id).status == "exhausted"
    assert eng.on_event(ev(m, 100_000, "d"))["why"] == "no_active_mandate"


def test_source_gate_dbt_is_opt_in(env):
    t, c, eng, rail = env
    m = eng.accept(eng.propose(c.id).id, True)
    assert eng.on_event(ev(m, 500_000, "x", src="DBT"))["why"] == "source_not_allowed"
    assert t.ledger.balance(c.id) == 100_000


def test_dispute_blocks_and_revocation_is_instant(env):
    t, c, eng, rail = env
    m = eng.accept(eng.propose(c.id).id, True)
    t.ledger.open_dispute(c.id)
    assert eng.on_event(ev(m, 100_000, "z"))["why"] == "blocked"
    t.ledger.resolve_dispute(c.id)
    assert eng.revoke(m.id, "customer") and rail.cancelled == [m.id]
    assert eng.on_event(ev(m, 100_000, "y"))["why"] == "no_active_mandate"


def test_rail_failure_records_nothing(env):
    t, c, eng, rail = env
    m = eng.accept(eng.propose(c.id).id, True)
    rail.fail = True
    assert eng.on_event(ev(m, 100_000))["status"] == "rail_failed"
    assert t.ledger.balance(c.id) == 100_000


def test_webhook_signature(env):
    t, c, eng, rail = env
    m = eng.accept(eng.propose(c.id).id, True)
    raw, sig = rail.sign(ev(m, 100_000, "w1"))
    assert eng.on_webhook(raw, "0" * 64)["why"] == "bad_signature"
    assert eng.on_webhook(raw, sig)["status"] == "swept"


def test_expiry_and_coverage_is_not_a_guarantee(env):
    t, c, eng, rail = env
    m = eng.accept(eng.propose(c.id, days=30).id, True)
    cov = eng.coverage()
    assert cov["covered_pct"] == 100 and "not a default guarantee" in cov["note"]
    t._clock = lambda: NOW + 40 * 86400
    assert eng.on_event(ev(m, 100_000, "late"))["why"] == "expired"


def test_chain_records_lifecycle(env):
    t, c, eng, rail = env
    m = eng.accept(eng.propose(c.id).id, True)
    eng.revoke(m.id, "owner")
    kinds = [e.kind for e in t.ledger.entries(c.id)]
    assert {"mandate_proposed", "mandate_active", "mandate_revoked"} <= set(kinds)
    assert t.ledger.verify()[0]
