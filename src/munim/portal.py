"""Customer portal: what a CUSTOMER can do for themselves once linked via an invite.

Principles: anything that REDUCES what Munim does (stop messages, raise a dispute) takes effect immediately;
anything that touches money or promises is forwarded to the shopkeeper as a pending question and never auto-applied.
Linking via the invite IS the customer's own consent (recorded as 'customer_self_linked'), which is stronger than
the shopkeeper attesting it. Replies are Hindi or English by the customer's language, text + optional Timbre voice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from munim.insights.statement import build as build_statement
from munim.scribe.extract import extract
from munim.scribe.indic_numbers import format_inr, paise_to_words, paise_to_words_en
from munim.scribe.names import Resolution
from munim.service import Reply

STOP = re.compile(r"\b(ruko|stop|band\s*karo|mat\s*bhejo|unsubscribe|nahi\s*chahiye|opt\s*out)\b", re.I)
WRONG = re.compile(r"\b(galat|ghalat|wrong|dispute|maine\s*nahi|nahi\s*liya|mistake)\b", re.I)
ASK_BAL = re.compile(r"\b(kitna|baaki|balance|hisaab|hisab|statement|bakaya|how\s+much|owe)\b", re.I)

SUPPORTED_REPLY = {"hi", "en"}


@dataclass
class PortalResult:
    text: str
    voice: str | None = None
    tts_language: str = "hi-en"
    owner_msgs: list[Reply] = field(default_factory=list)       # to deliver to the shop's owner chat(s)


def welcome(shop: str, name: str, lang: str) -> PortalResult:
    if lang == "en":
        return PortalResult(
            f"Hello {name}! You are now linked to {shop} on Munim (AI assistant). You can ask “how much do I owe?”, "
            "say “wrong” if something looks incorrect, or “stop” to stop reminders. Your consent to receive messages is recorded.",
            f"Hello {name}. You are linked to {shop}. Ask how much you owe, or say stop at any time.", "en-IN")
    return PortalResult(
        f"Namaste {name} ji! Aap {shop} se Munim (AI sahayak) par jud gaye. Poochh sakte hain: “mera kitna baaki hai?”. "
        "Kuch galat lage to “galat” likhein, sandesh band karne ke liye “ruko”. Sandesh paane ki aapki sehmati darj ho gayi hai.",
        f"Namaste {name} ji. Aap {shop} se jud gaye. Apna hisaab poochh sakte hain, aur kabhi bhi ruko bol sakte hain.", "hi-en")


def handle(tenant, customer_id: int, text: str, *, owner_chat: int | None, detected_lang: str | None = None) -> PortalResult:
    led, svc = tenant.ledger, tenant.service
    row = led.customer_row(customer_id)
    if detected_lang:                                    # learn the customer's language from what they actually spoke
        code = detected_lang.split("-")[0].lower()
        if code and code != (row["lang"] or "hi"):
            led.set_flag(customer_id, lang=code)
            row = led.customer_row(customer_id)
    lang = "en" if (row["lang"] or "hi") == "en" else "hi"
    name = row["name"]
    en = lang == "en"
    tts = "en-IN" if en else "hi-en"
    custs = [c for c in led.customers() if c.id == customer_id]
    cust = custs[0]

    if STOP.search(text):
        led.revoke_consent(customer_id)
        try:
            from munim.payments.mandate import Engine, SandboxRail
            Engine(tenant, SandboxRail()).revoke_customer(customer_id, "customer")
        except Exception:
            pass
        msg = ("Done — you will not get reminders any more." if en else "Theek hai — ab aapko koi reminder nahi aayega.")
        owner = [Reply(f"🔕 {name} ne khud sandesh band karne ko kaha (portal). Reminder band kar diye.")]
        return PortalResult(msg, msg if not en else msg, tts, owner)
    if WRONG.search(text):
        led.open_dispute(customer_id, None, "raised by customer in portal")
        msg = ("Noted. The shop has been told your account looks wrong; no reminders until it is sorted out." if en
               else "Darj kar liya. Dukaan ko bata diya gaya hai ki aapko hisaab galat lag raha hai; suljhne tak koi reminder nahi aayega.")
        owner = [Reply(f"⚠️ {name} ko hisaab galat lag raha hai (portal). Vivaad darj; /dispute {name.split()[0]} se saboot dekhein.")]
        return PortalResult(msg, msg, tts, owner)

    from munim.payments import mandate_portal
    mp = mandate_portal.customer_message(tenant, customer_id, text, lang)
    if mp is not None:
        return mp
    from munim.agent import negotiator
    neg = negotiator.respond(tenant, customer_id, text, lang=lang)
    if neg is not None:
        owner = [Reply(n) for n in neg.owner_notes] + ([Reply(neg.escalate)] if neg.escalate else [])
        return PortalResult(neg.text, neg.voice, neg.tts_language, owner)
    ex = extract(text, custs, svc._today())
    ex.resolution = Resolution("match", cust, [(cust, 100.0)], None, 100.0)
    if ex.intent == "promise_to_pay" and ex.when is not None and owner_chat is not None:
        from munim.audit.auditor import AuditResult
        res = AuditResult("ACT", [], "promise_to_pay", cust, ex.amount_paise)
        owner_reply = svc._ask_promise(owner_chat, res, ex, {"src": "customer_portal"}, svc._clock())
        msg = ("Thank you. I have told the shop about your promise; they will confirm." if en
               else "Shukriya. Aapka vaada dukaan ko bata diya hai; woh confirm karenge.")
        return PortalResult(msg, msg, tts, [owner_reply])
    if ex.intent == "record_payment" and ex.amount_paise and owner_chat is not None:
        owner_reply = svc.on_customer_message(owner_chat, customer_id, text)
        msg = ("Thanks. I have asked the shop to confirm they received it." if en
               else "Shukriya. Dukaan se poochh liya hai ki paise mile ya nahi; woh confirm karenge.")
        return PortalResult(msg, msg, tts, [owner_reply])
    if ASK_BAL.search(text) or ex.intent == "balance_query":
        st = build_statement(led, customer_id, svc._clock(), tenant.name, self_service=True)
        if st.ok:
            return PortalResult(st.text, st.voice_text, st.tts_language)
        bal = led.balance(customer_id)
        msg = (f"Your outstanding is {format_inr(max(bal, 0))}." if en else f"Aapka baaki {format_inr(max(bal, 0))} hai.")
        v = (f"Your outstanding is {paise_to_words_en(max(bal, 0))}." if en else f"Aapka baaki {paise_to_words(max(bal, 0))} hai.")
        return PortalResult(msg, v, tts)
    msg = ("I can tell you your balance (“how much do I owe?”), or you can say “wrong” or “stop”." if en
           else "Main aapka hisaab bata sakta hoon (“mera kitna baaki hai?”). Kuch galat lage to “galat”, band karne ke liye “ruko” likhein.")
    return PortalResult(msg, None, tts)
