import json

import httpx
import pytest

from munim.agent.qa import answer, answer_rules, is_new_query, rule_tool, _redact
from munim.config import Settings
from munim.insights.statement import build
from munim.ledger.chain import Ledger
from munim.llm.client import LLMClient
from munim.service import MunimService


@pytest.fixture
def led(tmp_path):
    t = [1_700_000_000.0]
    led = Ledger(str(tmp_path / "q.db"), "s")
    a = led.add_customer("Ramesh Sharma")
    b = led.add_customer("Sunita Verma")
    led.add_udhaar(a.id, 50000)
    led.record_payment(a.id, 20000)
    led.add_udhaar(b.id, 90000)
    return led


def test_rule_tools():
    assert rule_tool("Ramesh ne last payment kab di") == "last_payment"
    assert rule_tool("Ramesh ki history dikhao") == "history"
    assert rule_tool("agle hafte kitni vasooli") == "forecast"
    assert rule_tool("sabse zyada udhaar kiska") == "top"
    assert rule_tool("Ramesh ka kitna baaki") == "balance"
    assert rule_tool("namaste") is None
    assert is_new_query("Ramesh ki history dikhao") and not is_new_query("Ramesh ko 100 udhaar likh do")


def test_answers_come_from_ledger(led):
    now = led._clock()
    qa, cid = answer_rules(led, now, "Ramesh ne last payment kab di")
    assert cid == 1 and "₹200" in qa.text and "din pehle" in qa.text and not any(c.isdigit() for c in qa.voice)
    qa, _ = answer_rules(led, now, "Ramesh ki history dikhao")
    assert "udhaar" in qa.text and "jama" in qa.text and "₹300" in qa.text
    qa, _ = answer_rules(led, now, "Sunita ne last payment kab di")
    assert "koi payment darj nahi" in qa.text


def test_needs_customer_and_ambiguity(led):
    qa, _ = answer_rules(led, led._clock(), "last payment kab di")
    assert "Kiske baare mein" in qa.text


def test_service_routes_new_queries_without_touching_ledger(led):
    svc = MunimService(led, Settings())
    n = len(led.entries())
    r = svc.on_typed(1, "Ramesh ki history dikhao")
    assert "pichli" in r.text and len(led.entries()) == n


def test_redaction_hides_names():
    from munim.scribe.names import Customer
    out, seen = _redact("Ramesh Sharma ne kab diya? Sharma ji ka", [Customer(1, "Ramesh Sharma")])
    assert "Ramesh" not in out and "Sharma" not in out and seen == {1}


def llm_returning(payload: dict, captured: list | None = None):
    def handler(req: httpx.Request):
        if captured is not None:
            captured.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})
    return LLMClient("k", transport=httpx.MockTransport(handler))


async def test_llm_routes_unknown_question_but_never_sees_names(led):
    cap: list = []
    llm = llm_returning({"tool": "last_payment", "customer": "C1"}, cap)
    qa = await answer(led, led._clock(), "Ramesh Sharma se aakhri baar paisa mila tha kab?", llm)
    assert qa.via == "llm" and "₹200" in qa.text
    sent = json.dumps(cap[0])
    assert "Ramesh" not in sent and "Sharma" not in sent and "C1" in sent


async def test_llm_invented_customer_rejected(led):
    llm = llm_returning({"tool": "balance", "customer": "C2"})        # question never mentioned customer 2
    qa = await answer(led, led._clock(), "Ramesh se kab tak paisa wapas milega bhai", llm)
    assert "Sunita" not in qa.text


async def test_llm_bad_output_falls_back_to_help(led):
    llm = llm_returning({"tool": "delete_everything", "customer": None})
    qa = await answer(led, led._clock(), "kuch bhi random", llm)
    assert qa.tool is None and "samajh nahi aaya" in qa.text.lower()


async def test_no_llm_key_rules_only(led):
    qa = await answer(led, led._clock(), "kuch bhi random", LLMClient(""))
    assert qa.tool is None


# ---- statement
def test_statement_refusals(led):
    now = led._clock()
    assert build(led, 1, now, "Dukaan").reason == "no_consent"
    led.grant_consent(1)
    assert build(led, 1, now, "Dukaan").ok
    led.open_dispute(1, None, "x")
    assert build(led, 1, now, "Dukaan", asked=True).reason == "disputed"
    led.resolve_dispute(1)
    led.revoke_consent(1)
    assert build(led, 1, now, "Dukaan", asked=True).reason == "opted_out"


def test_statement_text_voice_rules(led):
    led.grant_consent(1)
    st = build(led, 1, led._clock(), "Gupta Store")
    assert st.ok and "AI" in st.text and "₹300" in st.text and st.tts_language == "hi-en"
    assert not any(c.isdigit() for c in st.voice_text)               # TTS never gets digits
    led.set_flag(1, lang="en")
    st = build(led, 1, led._clock(), "Gupta Store")
    assert st.tts_language == "en-IN" and "three hundred rupees" in st.voice_text and "AI" in st.voice_text


def test_service_statement_returns_voice(led):
    led.grant_consent(1)
    r = MunimService(led, Settings(), "Gupta Store").statement(1, 1)
    assert r.voices and r.voices[0][1] == "hi-en"
    assert "Statement nahi" in MunimService(led, Settings()).statement(1, 2).text
