import time
from datetime import date

import pytest

from munim.config import Settings
from munim.consortium.pool import K_MIN, ManualWeather, Pool, week_of
from munim.inventory.stock import Inventory
from munim.tenancy.registry import Registry

NOW = time.mktime(date(2026, 5, 20).timetuple()) + 12 * 3600       # a Wednesday in May


class Clock:
    t = NOW

    def __call__(self):
        return self.t


@pytest.fixture
def world(tmp_path):
    clk = Clock()
    clk.t = NOW
    reg = Registry(str(tmp_path), "m", Settings(), clock=clk)
    pool = Pool(reg, clock=clk)
    return reg, pool, clk


def make_shop(reg, i, pin="800001", sales=None):
    t = reg.create_shop(f"Shop number {i}", 1000 + i)
    reg.set_pincode(t.shop_id, pin)
    inv = Inventory(t.aux, reg._clock)
    s = inv.upsert("Cold Drink", "pc")
    inv.restock(s.id, 10_000)
    for wk, units in (sales or {}).items():
        reg._clock.t = NOW - wk * 7 * 86400
        inv.sale(s.id, units)
    reg._clock.t = NOW
    return t


def fill(reg, pool, n, weeks_units, pin="800001"):
    shops = [make_shop(reg, i, pin, weeks_units(i)) for i in range(n)]
    for t in shops:
        pool.contribute(t.shop_id)
    return shops


def test_k_anonymity_blocks_small_pools(world):
    reg, pool, clk = world
    fill(reg, pool, K_MIN - 1, lambda i: {0: 10})
    a = pool.aggregate("800001", "cold drink", week_of(date(2026, 5, 20)))
    assert not a.ok and a.units is None and "at least" in a.why


def test_dominance_rule(world):
    reg, pool, clk = world
    fill(reg, pool, K_MIN, lambda i: {0: 1000 if i == 0 else 10})
    a = pool.aggregate("800001", "cold drink", week_of(date(2026, 5, 20)))
    assert not a.ok and "dominates" in a.why


def test_aggregate_released_and_shops_are_pseudonymous(world):
    reg, pool, clk = world
    shops = fill(reg, pool, K_MIN, lambda i: {0: 20})
    a = pool.aggregate("800001", "cold drink", week_of(date(2026, 5, 20)))
    assert a.ok and a.units == 20 * K_MIN
    raw = " ".join(str(tuple(r)) for r in pool.db.execute("SELECT * FROM velocity"))
    assert all(t.shop_id not in raw and t.name not in raw for t in shops)


def test_pincodes_do_not_mix(world):
    reg, pool, clk = world
    fill(reg, pool, K_MIN, lambda i: {0: 20}, pin="800001")
    assert not pool.aggregate("800002", "cold drink", week_of(date(2026, 5, 20))).ok


def test_forecast_surge_with_heatwave(world):
    reg, pool, clk = world
    fill(reg, pool, K_MIN, lambda i: {0: 20, 1: 20, 2: 20, 3: 20})
    f = pool.forecast("800001", "cold drink")
    assert f["ok"] and f["surge"] is False and f["multiplier"] == 1.0
    f2 = pool.forecast("800001", "cold drink", ManualWeather({"800001": 42}))
    assert f2["surge"] and f2["multiplier"] == 1.5 and f2["predicted"] == int(round(f2["baseline"] * 1.5))
    assert pool.forecast("800001", "namak", ManualWeather({"800001": 42}))["ok"] is False   # no data


def test_forecast_needs_history(world):
    reg, pool, clk = world
    fill(reg, pool, K_MIN, lambda i: {0: 20})
    assert pool.forecast("800001", "cold drink")["ok"] is False


def test_suggestion_is_capped(world):
    reg, pool, clk = world
    f = {"ok": True, "baseline": 100, "predicted": 150}
    assert pool.suggest_for_shop("x", "cold drink", 0, 10, f) == 15
    assert pool.suggest_for_shop("x", "cold drink", 100, 10, f) == 0
    assert pool.suggest_for_shop("x", "cold drink", 0, 10, {"ok": True, "baseline": 1, "predicted": 100}) == 20


def test_rfq_lifecycle_privacy_and_payment_direct(world):
    reg, pool, clk = world
    shops = fill(reg, pool, K_MIN, lambda i: {0: 20})
    rid = pool.open_rfq("800001", "cold drink", "pc", "2026-05-25", reference_paise=2000)
    for i, t in enumerate(shops):
        pool.join(rid, t.shop_id, 100 + i)
    assert pool.totals(rid) == (sum(100 + i for i in range(K_MIN)), K_MIN)
    outsider = make_shop(reg, 99, pin="800009")
    with pytest.raises(ValueError):
        pool.join(rid, outsider.shop_id, 5)                         # other pincode
    pool.add_quote(rid, "Alpha Dist", 2100)
    pool.add_quote(rid, "Beta Dist", 1950)
    pool.add_quote(rid, "Gamma Dist", 1700)
    ev = pool.evaluate(rid)
    acts = {q["distributor"]: q["action"] for q in ev["quotes"]}
    assert acts["Alpha Dist"] == "reject_above_walk_away" and acts["Gamma Dist"] == "accept" and acts["Beta Dist"] == "counter"
    assert ev["walk_away_paise"] == 1960 and ev["target_paise"] < ev["walk_away_paise"]
    assert "Rs" in pool.draft_counter(rid, "Beta Dist") and pool.draft_counter(rid, "Gamma Dist") == ""
    pool.finalize(rid, "Gamma Dist", 1700)
    po = pool.confirm(rid, shops[0].shop_id)
    assert po["total_paise"] == 100 * 1700 and "seedha" in po["note"]
    with pytest.raises(ValueError):
        pool.confirm(rid, shops[0].shop_id)                          # cannot confirm twice
    with pytest.raises(ValueError):
        pool.confirm(rid, outsider.shop_id)


def test_min_qty_not_met_rejected(world):
    reg, pool, clk = world
    shops = fill(reg, pool, K_MIN, lambda i: {0: 20})
    rid = pool.open_rfq("800001", "cold drink", "pc", "2026-05-25", 2000)
    pool.join(rid, shops[0].shop_id, 10)
    pool.add_quote(rid, "Big Dist", 1500, min_qty=5000)
    assert pool.evaluate(rid)["quotes"][0]["action"] == "reject_min_qty_not_met"


def test_withdraw_removes_from_totals(world):
    reg, pool, clk = world
    shops = fill(reg, pool, K_MIN, lambda i: {0: 20})
    rid = pool.open_rfq("800001", "cold drink", "pc", "2026-05-25", 2000)
    pool.join(rid, shops[0].shop_id, 10)
    pool.withdraw(rid, shops[0].shop_id)
    assert pool.totals(rid) == (0, 0) and pool.evaluate(rid)["ok"] is False
