import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from munim.api.server import create_app
from munim.config import Settings
from munim.ledger.chain import Ledger, LedgerError
from munim.ops import export as ex
from munim.ops.metrics import render
from munim.tenancy.registry import Registry


@pytest.fixture
def reg(tmp_path):
    return Registry(str(tmp_path), "m", Settings())


def filled(led):
    a = led.add_customer("Ramesh Sharma", lang="en")
    led.add_udhaar(a.id, 50000, src="voice")
    led.record_payment(a.id, 20000, src="upi", upi="abc")
    led.undo_last()
    led.grant_consent(a.id)
    return a


def test_export_verify_restore_roundtrip(reg, tmp_path):
    t = reg.create_shop("Alpha Store", 1)
    filled(t.ledger)
    data = ex.export_zip(t.ledger, "Alpha Store", 123.0)
    ok, msg = ex.verify_export(data)
    assert ok, msg
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "secret" not in z.read("manifest.json").decode().lower()
        assert b"Ramesh" not in z.read("ledger.jsonl")                  # chain file has pseudonyms only
    fresh = Ledger(str(tmp_path / "restored.db"), reg._secret(t.shop_id))
    assert ex.restore(fresh, data).startswith("ok")
    assert fresh.balance(1) == t.ledger.balance(1) and fresh.customers()[0].name == "Ramesh Sharma"
    assert fresh.customer_row(1)["consent"] == "granted"
    with pytest.raises(LedgerError):
        ex.restore(fresh, data)                                          # non-empty ledger refused


def _tamper(data, mutate):
    zin = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for n in zin.namelist():
            b = zin.read(n)
            z.writestr(n, mutate(n, b))
    return out.getvalue()


def test_tampered_or_truncated_backup_refused(reg, tmp_path):
    t = reg.create_shop("Alpha Store", 1)
    filled(t.ledger)
    data = ex.export_zip(t.ledger, "A", 1.0)

    def edit(n, b):
        return b.replace(b'"amount_paise": 50000', b'"amount_paise": 1') if n == "ledger.jsonl" else b
    ok, msg = ex.verify_export(_tamper(data, edit))
    assert not ok and "edited" in msg

    def drop_last(n, b):
        return b"\n".join(b.split(b"\n")[:-1]) if n == "ledger.jsonl" else b
    ok, msg = ex.verify_export(_tamper(data, drop_last))
    assert not ok and "head hash" in msg
    assert not ex.verify_export(b"not a zip")[0]
    with pytest.raises(LedgerError):
        ex.restore(Ledger(str(tmp_path / "x.db"), "s"), _tamper(data, edit))


def test_metrics_have_no_pii(reg):
    t = reg.create_shop("Alpha Store", 1)
    reg.quota.charge(t.shop_id, 5)
    m = render(reg)
    assert "munim_shops 1" in m and "munim_gnani_calls_today 5" in m
    assert t.shop_id not in m and "Alpha" not in m


def test_api_auth_rate_limit_and_safety(reg):
    t = reg.create_shop("Alpha Store", 1)
    t.ledger.add_customer("Ramesh Sharma")
    key = reg.create_api_key(t.shop_id)
    c = TestClient(create_app(reg, rate_per_min=5))
    assert c.get("/healthz").json()["ok"]
    assert c.post("/v1/message", json={"principal": 1, "text": "hi"}).status_code == 401
    assert c.post("/v1/message", headers={"X-API-Key": "mk_wrong"}, json={"principal": 1, "text": "hi"}).status_code == 401
    h = {"X-API-Key": key}
    r = c.post("/v1/message", headers=h, json={"principal": 1, "text": "Ramesh ko 150 udhaar likh do"})
    assert r.status_code == 200 and r.json()["text"]
    assert c.post("/v1/message", headers=h, json={"principal": 1, "text": ""}).status_code == 422
    assert c.get("/v1/verify", headers=h).json()["ok"]
    # same audit gate as Telegram: typed text -> soft write allowed, but a batch still needs a confirm
    r = c.post("/v1/message", headers=h, json={"principal": 2, "text": "Ramesh ne 100 diye, Ramesh ne 50 diye aur Ramesh ne 70 diye"})
    assert r.status_code == 200
    codes = [c.post("/v1/message", headers=h, json={"principal": 1, "text": "x"}).status_code for _ in range(8)]
    assert 429 in codes                                                  # rate limit bites
    reg.revoke_api_keys(t.shop_id)
    assert TestClient(create_app(reg)).post("/v1/message", headers=h, json={"principal": 1, "text": "hi"}).status_code == 401


def test_api_keys_are_per_shop_and_hashed(reg):
    a, b = reg.create_shop("Alpha Store", 1), reg.create_shop("Beta Store", 2)
    ka, kb = reg.create_api_key(a.shop_id), reg.create_api_key(b.shop_id)
    assert reg.shop_for_api_key(ka) == a.shop_id and reg.shop_for_api_key(kb) == b.shop_id
    stored = [r["key_hash"] for r in reg._db.execute("SELECT key_hash FROM api_keys").fetchall()]
    assert ka not in stored and kb not in stored
    assert not any(isinstance(x, str) and x.startswith("mk_") for x in stored)


def test_metrics_endpoint_needs_admin_token(reg, monkeypatch):
    c = TestClient(create_app(reg))
    monkeypatch.delenv("MUNIM_ADMIN_TOKEN", raising=False)
    assert c.get("/metrics").status_code == 401
    monkeypatch.setenv("MUNIM_ADMIN_TOKEN", "adm")
    assert c.get("/metrics", headers={"X-Admin-Token": "bad"}).status_code == 401
    assert "munim_shops" in c.get("/metrics", headers={"X-Admin-Token": "adm"}).text
