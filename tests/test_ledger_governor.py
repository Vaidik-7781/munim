import sqlite3
from datetime import datetime

import pytest

from munim.config import Settings
from munim.govern.governor import IST, check, draft_reminder
from munim.govern.tone_lint import lint
from munim.ledger.chain import GENESIS, Ledger, LedgerError

CFG = Settings()


class Clock:
    def __init__(self, dt: datetime):
        self.t = dt.timestamp()

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def ist(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


@pytest.fixture
def clock():
    return Clock(ist(2026, 11, 2, 11))


@pytest.fixture
def led(tmp_path, clock):
    L = Ledger(str(tmp_path / "t.db"), "secret", clock=clock)
    yield L
    L.close()


# ------------------------------------------------------------ ledger
def test_genesis_and_chain_links(led):
    c = led.add_customer("Ramesh Sharma")
    e1 = led.add_udhaar(c.id, 15000)
    e2 = led.record_payment(c.id, 5000)
    assert e1.prev_hash == GENESIS and e2.prev_hash == e1.hash
    assert led.verify()[0]


def test_balance_and_undo_by_reversal(led, clock):
    c = led.add_customer("A")
    led.add_udhaar(c.id, 25000)
    led.record_payment(c.id, 10000)
    assert led.balance(c.id) == 15000
    rev = led.undo_last(c.id)
    assert rev.kind == "reversal" and led.balance(c.id) == 25000
    rev2 = led.undo_last(c.id)
    assert led.balance(c.id) == 0 and rev2.reverses_seq == 1
    assert led.undo_last(c.id) is None
    assert len(led.entries(c.id)) == 4          # nothing deleted — history is intact
    assert led.verify()[0]


def test_undo_global_last(led):
    a, b = led.add_customer("A"), led.add_customer("B")
    led.add_udhaar(a.id, 100)
    led.add_udhaar(b.id, 200)
    led.undo_last()
    assert led.balance(b.id) == 0 and led.balance(a.id) == 100


def test_tamper_amount_detected(led, tmp_path):
    c = led.add_customer("A")
    led.add_udhaar(c.id, 15000)
    led.add_udhaar(c.id, 20000)
    led.add_udhaar(c.id, 30000)
    led._db.execute("UPDATE entries SET amount_paise=1 WHERE seq=2")
    ok, seq, msg = led.verify()
    assert not ok and seq == 2 and "hash" in msg


def test_tamper_delete_detected(led):
    c = led.add_customer("A")
    for p in (100, 200, 300):
        led.add_udhaar(c.id, p)
    led._db.execute("DELETE FROM entries WHERE seq=2")
    ok, seq, msg = led.verify()
    assert not ok and seq == 3


def test_tamper_rechain_attempt_detected(led):
    c = led.add_customer("A")
    for p in (100, 200, 300):
        led.add_udhaar(c.id, p)
    led._db.execute("UPDATE entries SET prev_hash=? WHERE seq=3", ("f" * 64,))
    assert not led.verify()[0]


def test_rejects_float_and_nonpositive(led):
    c = led.add_customer("A")
    with pytest.raises(LedgerError):
        led.add_udhaar(c.id, 0)
    with pytest.raises(LedgerError):
        led.add_udhaar(c.id, -5)
    with pytest.raises(LedgerError):
        led.append("add_udhaar", c.id, 1.5)  # type: ignore[arg-type]


def test_chain_has_no_names(led):
    c = led.add_customer("Ramesh Sharma")
    led.add_udhaar(c.id, 100)
    raw = " ".join(str(tuple(r)) for r in led._db.execute("SELECT * FROM entries"))
    assert "Ramesh" not in raw and "Sharma" not in raw


def test_erasure_keeps_chain_valid(led):
    c = led.add_customer("Ramesh Sharma")
    led.add_udhaar(c.id, 100)
    led.erase_customer(c.id)
    assert led.verify()[0]
    assert led.customers() == []
    assert led.customer_row(c.id)["name"] == "[erased]"


def test_history_excludes_reversed(led):
    c = led.add_customer("A")
    led.add_udhaar(c.id, 100)
    led.add_udhaar(c.id, 200)
    led.undo_last(c.id)
    assert led.history_amounts(c.id) == [100]


def test_recent_writes_window(led, clock):
    c = led.add_customer("A")
    led.add_udhaar(c.id, 100)
    clock.advance(60)
    rw = led.recent_writes(120)
    assert rw == [(c.id, "add_udhaar", 100, 60.0)]
    clock.advance(100)
    assert led.recent_writes(120) == []


def test_dispute_flow(led):
    c = led.add_customer("A")
    e = led.add_udhaar(c.id, 100)
    assert not led.is_disputed(c.id)
    led.open_dispute(c.id, e.seq, "maine nahi liya")
    assert led.is_disputed(c.id)
    led.resolve_dispute(c.id)
    assert not led.is_disputed(c.id)
    assert led.balance(c.id) == 100   # a dispute alone never changes money


def test_persistence_roundtrip(tmp_path, clock):
    p = str(tmp_path / "p.db")
    L1 = Ledger(p, "s", clock=clock)
    c = L1.add_customer("A")
    L1.add_udhaar(c.id, 500)
    L1.close()
    L2 = Ledger(p, "s", clock=clock)
    assert L2.balance(c.id) == 500 and L2.verify()[0]


# ------------------------------------------------------------ governor
def ready(led, amount=25000):
    c = led.add_customer("Ramesh Sharma")
    led.add_udhaar(c.id, amount)
    led.grant_consent(c.id)
    return c


def test_allowed_happy_path(led):
    c = ready(led)
    d = check(led, CFG, c.id)
    assert d.allowed, d.reasons


def test_blocks_without_consent(led):
    c = led.add_customer("A")
    led.add_udhaar(c.id, 100)
    assert "no_consent" in check(led, CFG, c.id).reasons


def test_blocks_paused_optout_disputed_nobalance(led):
    c = ready(led)
    led.set_flag(c.id, paused=1)
    assert "paused" in check(led, CFG, c.id).reasons
    led.set_flag(c.id, paused=0, opted_out=1)
    assert "opted_out" in check(led, CFG, c.id).reasons
    led.set_flag(c.id, opted_out=0)
    led.open_dispute(c.id, 1)
    assert "disputed" in check(led, CFG, c.id).reasons
    c2 = led.add_customer("B")
    led.grant_consent(c2.id)
    assert "no_balance" in check(led, CFG, c2.id).reasons


@pytest.mark.parametrize("hour,ok", [(7, False), (8, True), (18, True), (19, False), (23, False)])
def test_hours_window(led, clock, hour, ok):
    c = ready(led)
    clock.t = ist(2026, 11, 2, hour, 30).timestamp()
    d = check(led, CFG, c.id)
    assert ("outside_hours" not in d.reasons) == ok
    if not ok:
        assert d.next_allowed_at is not None
        assert datetime.fromtimestamp(d.next_allowed_at, IST).hour == 8


def test_72h_gap_and_next_time(led, clock):
    c = ready(led)
    led.log_reminder(c.id)
    clock.advance(71 * 3600)
    d = check(led, CFG, c.id)
    assert "too_soon" in d.reasons
    clock.advance(2 * 3600)
    assert "too_soon" not in check(led, CFG, c.id).reasons


def test_monthly_cap_three(led, clock):
    c = ready(led)
    for _ in range(3):
        led.log_reminder(c.id)
        clock.advance(73 * 3600)
    d = check(led, CFG, c.id)
    assert "monthly_cap" in d.reasons
    clock.advance(30 * 86400)
    assert "monthly_cap" not in check(led, CFG, c.id).reasons


def test_draft_is_linted_and_disclosed(led):
    c = ready(led, 7_544_000)
    d, draft = draft_reminder(led, CFG, c.id, shop="Gupta Kirana")
    assert d.allowed and draft is not None
    assert "₹75,440" in draft.text and "AI" in draft.text and "Gupta Kirana" in draft.text
    assert "75" not in draft.voice_text.replace("75440", "")  # digits never go to TTS
    assert not any(ch.isdigit() for ch in draft.voice_text)
    assert lint(draft.text).ok


def test_no_draft_when_blocked(led):
    c = led.add_customer("A")
    led.add_udhaar(c.id, 100)
    d, draft = draft_reminder(led, CFG, c.id, shop="S")
    assert draft is None and not d.allowed and "sehmati" in d.explain_hi()


# ------------------------------------------------------------ tone linter
@pytest.mark.parametrize("text,code", [
    ("Pay today or we will call the police. Munim AI", "legal_threat"),
    ("Aapko sharam aani chahiye. Munim AI", "shaming"),
    ("Paise do warna bura hoga. Munim AI", "threat"),
    ("Yeh last warning hai. Munim AI", "false_urgency"),
    ("Padosi ko bata denge. Munim AI", "third_party"),
    ("Byaaj lagega. Munim AI", "unagreed_charges"),
    ("PAISE DO ABHI KE ABHI WARNA", "shouting"),
    ("Namaste, khate mein 150 baaki hai.", "missing_ai_disclosure"),
    ("Hello!! Munim AI yahan hai!!", "excess_exclamation"),
])
def test_linter_catches(text, code):
    assert code in lint(text).violations


def test_linter_passes_gentle():
    assert lint("Namaste Ramesh ji. Yeh Munim (AI sahayak) ka sandesh hai. Jab suvidha ho bata dijiye.").ok
