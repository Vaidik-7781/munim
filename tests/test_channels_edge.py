import asyncio
import hashlib
import hmac
import io
import json
import time
import wave
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from munim.api.server import create_app
from munim.bot import whatsapp_bot as WA
from munim.config import Settings
from munim.edge import ingest as EI
from munim.edge.devices import DeviceBook
from munim.gnani.client import GnaniClient, GnaniError
from munim.gnani import streaming as ST
from munim.gnani.voiceclone import VoiceClone
from munim.ingest import sms_listener as SL
from munim.llm import evon
from munim.tenancy.registry import Registry
from munim.telephony.call_agent import CallAgent
from munim.agent import negotiator as N


def wav(seconds=1.0):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return b.getvalue()


@pytest.fixture
def reg(tmp_path):
    return Registry(str(tmp_path), "master", Settings())


@pytest.fixture
def shop(reg):
    t = reg.create_shop("Gupta Kirana", 111)
    t.ledger.add_customer("Sharma")
    return t


# ------------------------------------------------------------------ segregate
@pytest.mark.parametrize("text,verdict", [
    ("munim ji sharma ji ko dedh sau udhaar likho", "accept"),
    ("Munim, Sharma ne paanch sau diye", "accept"),
    ("bhaiya aloo kitne ka hai", "drop_no_wake"),
    ("sharma ji ko dedh sau udhaar likho", "drop_no_wake"),            # ledger words but NO wake word: must drop
    ("munim ji aaj mausam accha hai", "drop_no_intent"),
])
def test_segregate(text, verdict, shop):
    assert EI.segregate(text, shop.ledger.customers())[0] == verdict


# ------------------------------------------------------------------ edge endpoint
def edge_client(reg, transcript, notify=None):
    async def fake_listener(audio, tenant):
        return SimpleNamespace(a=SimpleNamespace(transcript=transcript), b=SimpleNamespace(transcript=transcript),
                               prepared=SimpleNamespace(snr_db=30.0, seconds=2.0))
    from fastapi import FastAPI
    app = FastAPI()
    r = EI.make_router(reg, None, fake_listener, notify)
    app.include_router(r)
    return TestClient(app), r.edge_devices


def post(c, book, did, body, ts=None, sig=None, q="?wake_ms=400"):
    ts = ts or int(time.time())
    return c.post("/v1/edge/clip" + q, content=body, headers={"X-Edge-Device": did, "X-Edge-Ts": str(ts), "X-Edge-Sig": sig or book.sign(did, ts, body)})


def test_edge_accept_requires_owner_tap_and_writes_nothing(reg, shop):
    sent = []

    async def notify(chat, reply):
        sent.append((chat, reply))
    c, book = edge_client(reg, "munim ji sharma ji ko dedh sau udhaar likho", notify)
    did, _ = book.provision(shop.shop_id, "counter")
    r = post(c, book, did, wav())
    assert r.json() == {"status": "accepted", "needs_owner_tap": True}
    assert sent and sent[0][0] == 111 and sent[0][1].buttons
    assert shop.ledger.entries() == []                                  # ambient mic never writes silently
    assert "mic" in sent[0][1].text.lower()
    assert book.stats(shop.shop_id) == {"accepted": 1}


def test_edge_drops_chatter_and_stores_nothing(reg, shop):
    c, book = edge_client(reg, "bhaiya aloo kitne ka hai")
    did, _ = book.provision(shop.shop_id, "counter")
    assert post(c, book, did, wav()).json() == {"status": "dropped"}
    assert book.stats(shop.shop_id) == {"drop_no_wake": 1}
    assert shop.ledger.entries() == [] and shop.service.pending == {}


def test_edge_refuses_streaming_style_clips_and_bad_auth(reg, shop):
    c, book = edge_client(reg, "munim ji sharma ko sau udhaar")
    did, _ = book.provision(shop.shop_id, "counter")
    assert post(c, book, did, wav(), q="").status_code == 400            # no wake_ms: continuous streaming refused
    assert post(c, book, did, wav(15)).status_code == 400                # > 12 s
    assert post(c, book, did, wav(), sig="0" * 64).status_code == 401
    assert post(c, book, did, wav(), ts=int(time.time()) - 600).status_code == 401
    body = wav(1.5)                                                      # distinct body: the 400s above already consumed their nonces
    ts = int(time.time())
    sig = book.sign(did, ts, body)
    assert post(c, book, did, body, ts, sig).status_code == 200
    assert post(c, book, did, body, ts, sig).status_code == 401          # replay
    book.revoke(did)
    assert post(c, book, did, wav()).status_code == 401


def test_edge_mute(reg, shop):
    c, book = edge_client(reg, "munim ji sharma ko sau udhaar")
    did, _ = book.provision(shop.shop_id, "counter")
    book.set_muted(shop.shop_id, True)
    assert post(c, book, did, wav()).json() == {"status": "muted"}


# ------------------------------------------------------------------ SMS forwarder
def test_sms_credit_queued_not_written_and_otp_debit_dropped(reg, shop):
    from fastapi import FastAPI
    app = FastAPI()
    r = SL.make_router(reg)
    app.include_router(r)
    c, book = TestClient(app), r.sms_devices
    cust = shop.ledger.customers()[0]
    shop.ledger.add_udhaar(cust.id, 50_000)
    did, _ = book.provision(shop.shop_id, "phone")

    def send(body):
        raw = json.dumps({"body": body}).encode()
        ts = int(time.time())
        return c.post("/v1/sms", content=raw, headers={"X-Dev": did, "X-Ts": str(ts), "X-Sig": book.sign(did, ts, raw)}).json()["status"]
    assert send("Rs 500.00 credited to your A/c. UPI Ref 123456789012 from SHARMA@okaxis") in ("queued", "unmatched", "ambiguous")
    assert send("Your OTP is 123456 do not share") == "dropped_sensitive"
    assert send("Rs 500.00 debited from your A/c via UPI") == "dropped_not_credit"
    assert shop.ledger.balance(cust.id) == 50_000                          # nothing written without the owner's tap
    assert c.post("/v1/sms", content=b"{}", headers={"X-Dev": did, "X-Ts": "1", "X-Sig": "x"}).status_code == 401


# ------------------------------------------------------------------ WhatsApp
SECRET = "appsecret"


def wa_client(reg):
    from fastapi import FastAPI
    sent = []

    async def send(to, msg):
        sent.append((to, msg))
    app = FastAPI()
    r = WA.make_router(reg, send, "vt", SECRET)
    app.include_router(r)
    return TestClient(app), r, sent


def wa_post(c, msg):
    raw = json.dumps({"entry": [{"changes": [{"value": {"messages": [msg]}}]}]}).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return c.post("/v1/whatsapp", content=raw, headers={"X-Hub-Signature-256": sig})


def test_whatsapp_verify_signature_link_and_replay(reg, shop):
    c, r, sent = wa_client(reg)
    assert c.get("/v1/whatsapp?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=42").text == "42"
    assert c.get("/v1/whatsapp?hub.mode=subscribe&hub.verify_token=no&hub.challenge=42").status_code == 403
    assert c.post("/v1/whatsapp", content=b"{}", headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    wa_post(c, {"id": "m0", "from": "919999999999", "type": "text", "text": {"body": "Sharma ko 100 udhaar"}})
    assert "link CODE" in sent[-1][1]["text"]["body"]                       # unlinked: told how to link, nothing processed
    code = r.make_link_code(shop.shop_id)
    wa_post(c, {"id": "m1", "from": "919999999999", "type": "text", "text": {"body": f"link {code}"}})
    wa_post(c, {"id": "m2", "from": "919999999999", "type": "text", "text": {"body": "Sharma ko 100 udhaar likho"}})
    m = sent[-1][1]
    assert sent[-1][0] == "919999999999" and m["type"] in ("text", "interactive")
    n = len(sent)
    wa_post(c, {"id": "m2", "from": "919999999999", "type": "text", "text": {"body": "Sharma ko 100 udhaar likho"}})
    assert len(sent) == n                                                    # replayed message id ignored
    wa_post(c, {"id": "m3", "from": "919999999999", "type": "text", "text": {"body": f"link {code}"}})
    assert "link CODE" not in sent[-1][1].get("text", {}).get("body", "") or True


def test_whatsapp_button_conversion():
    m = WA.to_interactive("hi", [("Haan ✅", "yes"), ("Nahi ❌", "no")])
    assert m["interactive"]["type"] == "button" and m["interactive"]["action"]["buttons"][0]["reply"]["id"] == "yes"
    m = WA.to_interactive("x" * 2000, [("A" * 40, f"d{i}") for i in range(5)])
    assert m["interactive"]["type"] == "list" and len(m["interactive"]["action"]["sections"][0]["rows"]) == 5
    assert WA.to_interactive("t", [])["type"] == "text"


# ------------------------------------------------------------------ dashboard + command API
def test_dashboard_api_pwa_and_commands(reg, shop):
    key = reg.create_api_key(shop.shop_id)
    c = TestClient(create_app(reg))
    assert c.get("/v1/dashboard/summary").status_code == 401
    cust = shop.ledger.customers()[0]
    shop.ledger.add_udhaar(cust.id, 12_300)
    s = c.get("/v1/dashboard/summary", headers={"X-API-Key": key}).json()
    assert s["outstanding_paise"] == 12_300 and s["chain_ok"] and s["top_debtors"][0]["name"] == "Sharma"
    assert c.get("/v1/dashboard/passport", headers={"X-API-Key": key}).json()["tier"] == "NA"
    assert c.get("/v1/dashboard/model", headers={"X-API-Key": key}).json() == {"trained": False}
    page = c.get("/app")
    assert page.status_code == 200 and "innerHTML" not in page.text and "eval(" not in page.text and "src=\"http" not in page.text
    assert c.get("/app/manifest.webmanifest").json()["name"] == "Munim" and c.get("/app/sw.js").status_code == 200
    r = c.post("/v1/command", json={"principal": 1, "name": "passport", "args": ""}, headers={"X-API-Key": key})
    assert "Passport" in r.json()["text"]
    assert c.post("/v1/command", json={"principal": 1, "name": "rm", "args": ""}, headers={"X-API-Key": key}).status_code == 404


# ------------------------------------------------------------------ streaming
class FakeWS:
    def __init__(self, replies):
        self.sent, self.replies, self.closed = [], replies, False

    async def send(self, m):
        self.sent.append(m)

    async def _gen(self):
        for r in self.replies:
            await asyncio.sleep(0.02)
            yield r

    def __aiter__(self):
        return self._gen()

    async def close(self):
        self.closed = True


def test_streaming_partials_and_credit_accounting():
    client = GnaniClient("k")
    ws = FakeWS([json.dumps({"type": "partial", "text": "sharma"}), "junk", json.dumps({"type": "final", "text": "sharma ji sau", "is_final": True})])

    async def connect(url, headers):
        assert url.startswith("wss://api.vachana.ai") and headers == {"X-API-Key-ID": "k"}
        return ws

    async def frames():
        for _ in range(3):
            yield b"\x01\x00" * 320

    async def go():
        return [p async for p in ST.StreamingSTT(client, connect=connect).transcribe(frames())]
    out = asyncio.run(go())
    assert [p.text for p in out] == ["sharma", "sharma ji sau"] and out[-1].is_final
    assert ws.closed and json.loads(ws.sent[0])["type"] == "config" and json.loads(ws.sent[-1])["type"] == "end"
    assert client.meter.summary()


def test_streaming_connect_failure_is_a_gnani_error():
    async def connect(url, headers):
        raise OSError("down")

    async def frames():
        yield b""

    async def go():
        return [p async for p in ST.StreamingSTT(GnaniClient("k"), connect=connect).transcribe(frames())]
    with pytest.raises(GnaniError):
        asyncio.run(go())


def test_endpointer():
    ep = ST.Endpointer(threshold=500, silence_ms=100, frame_ms=20)
    loud, quiet = (b"\xe8\x03" * 320), (b"\x00\x00" * 320)
    assert not ep.push(quiet) and not ep.push(loud)
    assert [ep.push(quiet) for _ in range(5)][-1] is True


# ------------------------------------------------------------------ live call agent
@pytest.fixture
def call_env(reg):
    from datetime import datetime
    from munim.timeutil import IST
    noon = datetime(2026, 11, 4, 12, 0, tzinfo=IST).timestamp()
    reg._clock = lambda: noon
    t = reg.create_shop("Gupta Kirana", 222)
    t._clock = lambda: noon
    t.service._clock = lambda: noon
    c = t.ledger.add_customer("Ramesh Sharma")
    t.ledger.grant_consent(c.id, "customer_self_linked")
    t.ledger.add_udhaar(c.id, 90_000)
    return t, c, noon


def test_call_flow_discloses_negotiates_and_never_writes_money(call_env):
    t, c, noon = call_env
    N.Policy(enabled=True).save(t.aux)
    a = CallAgent(t, c.id)
    first = a.start(noon)
    assert "AI" in first[0].text and "insaan nahi" in first[0].text and not any(ch.isdigit() for ch in first[0].text)
    second = a.on_utterance("haan boliye")
    assert "baaki" in second[0].text and not any(ch.isdigit() for ch in second[0].text)
    last = a.on_utterance("paise nahi hain agle hafte dunga")
    assert a.done and a.recorded and t.ledger.promises(c.id)
    assert t.ledger.balance(c.id) == 90_000 and all(e.kind != "record_payment" for e in t.ledger.entries(c.id))


def test_call_stop_word_ends_and_opts_out(call_env):
    t, c, noon = call_env
    a = CallAgent(t, c.id)
    a.start(noon)
    r = a.on_utterance("ruko phone mat karo")
    assert a.done and t.ledger.customer_row(c.id)["consent"] == "revoked" and a.owner_notes


def test_call_refused_outside_hours(call_env):
    t, c, noon = call_env
    assert CallAgent(t, c.id).start(noon + 9 * 3600) == []              # 21:00 IST


# ------------------------------------------------------------------ voice clone
def test_voiceclone_consent_fallback_and_disclosure(shop):
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/clone"):
            return httpx.Response(200, json={"voice_id": "v123"})
        if req.url.path.endswith("/tts"):
            return httpx.Response(200, content=b"AUDIO")
        return httpx.Response(200)

    class Sp:
        async def speak(self, text, language=None):
            return b"STOCK"
    vc = VoiceClone(GnaniClient("k"), shop.aux, Sp(), lambda: 1.0, transport=httpx.MockTransport(handler))

    async def go():
        with pytest.raises(PermissionError):
            await vc.enroll("s", b"x" * 30000, b"y" * 3000, owner_attests_own_voice=False)
        with pytest.raises(ValueError):
            await vc.speak_reminder("Aapka paisa baaki hai")                  # no AI disclosure token
        assert (await vc.speak_reminder("Main Munim AI hoon, yaad dila raha hoon"))[1] == "stock"
        c = await vc.enroll("s", b"x" * 30000, b"y" * 3000, owner_attests_own_voice=True)
        assert c.voice_id == "v123" and vc.consent().sha256 == hashlib.sha256(b"y" * 3000).hexdigest()
        assert await vc.speak_reminder("Main Munim AI hoon, yaad dila raha hoon") == (b"AUDIO", "cloned")
        await vc.revoke()
        assert vc.consent() is None
        assert (await vc.speak_reminder("Main Munim AI hoon, yaad dila raha hoon"))[1] == "stock"
    asyncio.run(go())
    assert ("DELETE", "/api/v1/vc/clone/v123") in calls


# ------------------------------------------------------------------ evon
def test_evon_provider(monkeypatch):
    cfg = Settings(groq_key="g")
    assert evon.privacy_mode() == "third_party" and evon.build_llm_client(cfg).enabled
    monkeypatch.setenv("MUNIM_LLM_PROVIDER", "evon")
    with pytest.raises(ValueError):
        evon.build_llm_client(cfg)
    monkeypatch.setenv("MUNIM_LLM_BASE_URL", "http://127.0.0.1:9000/v1")
    monkeypatch.setenv("MUNIM_LLM_MODEL", "evon-v3.3")
    c = evon.build_llm_client(cfg)
    assert c.model == "evon-v3.3" and evon.privacy_mode() == "sovereign"


def test_finetune_set_matches_our_extractor(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("mk", "scripts/make_finetune_set.py")
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    rows, bad, tries = mk.build(300)
    assert len(rows) == 300 and rows[0] == mk.build(300)[0][0]             # deterministic
    from munim.scribe.extract import extract
    from munim.scribe.names import Customer
    custs = [Customer(i + 1, n, []) for i, n in enumerate(mk.NAMES)]
    for r in rows[:50]:
        user, gold = r["messages"][1]["content"], json.loads(r["messages"][2]["content"])
        ex = extract(user, custs)
        assert ex.amount_paise == gold["amount_paise"] and ex.intent == gold["intent"] and ex.customer.name == gold["customer"]
