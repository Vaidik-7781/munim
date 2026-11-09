import pytest

from munim import portal
from munim.config import Settings
from munim.tenancy.registry import Registry


@pytest.fixture
def env(tmp_path):
    reg = Registry(str(tmp_path), "m", Settings())
    t = reg.create_shop("Gupta Store", 1)
    c = t.ledger.add_customer("Sunita Verma")
    t.ledger.add_udhaar(c.id, 45000)
    t.ledger.record_payment(c.id, 10000)
    return t, c


def test_balance_self_service_even_when_opted_out(env):
    t, c = env
    t.ledger.revoke_consent(c.id)
    r = portal.handle(t, c.id, "mera kitna baaki hai?", owner_chat=1)
    assert "₹350" in r.text and r.voice and not any(ch.isdigit() for ch in r.voice)


def test_stop_is_immediate_and_notifies_owner(env):
    t, c = env
    t.ledger.grant_consent(c.id)
    r = portal.handle(t, c.id, "ruko, ab mat bhejo", owner_chat=1)
    assert t.ledger.customer_row(c.id)["consent"] == "revoked" and r.owner_msgs and "band" in r.owner_msgs[0].text


def test_wrong_opens_dispute(env):
    t, c = env
    r = portal.handle(t, c.id, "yeh hisaab galat hai", owner_chat=1)
    assert t.ledger.is_disputed(c.id) and r.owner_msgs


def test_promise_goes_to_owner_as_pending_never_written(env):
    t, c = env
    r = portal.handle(t, c.id, "shukravar tak 200 denge", owner_chat=1)
    assert r.owner_msgs and r.owner_msgs[0].buttons
    pend = t.service.pending.get(1)
    assert pend is not None and pend.kind == "confirm_promise"


def test_payment_claim_needs_owner_confirmation(env):
    t, c = env
    before = t.ledger.balance(c.id)
    r = portal.handle(t, c.id, "maine 200 de diye", owner_chat=1)
    assert r.owner_msgs and "Sirf tab" in r.owner_msgs[0].text
    assert t.ledger.balance(c.id) == before


def test_language_learned_and_english_reply(env):
    t, c = env
    r = portal.handle(t, c.id, "how much do I owe", owner_chat=1, detected_lang="en-IN")
    assert t.ledger.customer_row(c.id)["lang"] == "en" and r.tts_language == "en-IN" and "outstanding" in r.text.lower()
    portal.handle(t, c.id, "mera hisaab", owner_chat=1, detected_lang="ta-IN")
    assert t.ledger.customer_row(c.id)["lang"] == "ta"           # remembered for reminders; replies stay hi/en


def test_welcome_texts():
    assert "sehmati" in portal.welcome("S", "Ram", "hi").text and "consent" in portal.welcome("S", "Ram", "en").text
