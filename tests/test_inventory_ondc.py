import json

import pytest

from munim.config import Settings
from munim.inventory import distributor_ocr as ocr
from munim.inventory.stock import Inventory, fmt_qty
from munim.inventory.voice import parse, parse_price
from munim.ondc import gateway as gw
from munim.ondc.beckn import build_catalog
from munim.tenancy.registry import Registry

NOW = 1_800_000_000.0


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clk = Clock()
    reg = Registry(str(tmp_path), "m", Settings(), clock=clk)
    t = reg.create_shop("Gupta Kirana", 1)
    reg.set_pincode(t.shop_id, "800001")
    return reg, t, clk


@pytest.mark.parametrize("text,kind,qty,dim,item", [
    ("2 kilo Aashirvaad Atta sold", "sale", 2000, "g", "Aashirvaad Atta"),
    ("stocked 50 packets of Maggi", "restock", 50, "pc", "of Maggi"),
    ("paanch sau gram namak becha", "sale", 500, "g", "namak"),
    ("aadha kilo cheeni bika", "sale", 500, "g", "cheeni"),
    ("1 litre tel becha", "sale", 1000, "ml", "tel"),
])
def test_parse(text, kind, qty, dim, item):
    c = parse(text)
    assert (c.kind, c.qty, c.dim, c.item) == (kind, qty, dim, item)


def test_money_lines_are_not_stock():
    assert parse("Sharma ji ko dedh sau udhaar likh do") is None
    assert parse("Verma ne paanch sau diye") is None


def test_price_parse():
    assert parse_price("maggi 14 rupaye packet") == ("maggi", 1400, 1)
    assert parse_price("atta 55 per kilo") == ("atta", 5500, 1000)


def test_voice_flow_through_service(env):
    reg, t, clk = env
    svc = t.service
    r = svc.on_typed(1, "stocked 50 packets of Maggi")               # unknown sku -> confirm
    assert "Naya item" in r.text
    r = svc.on_callback(1, "yes")
    inv = Inventory(t.aux, clk)
    s = inv.find("maggi").sku
    assert s.stock == 50
    r = svc.on_typed(1, "10 packet maggi sold")
    assert "40 pc" in r.text and r.buttons[0][1].startswith("stockundo:")
    assert inv.get(s.id).stock == 40
    svc.on_callback(1, r.buttons[0][1])
    assert inv.get(s.id).stock == 50                                  # undo = reversal event
    assert t.ledger.entries() == []                                   # stock never touches the money chain


def test_sale_of_unknown_item_does_not_create_it(env):
    reg, t, clk = env
    r = t.service.on_typed(1, "2 kilo atta sold")
    assert "stock list mein nahi" in r.text and Inventory(t.aux, clk).skus() == []


def test_unit_mismatch_asks(env):
    reg, t, clk = env
    inv = Inventory(t.aux, clk)
    inv.upsert("maggi", "pc")
    r = t.service.on_typed(1, "2 kilo maggi sold")
    assert "pc mein gina" in r.text


def test_catalog_hides_unsafe_items(env):
    reg, t, clk = env
    inv = Inventory(t.aux, clk)
    ok = inv.upsert("Maggi", "pc", price_paise=1400, mrp_paise=1400, manufacturer="Nestle India")
    inv.restock(ok.id, 100)
    nopr = inv.upsert("Atta", "g", price_per=1000)
    inv.restock(nopr.id, 10_000)
    cat = build_catalog(t.shop_id, t.name, "800001", inv, buffer_pct=20)
    items = cat.payload["message"]["catalog"]["bpp/providers"][0]["items"]
    assert [i["descriptor"]["name"] for i in items] == ["Maggi"]
    assert items[0]["quantity"]["available"]["count"] == "80"       # 100 less 20 % buffer
    assert items[0]["price"]["value"] == "14.00"
    assert {s.name: w for s, w in cat.hidden}["Atta"] >= ["no_price", "no_mrp"]


def test_stale_count_is_hidden_then_fresh_again(env):
    reg, t, clk = env
    inv = Inventory(t.aux, clk)
    s = inv.upsert("Maggi", "pc", price_paise=1400, mrp_paise=1400, manufacturer="Nestle")
    inv.restock(s.id, 100)
    clk.t += 20 * 86400
    assert build_catalog(t.shop_id, t.name, "800001", inv).published == []
    inv.count(s.id, 90)
    assert build_catalog(t.shop_id, t.name, "800001", inv).published == [s.id]


def test_price_above_mrp_blocked(env):
    reg, t, clk = env
    inv = Inventory(t.aux, clk)
    s = inv.upsert("Maggi", "pc", price_paise=1600, mrp_paise=1400, manufacturer="Nestle")
    inv.restock(s.id, 100)
    assert "price_above_mrp" in dict((x.name, w) for x, w in build_catalog("s", "n", "800001", inv).hidden)["Maggi"]


def test_sync_is_opt_in_incremental_and_resilient(env):
    reg, t, clk = env
    inv = Inventory(t.aux, clk)
    s = inv.upsert("Maggi", "pc", price_paise=1400, mrp_paise=1400, manufacturer="Nestle")
    inv.restock(s.id, 100)
    sink = gw.SandboxSink()
    assert gw.sync(t, "800001", sink)["status"] == "disabled"
    assert sink.sent == []                                            # nothing leaves before the owner opts in
    gw.enable(t)
    assert gw.sync(t, None, sink)["status"] == "needs_pincode"
    assert gw.sync(t, "800001", sink)["status"] == "pushed"
    assert gw.sync(t, "800001", sink)["status"] == "unchanged"
    inv.sale(s.id, 30)
    r = gw.sync(t, "800001", sink)
    assert r["changed"] == 1 and sink.sent[-1]["message"]["catalog"]["bpp/providers"][0]["items"][0]["quantity"]["available"]["count"] == "56"
    inv.count(s.id, 0)                                                # sold out -> pushed as count 0, not silently stale
    r = gw.sync(t, "800001", sink)
    assert r["removed"] == 1 and sink.sent[-1]["message"]["catalog"]["bpp/providers"][0]["items"][0]["quantity"]["available"]["count"] == "0"

    class Boom:
        def push(self, p):
            raise RuntimeError("net down")
    inv.restock(s.id, 100)
    assert gw.sync(t, "800001", Boom())["status"] == "queued"        # failure queued; ledger untouched
    assert t.aux.db.execute("SELECT COUNT(*) FROM ondc_outbox WHERE status='pending'").fetchone()[0] == 1


def test_network_order_becomes_sale_event(env):
    reg, t, clk = env
    inv = Inventory(t.aux, clk)
    s = inv.upsert("Atta", "g", price_paise=5500, price_per=1000, mrp_paise=5800, manufacturer="X")
    inv.restock(s.id, 10_000)
    assert gw.apply_order(t, [(f"{t.shop_id}-{s.id}", 2), ("bad", 1)]) == ["bad"]
    assert inv.get(s.id).stock == 8000


def test_velocity_and_cover(env):
    reg, t, clk = env
    inv = Inventory(t.aux, clk)
    s = inv.upsert("Maggi", "pc")
    inv.restock(s.id, 100)
    for _ in range(14):
        inv.sale(s.id, 10)
        clk.t += 86400
    s = inv.get(s.id)
    assert s.stock == -40
    assert inv.sold_units(s.id, 28) == 140


GOOD = {"supplier": "Agarwal Traders", "invoice_no": "A-17", "invoice_date": "2026-11-01", "payment_terms_days": 15,
        "total": "1,500.00", "tax_total": None,
        "lines": [{"desc": "Maggi", "qty": "100", "unit": "pc", "rate": "14.00", "amount": "1400.00"},
                  {"desc": "Misc", "qty": "1", "unit": "pc", "rate": "100.00", "amount": "100.00"}]}


def test_invoice_validation_and_commit(env):
    reg, t, clk = env
    inv = Inventory(t.aux, clk)
    s = inv.upsert("Maggi", "pc")
    iv = ocr.validate(GOOD, inv)
    assert iv.ok, iv.problems
    assert iv.lines[0].sku_id == s.id and iv.lines[0].base_qty == 100
    out = ocr.commit(t, iv)
    assert out["stocked_lines"] == 1 and out["due"] == "2026-11-16" and inv.get(s.id).stock == 100
    assert t.payables.balance(t.payables.customers()[0].id) == 150000
    assert ocr.commit(t, iv)["duplicate"] is True                    # same invoice twice -> no double payable


def test_invoice_arithmetic_errors_are_surfaced_not_fixed():
    bad = json.loads(json.dumps(GOOD))
    bad["lines"][0]["amount"] = "1900.00"
    iv = ocr.validate(bad)
    assert not iv.ok and "qty_x_rate_mismatch" in iv.lines[0].problems
    bad2 = json.loads(json.dumps(GOOD))
    bad2["total"] = "9999"
    assert "lines_do_not_sum_to_total" in ocr.validate(bad2).problems
    bad3 = json.loads(json.dumps(GOOD))
    bad3["lines"][0]["rate"] = "fourteen"
    assert "bad_rate" in ocr.validate(bad3).lines[0].problems
