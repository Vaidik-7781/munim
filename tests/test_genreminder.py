import json
from datetime import datetime

import httpx
import pytest

from munim.config import Settings
from munim.govern.genreminder import GenReminder, base_templates, validate, TOKENS_TEXT, TOKENS_VOICE
from munim.govern.governor import draft_reminder
from munim.ledger.chain import Ledger
from munim.llm.client import LLMClient


from munim.timeutil import IST
NOON = datetime(2026, 11, 3, 11, 0, tzinfo=IST).timestamp()


def llm_with(text, voice, calls=None):
    def handler(req):
        if calls is not None:
            calls.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"text": text, "voice": voice})}}]})
    return LLMClient("k", transport=httpx.MockTransport(handler))


@pytest.fixture
def base(tmp_path):
    led = Ledger(str(tmp_path / "g.db"), "s")
    c = led.add_customer("Ramesh Sharma")
    led.grant_consent(c.id)
    led.add_udhaar(c.id, 15000)
    d, draft = draft_reminder(led, Settings(), c.id, "Gupta Store", now=NOON)
    assert draft is not None, d.reasons
    return led, c, draft


GOOD_T = "Namaste [[NAME]] ji, [[SHOP]] ke Munim (AI sahayak) ki taraf se pyaar bhara yaad: aapke khate mein [[AMOUNT]] baaki hai. Jab theek lage bata dijiye. Galat lage ya band karwana ho to \"ruko\" likh dijiye."
GOOD_V = "Namaste [[NAME]] ji, yeh [[SHOP]] ki taraf se Munim, ek AI sahayak bol raha hai. Aapke khate mein [[AMOUNT_WORDS]] baaki dikh rahe hain. Jab suvidha ho dukaan par bata dijiye."


async def test_good_rewrite_accepted_and_facts_filled_by_code(base):
    led, c, draft = base
    g = await GenReminder(llm_with(GOOD_T, GOOD_V)).generate(draft, name="Ramesh", shop="Gupta Store", customer_lang="hi")
    assert g.mode == "rewritten" and "Ramesh" in g.draft.text and "₹150" in g.draft.text
    assert "Gupta Store" in g.draft.text and not any(ch.isdigit() for ch in g.draft.voice_text)
    assert "[[" not in g.draft.text + g.draft.voice_text


@pytest.mark.parametrize("bad_text", [
    GOOD_T.replace("[[AMOUNT]]", "Rs 999"),                               # model wrote its own number
    GOOD_T.replace("[[NAME]]", "[[NAME]] [[NAME]]"),                       # token duplicated
    GOOD_T.replace("(AI sahayak) ", ""),                                   # AI disclosure removed
    GOOD_T + " Warna police ko bata denge.",                               # threat
    GOOD_T.replace(' Galat lage ya band karwana ho to "ruko" likh dijiye.', ""),   # no way to stop
    GOOD_T + " Dekhein https://pay.me/x",                                  # URL
    "chhota",                                                              # too short
])
async def test_unsafe_rewrites_fall_back_to_template(base, bad_text):
    led, c, draft = base
    g = await GenReminder(llm_with(bad_text, GOOD_V)).generate(draft, name="Ramesh", shop="Gupta Store", customer_lang="hi")
    assert g.mode == "template" and g.draft is draft


async def test_no_llm_key_uses_template(base):
    led, c, draft = base
    g = await GenReminder(LLMClient("")).generate(draft, name="Ramesh", shop="S", customer_lang="hi")
    assert g.mode == "template"


async def test_llm_failure_uses_template(base):
    led, c, draft = base
    bad = LLMClient("k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert (await GenReminder(bad).generate(draft, name="R", shop="S", customer_lang="en")).mode == "template"


TAMIL_T = "வணக்கம் [[NAME]], [[SHOP]] இன் Munim (AI உதவியாளர்) நினைவூட்டல்: உங்கள் கணக்கில் [[AMOUNT]] பாக்கி உள்ளது. தவறு என்றால் \"wrong\" என்றும் நிறுத்த \"stop\" என்றும் பதிலளிக்கவும்."
TAMIL_V = "வணக்கம் [[NAME]], இது [[SHOP]] சார்பாக Munim என்ற AI உதவியாளர். உங்கள் கணக்கில் [[AMOUNT_WORDS]] பாக்கி உள்ளது. கடையில் தெரிவிக்கவும்."


async def test_translation_cached_validated_and_flagged(base):
    led, c, draft = base
    calls = []
    gen = GenReminder(llm_with(TAMIL_T, TAMIL_V, calls))
    g = await gen.generate(draft, name="Ramesh", shop="Gupta Store", customer_lang="ta")
    assert g.mode == "translated" and g.draft.lang == "ta" and g.draft.tts_language == "ta"
    assert "anuvaad" in g.note and "₹150" in g.draft.text
    assert "one hundred fifty rupees" in g.draft.voice_text          # amount as English words, spoken code-mixed
    await gen.generate(draft, name="Sunita", shop="S", customer_lang="ta")
    assert len(calls) == 1                                           # template translation reused across customers
    # translation that drops the Latin stop/wrong words is rejected
    bad = GenReminder(llm_with(TAMIL_T.replace('"wrong"', "").replace('"stop"', ""), TAMIL_V))
    assert (await bad.generate(draft, name="R", shop="S", customer_lang="ta")).mode == "template"


def test_validate_direct():
    t, v = base_templates("hi")
    assert validate(t, TOKENS_TEXT, need_stopword=True) and validate(v, TOKENS_VOICE, need_stopword=False)
    assert not validate(t.replace("[[SHOP]]", ""), TOKENS_TEXT, need_stopword=True)
