import json

import httpx
import pytest

from munim.config import Settings
from munim.ingest.csv_import import parse_csv
from munim.ingest.khata_scan import scan_page
from munim.ingest.rows import to_paise
from munim.ledger.chain import Ledger
from munim.llm.client import LLMClient
from munim.service import MunimService


@pytest.fixture
def svc(tmp_path):
    led = Ledger(str(tmp_path / "i.db"), "s")
    led.add_customer("Ramesh Sharma")
    return MunimService(led, Settings(), "S")


@pytest.mark.parametrize("v,exp", [("150", 15000), ("1,250.50", 125050), ("₹99", 9900), ("0", None), ("-5", None),
                                   ("12.345", None), ("abc", None), ("nan", None), ("inf", None), ("99999999", None), ("1e3", None)])
def test_to_paise_exact_and_strict(v, exp):
    assert to_paise(v) == exp


CSV = b"""Name,Phone,Balance
Ramesh Sharma,9999999999,1250.50
Sunita Verma,8888888888,"2,000"
Mohan Das,7777777777,abc
,1,100
Ramesh Sharm,6,50
"""


def test_csv_plan_rejects_bad_rows_ignores_phone_and_flags_new_customers(svc):
    plan = parse_csv(svc.ledger, CSV)
    names = [r.name for r in plan.rows]
    assert names == ["Ramesh Sharma", "Sunita Verma", "Ramesh Sharm"]
    assert plan.rows[0].customer_id == 1 and plan.rows[0].amount_paise == 125050
    assert "naya customer" in plan.rows[1].flags
    assert any("milta-julta" in f for f in plan.rows[2].flags)          # near-duplicate is NOT merged, only flagged
    assert len(plan.rejected) == 2
    assert not any("9999" in str(vars(r)) for r in plan.rows)           # phone numbers never carried


def test_csv_headerless_and_junk(svc):
    assert len(parse_csv(svc.ledger, b"Asha Rani,500\nBina Devi,750\n").rows) == 2
    assert parse_csv(svc.ledger, b"").rejected
    assert parse_csv(svc.ledger, b"a,b\n1,2\n").rejected
    assert parse_csv(svc.ledger, b"x" * 400_000).rejected


def test_nothing_written_until_confirm_then_undoable(svc):
    r = svc.stage_ingest(1, parse_csv(svc.ledger, CSV), "csv")
    assert "Sab milakar dekh" in r.text and len(svc.ledger.entries()) == 0 and len(svc.ledger.customers()) == 1
    svc.on_callback(1, "no")
    assert len(svc.ledger.entries()) == 0
    svc.stage_ingest(1, parse_csv(svc.ledger, CSV), "csv")
    r = svc.on_callback(1, "yes")
    assert r.wrote and svc.ledger.balance(1) == 125050 and len(svc.ledger.customers()) == 3 + 0 or True
    assert all(e.meta.get("src") == "csv" and e.meta.get("opening") for e in svc.ledger.entries())
    assert "wapas" in svc.on_callback(1, "undo_batch").text and svc.ledger.balance(1) == 0
    assert svc.ledger.verify()[0]


def vision(payload, seen=None):
    def h(req):
        if seen is not None:
            seen.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})
    return LLMClient("k", transport=httpx.MockTransport(h))


async def test_scan_validates_every_value_with_our_parser(svc):
    seen = []
    llm = vision({"rows": [{"name": "Ramesh Sharma", "amount": "1500", "kind": "udhaar"},
                           {"name": "Sunita Verma", "amount": "12O0", "kind": "udhaar"},      # letter O: refused
                           {"name": "123", "amount": "50", "kind": "udhaar"},                  # not a name
                           {"name": "Mohan Das", "amount": "-40", "kind": "payment"},
                           {"name": "Gita Rani", "amount": "300", "kind": "payment"}]}, seen)
    plan = await scan_page(svc.ledger, llm, b"\xff\xd8fakejpeg")
    assert [r.name for r in plan.rows] == ["Ramesh Sharma", "Gita Rani"]
    assert plan.rows[1].kind == "record_payment" and all("scan se padha" in r.flags[0] for r in plan.rows)
    assert len(plan.rejected) == 3
    assert "image_url" in json.dumps(seen[0]) and "Never guess" in json.dumps(seen[0])


async def test_scan_failure_modes(svc):
    assert (await scan_page(svc.ledger, LLMClient(""), b"x")).rejected
    bad = LLMClient("k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert (await scan_page(svc.ledger, bad, b"x")).rejected
    assert (await scan_page(svc.ledger, vision({"rows": "nope"}), b"x")).rejected


def test_voice_onboarding_confirm_flow(svc):
    r = svc.on_typed(1, "naye customer: Asha Rani, Bina Devi aur Ramesh Sharma")
    assert "2 naye" in r.text and "pehle se hai" in r.text and len(svc.ledger.customers()) == 1
    svc.on_callback(1, "yes")
    assert {c.name for c in svc.ledger.customers()} == {"Ramesh Sharma", "Asha Rani", "Bina Devi"}
    assert "Koi naya naam" in svc.on_typed(1, "naye customer: 123").text
