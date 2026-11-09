import pytest

from munim.config import Settings
from munim.insights.staff_audit import audit_staff
from munim.tenancy.quota import GnaniQuota
from munim.tenancy.registry import Registry
from munim.tenancy.roles import CUSTOMER, OWNER, STAFF, can


@pytest.fixture
def reg(tmp_path):
    t = [1_700_000_000.0]
    r = Registry(str(tmp_path), "master", Settings(), clock=lambda: t[0])
    r.t = t
    return r


def test_shops_are_isolated_and_secrets_differ(reg):
    a = reg.create_shop("Gupta Store", 1)
    b = reg.create_shop("Sharma Kirana", 2)
    ca = a.ledger.add_customer("Ramesh")
    cb = b.ledger.add_customer("Ramesh")
    a.ledger.add_udhaar(ca.id, 10000)
    assert b.ledger.balance(cb.id) == 0
    assert a.ledger.ref(ca.id) != b.ledger.ref(cb.id)            # same customer id, different pseudonym per shop
    assert reg.tenant_for(1)[0].shop_id == a.shop_id and reg.tenant_for(2)[0].name == "Sharma Kirana"
    assert reg.tenant_for(99) is None and reg.shop_count() == 2


def test_chat_cannot_join_twice(reg):
    reg.create_shop("Alpha Store", 1)
    with pytest.raises(ValueError):
        reg.create_shop("Beta Store", 1)


def test_staff_invite_one_time_and_expiry(reg):
    t = reg.create_shop("Alpha Store", 1)
    tok = reg.make_invite(t.shop_id, STAFF)
    m = reg.redeem_invite(tok, 5, "munshi-1")
    assert m and m.role == STAFF and m.label == "munshi-1"
    assert reg.redeem_invite(tok, 6) is None                       # single use
    tok2 = reg.make_invite(t.shop_id, STAFF, ttl_s=10)
    reg.t[0] += 11
    assert reg.redeem_invite(tok2, 7) is None                      # expired
    assert reg.redeem_invite("garbage", 8) is None
    assert reg.staff_labels(t.shop_id) == {5: "munshi-1"}


def test_customer_invite_binds_customer(reg):
    t = reg.create_shop("Alpha Store", 1)
    c = t.ledger.add_customer("Sunita Verma")
    m = reg.redeem_invite(reg.make_invite(t.shop_id, CUSTOMER, c.id), 50)
    assert m.role == CUSTOMER and m.customer_id == c.id
    with pytest.raises(ValueError):
        reg.make_invite(t.shop_id, OWNER)


def test_permissions_deny_by_default():
    assert can(OWNER, "undo") and can(STAFF, "entry") and can(STAFF, "query")
    assert not can(STAFF, "undo") and not can(STAFF, "money_admin") and not can(STAFF, "communicate")
    assert not can(CUSTOMER, "entry") and can(CUSTOMER, "portal")
    assert not can(None, "query") and can(None, None)


def test_quota_fair_share_and_day_roll():
    t = [1_700_000_000.0]
    q = GnaniQuota(per_shop_daily=10, global_daily=15, clock=lambda: t[0])
    assert q.allow("a", 3)
    q.charge("a", 9)
    assert not q.allow("a", 3) and q.remaining("a") == 1
    q.charge("b", 6)
    assert q.remaining("c") == 0                                   # global budget gone: one shop cannot starve... all of them
    t[0] += 86400
    assert q.remaining("a") == 10


def test_actor_tag_and_staff_audit(reg):
    t = reg.create_shop("Alpha Store", 1)
    c = t.ledger.add_customer("Ramesh Sharma")
    svc = t.service
    svc.actors[7] = "munshi-1"
    for i in range(10):
        svc.on_typed(7, f"Ramesh ko {100 + i} udhaar likh do")
        svc.on_callback(7, "yes") if svc.pending.get(7) else None
    ents = [e for e in t.ledger.entries() if e.kind == "add_udhaar"]
    assert ents and all(e.meta.get("by") == "munshi-1" for e in ents)
    svc.on_typed(1, "Ramesh ko 50 udhaar likh do")                 # owner entry: untagged
    assert any("by" not in e.meta for e in t.ledger.entries() if e.kind == "add_udhaar")
    for e in ents[:5]:
        t.ledger.reverse_seq(e.seq)
    stats, flags = audit_staff(t.ledger)
    assert stats["munshi-1"]["entries"] == len(ents)
    assert any(f.label == "munshi-1" and f.code == "high_reversal_rate" for f in flags)
