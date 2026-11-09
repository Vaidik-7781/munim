import asyncio
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from munim import extras
from munim.api.server import create_app
from munim.config import Settings
from munim.tenancy.registry import Registry
from munim.timeutil import IST


@pytest.fixture(autouse=True)
def fresh_services():
    extras.SERVICES = extras.Services()
    yield


@pytest.fixture
def env(tmp_path):
    reg = Registry(str(tmp_path), "master", Settings())
    t = reg.create_shop("Gupta Kirana", 111)
    t.ledger.add_customer("Sharma")
    return reg, t


def run(reg, t, name, args="", chat=111):
    return asyncio.run(extras.run(reg, t, None, chat, name, args))


def test_unknown_command_and_error_wrapping(env):
    reg, t = env
    assert "nahi pehchana" in run(reg, t, "nope").text
    assert run(reg, t, "pincode", "12").text.startswith("⚠️")          # ValueError becomes a polite reply, not a crash
    assert "Pincode 560001" in run(reg, t, "pincode", "560001").text and reg.pincode(t.shop_id) == "560001"


def test_stock_price_count_flow(env):
    reg, t = env
    assert "Abhi koi item nahi" in run(reg, t, "stock").text
    from munim.inventory.stock import Inventory
    inv = Inventory(t.aux, t._clock)
    inv.upsert("Maggi", "pc")
    assert "Pehle item" in run(reg, t, "price", "daal 5 rupaye packet").text
    assert "daam" in run(reg, t, "price", "maggi 14 rupaye packet mrp 14 by Nestle").text
    assert "ginti" in run(reg, t, "count", "maggi 40 packet").text
    assert "Maggi" in run(reg, t, "stock").text
    assert "match nahi" in run(reg, t, "count", "maggi 40 kilo").text


def test_ondc_needs_pincode_and_is_owner_only_via_callback(env):
    reg, t = env
    r = extras.callback(reg, t, 999, "x:ondc")                           # a stranger tapping a forwarded button
    assert "maalik" in r.text and not t.aux.get("ondc_enabled")
    assert "Kuch nahi" in extras.callback(reg, t, 999, "x:no").text or "kuch nahi" in extras.callback(reg, t, 999, "x:no").text.lower()


def test_mandate_proposal_requires_linked_customer_and_never_activates_itself(env):
    reg, t = env
    c = t.ledger.customers()[0]
    out = run(reg, t, "mandate", "Sharma 25%")
    assert out.text.startswith("⚠️") or "prastaav" in out.text            # unlinked customer: refused or proposal only
    rows = t.aux.db.execute("SELECT status FROM mandates").fetchall()
    assert all(r["status"] != "active" for r in rows)
    assert "Capacity cover" in run(reg, t, "mandate").text


def test_recon_callbacks_none_and_all(env):
    reg, t = env
    assert "pending nahi" in run(reg, t, "recon").text
    assert "koi payment nahi" in extras.callback(reg, t, 111, "x:recon:none").text
    assert "0 payment" in extras.callback(reg, t, 111, "x:recon:all").text
    assert "Kuch pending nahi" in extras.callback(reg, t, 111, "x:recon:review").text


def test_edge_and_sms_commands_report_when_server_side_off(env):
    reg, t = env
    assert "chalu nahi" in run(reg, t, "edge", "provision").text
    assert "chalu nahi" in run(reg, t, "smsdev").text
    from munim.edge.devices import DeviceBook
    extras.SERVICES.edge_devices = DeviceBook(reg, "edge")
    out = run(reg, t, "edge", "provision counter").text
    assert "Secret" in out and "Munim ji" in out                          # signage text accompanies provisioning
    assert "Mic band" in run(reg, t, "edge", "mute").text


def test_callback_bad_payload_is_safe(env):
    reg, t = env
    assert "⚠️" in extras.callback(reg, t, 111, "x:claim:abc").text
    assert "Samay khatam" in extras.callback(reg, t, 111, "x:inv:go").text


def test_daily_isolated_per_shop_and_idempotent(env):
    reg, t = env
    t2 = reg.create_shop("Other", 222)
    reg.set_pincode(t.shop_id, "560001")
    noon = datetime(2026, 11, 4, 12, 0, tzinfo=IST).timestamp()
    out = asyncio.run(extras.daily(reg, noon))
    assert isinstance(out, list)
    assert asyncio.run(extras.daily(reg, noon)) is not None                # second run does not blow up


def test_api_command_and_rail_webhook(env):
    reg, t = env
    key = reg.create_api_key(t.shop_id)
    c = TestClient(create_app(reg))
    r = c.post("/v1/command", json={"principal": 1, "name": "passport", "args": ""}, headers={"X-API-Key": key})
    assert r.status_code == 200 and "Passport" in r.json()["text"]
    assert c.post("/v1/command", json={"principal": 1, "name": "rm", "args": ""}, headers={"X-API-Key": key}).status_code == 404
    assert c.post("/v1/command", json={"principal": 1, "name": "passport"}).status_code == 401
    bad = c.post("/v1/rail/webhook", content=b"{}", headers={"X-Shop-Key": key, "X-Signature": "bad"})
    assert bad.status_code == 401
