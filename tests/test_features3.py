import io
from types import SimpleNamespace

import pytest

from munim.books import payables
from munim.config import Settings
from munim.insights import dispute_pack
from munim.ledger.chain import Ledger
from munim.pay.upi_link import qr_png, upi_uri, valid_vpa
from munim.service import MunimService


@pytest.fixture
def led(tmp_path):
    l = Ledger(str(tmp_path / "f.db"), "s")
    a = l.add_customer("Ramesh Sharma")
    l.add_udhaar(a.id, 50000, src="voice", flags=["low_snr"], confirmed=True)
    l.add_udhaar(a.id, 20000, src="text", by="munshi-1")
    l.record_payment(a.id, 10000, src="upi")
    l.undo_last()
    return l


def test_dispute_pack_shows_provenance_weakness_and_chain(led):
    text, weak = dispute_pack.build(led, 1, "Gupta Store", led._clock())
    assert "koi badlaav nahi" in text and "voice note" in text and "munshi-1" in text and "UPI message" in text
    assert "wapas li gayi" in text and "awaaz saaf nahi thi" in text and "phir bhi aapne haan" in text
    assert "₹700" in text and len(weak) == 1
    pdf = dispute_pack.to_pdf(text)
    assert pdf.startswith(b"%PDF")


def test_dispute_pack_detects_tamper(led):
    led._db.execute("UPDATE entries SET amount_paise=1 WHERE seq=1")
    text, _ = dispute_pack.build(led, 1, "S", led._clock())
    assert "GADBAD" in text


def test_upi_uri_exact_and_validated():
    u = upi_uri("shop@okaxis", "Gupta Store", 125050, "Khata")
    assert "am=1250.50" in u and u.startswith("upi://pay?pa=shop@okaxis") and "cu=INR" in u
    assert valid_vpa("a.b-c@upi") and not valid_vpa("no-at-sign") and not valid_vpa("x@") and not valid_vpa("a b@upi")
    with pytest.raises(ValueError):
        upi_uri("bad", "S", 100)
    with pytest.raises(ValueError):
        upi_uri("a@upi", "S", 0)
    assert qr_png(u)[:4] == b"\x89PNG"


def test_payables_parse_record_and_position(tmp_path):
    recv = Ledger(str(tmp_path / "r.db"), "s")
    c = recv.add_customer("Ramesh Sharma")
    recv.add_udhaar(c.id, 300000)
    pay = Ledger(str(tmp_path / "p.db"), "s2")
    assert payables.parse_dena("Agarwal Traders 5000") == ("Agarwal Traders", "bought", 500000)
    assert payables.parse_dena("Agarwal Traders diya 2000") == ("Agarwal Traders", "paid", 200000)
    assert payables.parse_dena("nothing here") is None
    assert "naya supplier" in payables.record(pay, "Agarwal Traders", "bought", 500000, by="munshi-1")
    assert "naya supplier" not in payables.record(pay, "Agarwal Traders", "paid", 200000)
    pos = payables.position(recv, pay)
    assert "₹3,000" in pos and "₹3,000" in pos and "Dhyaan" not in pos
    payables.record(pay, "Agarwal Traders", "bought", 900000)
    assert "Dhyaan" in payables.position(recv, pay)


# ---- transliterate name rescue (service side) + listener
def test_rescued_name_is_asked_not_trusted(tmp_path):
    l = Ledger(str(tmp_path / "n.db"), "s")
    l.add_customer("Ramesh Sharma")
    svc = MunimService(l, Settings())
    r = svc.on_heard(1, "ரமேஷ் ko sau udhaar likh do", "ரமேஷ் ko sau udhaar likh do", snr=30, dur=4,
                     d_text="Ramesh Sharma ko sau udhaar likh do")
    assert not r.wrote and "roman mein" in r.text and r.buttons
    svc.on_callback(1, "yes")
    assert l.balance(1) == 10000


async def test_listener_runs_pass_d_only_when_no_customer_recognised(tmp_path):
    import httpx
    from munim.gnani.client import GnaniClient
    from munim.listen.listener import listen
    from tests.test_pipeline import synth_wav, to_ogg

    def make(a_text):
        def handler(req: httpx.Request):
            b = req.content
            if b"transliterate" in b:
                return httpx.Response(200, json={"transcript": "Ramesh Sharma ko sau udhaar"})
            if b"prisma-v3.0" in b:
                return httpx.Response(200, json={"transcript": a_text, "detected_language_code": "hi-IN"})
            return httpx.Response(200, json={"output": {"literal": a_text}})
        return handler

    async def nosleep(_):
        return None
    l = Ledger(str(tmp_path / "l.db"), "s")
    l.add_customer("Ramesh Sharma")
    ogg = to_ogg(synth_wav(3.0))
    h = await listen(GnaniClient("k", transport=httpx.MockTransport(make("ரமேஷ் ko sau udhaar")), sleep=nosleep), ogg, l.customers())
    assert h.d is not None and h.d.transcript.startswith("Ramesh") and h.calls == 3
    h2 = await listen(GnaniClient("k", transport=httpx.MockTransport(make("Ramesh Sharma ko sau udhaar")), sleep=nosleep), ogg, l.customers())
    assert h2.d is None and h2.calls == 2                             # customer recognised: no extra Gnani spend
    h3 = await listen(GnaniClient("k", transport=httpx.MockTransport(make("namaste")), sleep=nosleep), ogg, l.customers())
    assert h3.d is None                                              # no money mentioned: nothing to rescue
