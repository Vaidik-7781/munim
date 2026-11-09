import pytest

from munim.config import Settings
from munim.govern.governor import IST
from munim.ledger.chain import Ledger
from munim.service import MunimService
from datetime import datetime

CFG = Settings()
RS = 100


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 2, 11, 0, tzinfo=IST).timestamp()

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    led = Ledger(str(tmp_path / "s.db"), "k", clock=clock)
    sharma = led.add_customer("Ramesh Sharma")
    verma = led.add_customer("Sunita Verma")
    svc = MunimService(led, CFG, shop_name="Gupta Kirana", clock=clock)
    return svc, led, clock, sharma, verma


def voice(svc, a, b=None, **kw):
    b = a if b is None else b
    return svc.on_heard(1, a, b, snr=kw.get("snr", 25.0), dur=kw.get("dur", 3.0))


def test_clean_voice_writes_immediately_with_undo(env):
    svc, led, clock, sharma, _ = env
    r = voice(svc, "Sharma ji ko dedh sau udhaar likh do")
    assert r.wrote and led.balance(sharma.id) == 150 * RS
    assert "₹150" in r.text and ("Undo ↩️", "undo") in r.buttons
    assert "150" not in (r.voice_text or "")           # TTS text has no digits


def test_undo_button_reverses(env):
    svc, led, clock, sharma, _ = env
    voice(svc, "Sharma ji ko 150 udhaar")
    clock.t += 5
    r = svc.on_callback(1, "undo")
    assert led.balance(sharma.id) == 0 and "ulat" in r.text
    assert led.verify()[0]


def test_text_undo_command(env):
    svc, led, clock, sharma, _ = env
    voice(svc, "Sharma ji ko 150 udhaar")
    clock.t += 200
    svc.on_typed(1, "undo")
    assert led.balance(sharma.id) == 0


def test_noisy_audio_blocks_then_haan_writes(env):
    svc, led, clock, sharma, _ = env
    r = voice(svc, "Sharma ji ko 150 udhaar", snr=4.0)
    assert not r.wrote and led.balance(sharma.id) == 0
    assert "awaaz saaf nahi" in r.text and ("Haan ✅", "yes") in r.buttons
    r2 = svc.on_typed(1, "haan")
    assert r2.wrote and led.balance(sharma.id) == 150 * RS


def test_block_then_nahi_writes_nothing(env):
    svc, led, clock, sharma, _ = env
    voice(svc, "Sharma ji ko 150 udhaar", snr=4.0)
    r = svc.on_typed(1, "nahi")
    assert "kuch nahi likha" in r.text
    assert led.balance(sharma.id) == 0 and led.entries(sharma.id) == []


def test_scale_disagreement_asks_which_amount(env):
    svc, led, clock, sharma, _ = env
    r = voice(svc, "Sharma ji ko dedh sau udhaar", "Sharma ji ko dedh hazaar udhaar")
    assert not r.wrote and "₹150" in r.text and "₹1,500" in r.text
    assert led.balance(sharma.id) == 0
    r2 = svc.on_callback(1, f"amt:{150 * RS}")
    assert r2.wrote and led.balance(sharma.id) == 150 * RS


def test_choose_second_amount_by_text(env):
    svc, led, clock, sharma, _ = env
    voice(svc, "Sharma ji ko dedh sau udhaar", "Sharma ji ko dedh hazaar udhaar")
    r = svc.on_typed(1, "2")
    assert r.wrote and led.balance(sharma.id) == 1500 * RS


def test_pending_expires_after_90s(env):
    svc, led, clock, sharma, _ = env
    voice(svc, "Sharma ji ko 150 udhaar", snr=3.0)
    clock.t += 91
    r = svc.on_callback(1, "yes")
    assert "Samay khatam" in r.text and led.balance(sharma.id) == 0


def test_b_missing_requires_confirmation(env):
    svc, led, clock, sharma, _ = env
    r = svc.on_heard(1, "Sharma ji ko 150 udhaar", None, snr=25.0, dur=3.0)
    assert not r.wrote and "dobara jaanch" in r.text


def test_ambiguous_name_asks_which_sharma(env):
    svc, led, clock, sharma, _ = env
    dinesh = led.add_customer("Dinesh Sharma")
    r = voice(svc, "Sharma ji ko 150 udhaar")
    assert "Kaun sa Sharma" in r.text and not r.wrote
    r2 = svc.on_typed(1, "Dinesh")
    assert "Dinesh Sharma" in r2.text and "Sahi hai" in r2.text     # then confirms the money
    r3 = svc.on_typed(1, "haan")
    assert r3.wrote and led.balance(dinesh.id) == 150 * RS and led.balance(sharma.id) == 0


def test_duplicate_within_two_minutes_blocks(env):
    svc, led, clock, sharma, _ = env
    voice(svc, "Sharma ji ko 150 udhaar")
    clock.t += 30
    r = voice(svc, "Sharma ji ko 150 udhaar")
    assert not r.wrote and "same entry" in r.text
    assert led.balance(sharma.id) == 150 * RS


def test_multi_amount_confirms_sum(env):
    svc, led, clock, sharma, _ = env
    r = voice(svc, "Sharma ji dedh sau aur dhai hazaar udhaar")
    assert not r.wrote and "₹2,650" in r.text


def test_payment_and_balance(env):
    svc, led, clock, sharma, _ = env
    voice(svc, "Sharma ji ko paanch sau udhaar")
    clock.t += 500
    r = voice(svc, "Sharma ji ne dedh sau diye")
    assert r.wrote and led.balance(sharma.id) == 350 * RS
    q = svc.on_typed(1, "Sharma ji ka kitna baaki hai")
    assert "₹350" in q.text


def test_unknown_customer_not_created(env):
    svc, led, clock, *_ = env
    r = voice(svc, "Pandey ji ko 150 udhaar")
    assert "/add" in r.text and len(led.customers()) == 2


def test_gibberish_gets_help(env):
    svc, *_ = env
    assert "Munim ko aise bolein" in svc.on_typed(1, "namaste").text


def test_reminder_needs_consent_then_approval_flow(env):
    svc, led, clock, sharma, _ = env
    voice(svc, "Sharma ji ko paanch sau udhaar")
    r = svc.on_typed(1, "Sharma ji ko reminder bhejo")
    assert "sehmati" in r.text
    led.grant_consent(sharma.id)
    r = svc.on_typed(1, "Sharma ji ko reminder bhejo")
    assert "taiyaar" in r.text and "AI" in r.text
    assert led.reminder_times(sharma.id) == []            # nothing logged until approved
    r = svc.on_typed(1, "haan")
    assert "manzoor" in r.text and len(led.reminder_times(sharma.id)) == 1
    r = svc.on_typed(1, "Sharma ji ko reminder bhejo")
    assert "72 ghante" in r.text                          # Governor gap rule


def test_pause_resume_optout_dispute(env):
    svc, led, clock, sharma, _ = env
    svc.on_typed(1, "Sharma ji ruko")
    assert led.customer_row(sharma.id)["paused"] == 1
    svc.on_typed(1, "Sharma ji dobara shuru karo")
    assert led.customer_row(sharma.id)["paused"] == 0
    svc.on_typed(1, "Sharma ji reminder mat bhejo")
    assert led.customer_row(sharma.id)["opted_out"] == 1
    svc.on_typed(1, "Sharma ji maine nahi liya")
    assert led.is_disputed(sharma.id)


def test_ledger_always_verifies_after_flows(env):
    svc, led, clock, *_ = env
    voice(svc, "Sharma ji ko 150 udhaar")
    clock.t += 200
    voice(svc, "Verma ji ne 50 diye", snr=2.0)
    svc.on_typed(1, "nahi")
    svc.on_typed(1, "undo")
    assert led.verify()[0]
