import asyncio
import io
import subprocess
import wave

import httpx
import numpy as np
import pytest

from munim.config import Settings
from munim.gnani.client import GnaniClient
from munim.gnani.tts import Speaker
from munim.ledger.chain import Ledger
from munim.listen.listener import AudioTooLong, listen, prepare
from munim.listen.snr import estimate_snr_db
from munim.service import MunimService


def synth_wav(seconds=3.0, sr=16000, noise=0.002, speech_amp=0.3, seed=1) -> bytes:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * sr)) / sr
    gate = (np.sin(2 * np.pi * 2 * t) > -0.2).astype(float)          # on/off like syllables
    x = speech_amp * gate * np.sin(2 * np.pi * 220 * t) + noise * rng.standard_normal(len(t))
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr), w.writeframes(pcm)
    return buf.getvalue()


def to_ogg(wav: bytes) -> bytes:
    p = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-f", "ogg", "pipe:1"],
                       input=wav, capture_output=True, check=True)
    return p.stdout


def fake_gnani(a_text, b_text, fail_b=False):
    def handler(req: httpx.Request):
        if req.url.path == "/stt/v3":
            if b"prisma-v3.0" in req.content:
                return httpx.Response(200, json={"transcript": a_text, "detected_language_code": "hi-IN",
                                                 "request_id": "A1"})
            if fail_b:
                return httpx.Response(401)
            return httpx.Response(200, json={"output": {"literal": b_text}, "request_id": "B1"})
        return httpx.Response(200, content=b"OggS-fake")
    return handler


async def _nosleep(_):
    return None


def client(handler):
    return GnaniClient("k", transport=httpx.MockTransport(handler), sleep=_nosleep)


def test_snr_clean_vs_noisy():
    from munim.listen.snr import read_wav

    clean = estimate_snr_db(*read_wav(synth_wav(noise=0.001)))
    noisy = estimate_snr_db(*read_wav(synth_wav(noise=0.15)))
    assert clean > 25 and noisy < 10 and clean > noisy + 15


async def test_prepare_decodes_ogg_opus():
    ogg = to_ogg(synth_wav(2.5))
    p = await prepare(ogg)
    assert 2.3 < p.seconds < 2.7 and p.wav[:4] == b"RIFF"


async def test_too_long_rejected():
    with pytest.raises(AudioTooLong):
        await prepare(to_ogg(synth_wav(31.0)))


async def test_end_to_end_voice_to_ledger(tmp_path):
    led = Ledger(str(tmp_path / "e.db"), "k")
    sharma = led.add_customer("Ramesh Sharma")
    svc = MunimService(led, Settings(), "Gupta Kirana")
    c = client(fake_gnani("Sharma ji ko dedh sau udhaar likh do", "Sharma ji ko dedh sau udhaar likh do"))
    heard = await listen(c, to_ogg(synth_wav(3.0)), led.customers())
    assert heard.a.language == "hi-IN" and heard.b is not None
    r = svc.on_heard(1, heard.a.transcript, heard.b.transcript, snr=heard.prepared.snr_db,
                     dur=heard.prepared.seconds, req_ids=(heard.a.request_id, heard.b.request_id))
    assert r.wrote and led.balance(sharma.id) == 15000
    meta = led.entries(sharma.id)[0].meta
    assert meta["req"] == ["A1", "B1"] and "Sharma" not in str(meta)
    assert c.meter.summary()["by_kind"] == {"stt": 2}
    # keyterms sent to both passes carry shop vocabulary
    assert led.verify()[0]


async def test_pass_b_failure_with_auth_propagates(tmp_path):
    from munim.gnani.client import GnaniAuthError

    c = client(fake_gnani("x", "y", fail_b=True))
    with pytest.raises(GnaniAuthError):
        await listen(c, to_ogg(synth_wav(2.0)), [])


async def test_pass_b_transient_failure_degrades_to_confirm(tmp_path):
    def handler(req):
        if b"prisma-v3.0" in req.content:
            return httpx.Response(200, json={"transcript": "Sharma ji ko 150 udhaar", "request_id": "A"})
        return httpx.Response(504)

    led = Ledger(str(tmp_path / "d.db"), "k")
    led.add_customer("Ramesh Sharma")
    svc = MunimService(led, Settings(), "S")
    heard = await listen(client(handler), to_ogg(synth_wav(2.0)), led.customers())
    assert heard.b is None and heard.b_error == "GnaniUnavailable"
    r = svc.on_heard(1, heard.a.transcript, None, snr=30.0, dur=2.0)
    assert not r.wrote and "dobara jaanch" in r.text


async def test_speaker_rules():
    c = client(lambda r: httpx.Response(200, content=b"OggS-fake"))
    s = Speaker(c)
    assert await s.speak("Likh diya. ek sau pachaas rupaye") == b"OggS-fake"
    assert await s.speak("Likh diya. ek sau pachaas rupaye") == b"OggS-fake"
    assert c.meter.summary()["calls"] == 1                      # cache hit costs nothing
    assert await s.speak("Likh diya 150 rupaye") is None         # digits refused
    with pytest.raises(ValueError):
        Speaker(c, voice="Anushka")                              # not a Timbre v3.0 voice


async def test_speaker_degrades_on_error():
    c = client(lambda r: httpx.Response(402))
    assert await Speaker(c).speak("namaste") is None


def test_bot_builds_offline(tmp_path):
    from munim.bot.telegram_bot import build_app

    cfg = Settings(telegram_token="123456:ABC-fake")
    led = Ledger(str(tmp_path / "b.db"), "k")
    app = build_app(cfg, led, client(lambda r: httpx.Response(200)))
    assert len(app.handlers[0]) >= 8


# ---------------- Gnani escalation / language retry
def counting_gnani(fn):
    seen = []

    def handler(req: httpx.Request):
        body = req.content
        seen.append(body)
        return fn(body)

    return handler, seen


async def test_third_ear_only_on_amount_disagreement_and_ranks_options(tmp_path):
    def fn(body: bytes):
        if b"codemix" in body:
            return httpx.Response(200, json={"transcript": "Sharma ji ko dedh sau udhaar"})
        if b"prisma-v3.0" in body:
            return httpx.Response(200, json={"transcript": "Sharma ji ko dedh sau udhaar", "detected_language_code": "hi-IN"})
        return httpx.Response(200, json={"output": {"literal": "Sharma ji ko dedh hazaar udhaar"}})

    handler, seen = counting_gnani(fn)
    led = Ledger(str(tmp_path / "c.db"), "k")
    sharma = led.add_customer("Ramesh Sharma")
    svc = MunimService(led, Settings(), "S")
    heard = await listen(client(handler), to_ogg(synth_wav(3.0)), led.customers())
    assert heard.c is not None and heard.calls == 3 and len(seen) == 3
    r = svc.on_heard(1, heard.a.transcript, heard.b.transcript, snr=30, dur=3, c_text=heard.c.transcript)
    assert not r.wrote and "Teesri baar" in r.text and "⭐" in r.text
    assert r.buttons[0][0].startswith("⭐ ₹150")            # recommended option listed first
    assert led.balance(sharma.id) == 0                      # ranking never auto-writes


async def test_no_third_ear_when_ears_agree(tmp_path):
    def fn(body: bytes):
        if b"prisma-v3.0" in body:
            return httpx.Response(200, json={"transcript": "ek sau pachaas", "detected_language_code": "hi-IN"})
        return httpx.Response(200, json={"output": {"literal": "dedh sau"}})

    handler, seen = counting_gnani(fn)
    heard = await listen(client(handler), to_ogg(synth_wav(2.0)), [])
    assert heard.c is None and len(seen) == 2             # 150 == 150 via different wording -> no escalation


async def test_language_retry_in_detected_language():
    langs = []

    def fn(body: bytes):
        if b"prisma-v3.0" in body:
            langs.append(b"ta-IN" in body)
            return httpx.Response(200, json={"transcript": "x", "detected_language_code": "ta-IN"})
        return httpx.Response(200, json={"output": {"literal": "x"}})

    heard = await listen(client(lambda r: fn(r.content)), to_ogg(synth_wav(2.0)), [])
    assert langs == [False, True] and heard.retried_lang == "ta-IN" and heard.calls == 3
