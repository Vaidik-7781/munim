import io
import wave

import numpy as np
import pytest

from munim.config import Settings
from munim.ledger.chain import Ledger
from munim.listen.chunker import split_wav
from munim.scribe.batch import is_batch, segments
from munim.scribe.upi import parse_upi
from munim.service import MunimService


def wav_of(x, sr=16000):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    return b.getvalue()


@pytest.fixture
def svc(tmp_path):
    t = [1_700_000_000.0]
    led = Ledger(str(tmp_path / "b.db"), "s", clock=lambda: t[0]) if "clock" in Ledger.__init__.__code__.co_varnames else Ledger(str(tmp_path / "b.db"), "s")
    for n in ("Ramesh Sharma", "Sunita Verma", "Mohan Das"):
        led.add_customer(n)
    return MunimService(led, Settings())


# ---- chunker
def test_split_wav_cuts_at_silence_and_respects_max():
    sr = 16000
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.2, sr * 70).astype(np.float32)
    for k in (27, 55):                                   # quiet gaps just before the 28 s / 56 s limits
        x[int(k * sr): int((k + 0.3) * sr)] = 0.0
    pieces = split_wav(wav_of(x))
    assert len(pieces) == 3
    for p in pieces:
        with wave.open(io.BytesIO(p)) as w:
            assert w.getnframes() / sr <= 28.01
    with wave.open(io.BytesIO(pieces[0])) as w:
        assert 27.0 <= w.getnframes() / sr <= 27.4       # cut landed inside the silence, not at the hard limit
    total = sum(wave.open(io.BytesIO(p)).getnframes() for p in pieces)
    assert total == len(x)                               # nothing lost or duplicated


def test_short_audio_not_split():
    x = np.zeros(16000 * 5, dtype=np.float32)
    assert len(split_wav(wav_of(x))) == 1


# ---- batch segmentation
def cust():
    from munim.scribe.names import Customer
    return [Customer(1, "Ramesh Sharma"), Customer(2, "Sunita Verma"), Customer(3, "Mohan Das")]


def test_batch_detects_three_entries():
    s = segments("Ramesh ko sau udhaar, Sunita ne paanch sau diye, Mohan ko dedh sau udhaar likh do", cust())
    assert is_batch(s) and len(s) == 3


def test_single_entry_is_not_batch():
    assert not is_batch(segments("Ramesh ko dedh sau udhaar likh do", cust()))
    assert not is_batch(segments("Ramesh ji ko 100 aur 50 udhaar likh do", cust()))   # same customer: old multi_amount path


def test_intent_never_guessed_when_lines_disagree():
    s = segments("Ramesh ko sau, Sunita ne paanch sau diye, Mohan ko dedh sau udhaar likh do", cust())
    assert s[0].ex.intent == "unknown" and not s[0].inherited


def test_intent_inherited_only_when_all_explicit_agree():
    s = segments("Ramesh ko 100 aur Sunita ko 200 udhaar likh do", cust())
    assert s[0].ex.intent == "add_udhaar" and s[0].inherited


# ---- batch service flow
def test_typed_batch_requires_one_confirm_then_writes(svc):
    r = svc.on_typed(1, "Ramesh ne 100 diye, Sunita ne 200 diye, Mohan ne 50 diye")
    assert "3 entry taiyaar" in r.text and not r.wrote
    assert svc.ledger.balance(1) == 0                    # nothing written yet
    r2 = svc.on_typed(1, "haan")
    assert r2.wrote and svc.ledger.balance(1) == -10000
    assert svc.ledger.balance(3) == -5000


def test_batch_no_writes_nothing(svc):
    svc.on_typed(1, "Ramesh ko 100 aur Sunita ko 200 udhaar likh do")
    svc.on_callback(1, "no")
    assert svc.ledger.balance(1) == 0 and svc.ledger.balance(2) == 0


def test_batch_undo_reverses_only_batch_entries(svc):
    svc.on_typed(1, "Ramesh ko 100 aur Sunita ko 200 udhaar likh do")
    svc.on_callback(1, "yes")
    assert svc.ledger.balance(1) == 10000 and svc.ledger.balance(2) == 20000
    r = svc.on_callback(1, "undo_batch")
    assert "2 entry wapas" in r.text
    assert svc.ledger.balance(1) == 0 and svc.ledger.balance(2) == 0
    assert svc.ledger.verify()[0]
    assert "nahi mila" in svc.on_callback(1, "undo_batch").text   # idempotent


def test_voice_batch_disagreement_between_ears_holds_line_back(svc):
    a = "Ramesh ne 100 diye, Sunita ne 200 diye"
    b = "Ramesh ne 100 diye, Sunita ne 2000 diye"          # ear B disagrees on Sunita
    r = svc.on_heard(1, a, b, snr=25.0, dur=8.0)
    assert "1 entry taiyaar" in r.text and "Sunita" in r.text and "Roka hua" in r.text
    svc.on_callback(1, "yes")
    assert svc.ledger.balance(1) == -10000 and svc.ledger.balance(2) == 0


def test_voice_batch_without_second_ear_writes_nothing_automatically(svc):
    r = svc.on_heard(1, "Ramesh ne 100 diye, Sunita ne 200 diye", None, snr=25.0, dur=8.0)
    assert "Koi entry seedhi" in r.text and not r.wrote
    assert svc.ledger.balance(1) == 0


def test_batch_duplicate_line_held(svc):
    r = svc.on_typed(1, "Ramesh ne 100 diye, Ramesh ne 100 diye, Sunita ne 50 diye")
    assert "2 entry taiyaar" in r.text and "dobara aayi" in r.text


# ---- UPI parse
@pytest.mark.parametrize("text,amt,payer", [
    ("Rs.500.00 credited to HDFC Bank A/c xx1234 on 02-11-26 from VPA ramesh.sharma@okaxis (UPI 412345678901)", 50000, "Ramesh Sharma"),
    ("Received ₹1,250 from Sunita Verma. UPI Ref No 412345678902", 125000, "Sunita Verma"),
    ("Mohan Das paid you ₹300", 30000, "Mohan Das"),
    ("Your A/C XXXXX1234 credited by Rs 750.00 on 02Nov26 transfer from RAMESH SHARMA Ref No 412345678903. Avl Bal Rs 12,000.00", 75000, "Ramesh Sharma"),
])
def test_parse_upi_credits(text, amt, payer):
    c = parse_upi(text)
    assert c and c.amount_paise == amt and c.payer == payer


@pytest.mark.parametrize("text", [
    "Rs 200 debited from A/c 1234 paid to Big Bazaar UPI 412345678904",
    "hello 500", "Rs 100 credited and Rs 200 credited today", "credited",
])
def test_parse_upi_refuses_debits_and_ambiguity(text):
    assert parse_upi(text) is None


# ---- UPI service flow
def test_upi_matched_confirm_then_dedupe(svc):
    svc.ledger.add_udhaar(1, 100000)
    msg = "Received ₹500 from Ramesh Sharma. UPI Ref No 412345678901"
    r = svc.on_typed(1, msg)
    assert "UPI" in r.text and r.buttons and svc.ledger.balance(1) == 100000
    r = svc.on_callback(1, "yes")
    assert svc.ledger.balance(1) == 50000
    again = svc.on_typed(1, msg)
    assert "pehle hi" in again.text and svc.ledger.balance(1) == 50000
    # the raw reference never enters the chain
    assert all("412345678901" not in str(e.meta) for e in svc.ledger.entries())
    # undo frees the reference again (reversed entries do not block a legitimate re-record)
    svc.on_callback(1, "undo")
    assert svc.ledger.balance(1) == 100000
    assert "pehle hi" not in svc.on_typed(1, msg).text


def test_upi_unknown_payer_asks_which_customer(svc):
    svc.ledger.add_udhaar(2, 70000)
    r = svc.on_typed(1, "Received ₹300 from Zzyzx Qwerty. UPI Ref No 412345678909")
    assert "Kis customer" in r.text and r.buttons
    r = svc.on_callback(1, f"cust:{2}")
    assert "Sunita" in r.text and svc.ledger.balance(2) == 70000     # still asks to confirm; not yet written
    svc.on_callback(1, "yes")
    assert svc.ledger.balance(2) == 40000


def test_upi_debit_never_booked(svc):
    r = svc.on_typed(1, "Rs 200 debited from A/c 1234 paid to Big Bazaar UPI 412345678904")
    assert "saaf nahi" in r.text


# ---- listen_long (fake Gnani, real chunker + ffmpeg)
async def test_listen_long_chunks_chain_previous_text_and_join():
    import httpx
    from munim.gnani.client import GnaniClient
    from munim.listen.listener import AudioTooLong, listen_long
    from tests.test_pipeline import to_ogg

    sr = 16000
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.2, sr * 40).astype(np.float32)
    x[int(27 * sr): int(27.3 * sr)] = 0.0
    seen = []

    def handler(req: httpx.Request):
        body = req.content
        seen.append(body)
        if b"prisma-v3.0" in body:
            return httpx.Response(200, json={"transcript": f"A{len([s for s in seen if b'prisma-v3.0' in s])}", "detected_language_code": "hi-IN"})
        return httpx.Response(200, json={"output": {"literal": "B"}})

    async def nosleep(_):
        return None
    c = GnaniClient("k", transport=httpx.MockTransport(handler), sleep=nosleep)
    lh = await listen_long(c, to_ogg(wav_of(x)), [])
    assert lh.chunks == 2 and lh.calls == 4
    assert lh.a_text == "A1. A2" and lh.b_text == "B. B"
    prisma = [s for s in seen if b"prisma-v3.0" in s]
    assert b"previous_text" not in prisma[0] and b"A1" in prisma[1]       # chunk 2 is told what chunk 1 said
    with pytest.raises(AudioTooLong):
        await listen_long(c, to_ogg(wav_of(rng.normal(0, 0.2, sr * 100).astype(np.float32))), [])
