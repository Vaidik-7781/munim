"""Live-call state machine for customers without smartphones: AI disclosure -> consent to talk -> balance -> negotiate -> recap.

Transport-agnostic and side-effect-light: feed it each customer utterance (from streaming STT) and speak what it returns (Timbre
via Speaker). It never writes money. The only writes are what the negotiator already does inside the owner's policy (promise
entries) and an opt-out when the customer says stop. NOT included: the telephony bridge (Exotel/Twilio/SIP) — TODO; the call
must be placed by the shop's own number with TRAI/DLT rules respected; this class assumes that is handled outside.
Rules: Governor check first (consent, 08–19 IST, 72 h gap, 3/30 d, dispute); AI disclosure in the very first sentence; max 4
customer turns; stop words end the call at once and opt the customer out; amounts spoken as words, never digits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from munim.agent import negotiator
from munim.govern.governor import check
from munim.scribe.indic_numbers import paise_to_words

STOP = re.compile(r"\b(ruko|stop|band\s*karo|mat\s*karo|phone\s*mat|nahi\s*chahiye)\b", re.I)
YES = re.compile(r"\b(haan|han|ha|yes|boliye|bolo|ji|theek)\b", re.I)
NO = re.compile(r"\b(nahi|no|abhi\s*nahi|busy)\b", re.I)
MAX_TURNS = 4


@dataclass
class Say:
    text: str
    language: str = "hi-en"


@dataclass
class CallAgent:
    tenant: object
    customer_id: int
    hook: Callable = negotiator.respond
    state: str = "new"
    turns: int = 0
    recorded: list[str] = field(default_factory=list)
    owner_notes: list[str] = field(default_factory=list)
    done: bool = False

    def _name(self) -> str:
        return self.tenant.ledger.customer_row(self.customer_id)["name"].split()[0]

    def start(self, now: float | None = None) -> list[Say]:
        t = self.tenant
        d = check(t.ledger, t.cfg, self.customer_id, now if now is not None else t._clock())
        if not d.allowed:
            self.done, self.state = True, "refused"
            return []
        t.ledger.log_reminder(self.customer_id, "call")
        self.state = "asked_consent"
        return [Say(f"Namaste {self._name()} ji, main {t.name} ka Munim hoon — ek AI sahayak, insaan nahi. Kya aap do minute baat kar sakte hain?")]

    def _end(self, text: str) -> list[Say]:
        self.done, self.state = True, "ended"
        return [Say(text)]

    def on_utterance(self, text: str) -> list[Say]:
        if self.done:
            return []
        L = self.tenant.ledger
        if STOP.search(text):
            L.revoke_consent(self.customer_id)
            self.owner_notes.append(f"🔕 {L.customer_row(self.customer_id)['name']} ne call par rokne ko kaha: reminder band.")
            return self._end("Theek hai, ab aapko phone nahi aayega. Shukriya.")
        self.turns += 1
        if self.turns > MAX_TURNS:
            return self._end("Aapka waqt dene ke liye shukriya. Dukaan aapse sampark karegi. Namaste.")
        if self.state == "asked_consent":
            if NO.search(text) and not YES.search(text):
                return self._end("Koi baat nahi, main baad mein koshish nahi karunga bina poochhe. Namaste.")
            self.state = "negotiating"
            bal = max(0, L.balance(self.customer_id))
            return [Say(f"Aapka {paise_to_words(bal)} baaki hai. Aap kab tak de sakte hain?")]
        res = self.hook(self.tenant, self.customer_id, text, lang="hi")
        if res is None:
            self.owner_notes.append(f"📞 {L.customer_row(self.customer_id)['name']} ne call par kaha: “{text[:100]}”. Maine kuch tay nahi kiya.")
            return self._end("Maine aapki baat dukaan ko bata di hai, woh aapse baat karenge. Shukriya.")
        self.recorded += res.recorded
        self.owner_notes += res.owner_notes + ([res.escalate] if res.escalate else [])
        if res.recorded:
            return self._end((res.voice or res.text) + " Shukriya, namaste.")
        return [Say(res.voice or res.text)]
