"""Customer statement: a short, plain-language account summary a shopkeeper can forward (text + Timbre voice note).

Customer-facing, so it passes the same respect rules as reminders: never for erased / opted-out / disputed accounts,
and (unless the customer asked for it) only with recorded consent. Discloses the AI sender. Amounts in the voice
version are words (TTS mispronounces digits).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from munim.insights import engine as E
from munim.ledger.chain import Ledger
from munim.scribe.indic_numbers import format_inr, paise_to_words, paise_to_words_en, rupees_to_words, rupees_to_words_en
from munim.timeutil import IST


@dataclass
class Statement:
    ok: bool
    text: str = ""
    voice_text: str = ""
    tts_language: str = "hi-en"
    reason: str = ""


REFUSE_HI = {
    "erased": "customer ka data hata diya gaya hai", "opted_out": "customer ne sandesh band karne ko kaha hai",
    "disputed": "khate par vivaad khula hai — pehle suljhayein", "no_consent": "sehmati darj nahi (agar customer ne khud maanga ho to 'asked' likhein)",
    "no_balance_or_history": "khate mein koi entry nahi",
}


def build(ledger: Ledger, cid: int, now: float, shop: str, *, asked: bool = False, n: int = 6,
          self_service: bool = False) -> Statement:
    """self_service=True: the customer is reading THEIR OWN account in the customer portal — always allowed (except erased)."""
    row = ledger.customer_row(cid)
    if row["erased"]:
        return Statement(False, reason="erased")
    if not self_service and (row["opted_out"] or row["consent"] == "revoked"):
        return Statement(False, reason="opted_out")
    if not self_service and ledger.is_disputed(cid):
        return Statement(False, reason="disputed")
    if row["consent"] != "granted" and not asked and not self_service:
        return Statement(False, reason="no_consent")
    ents = E.live_money_entries(ledger, cid)
    if not ents:
        return Statement(False, reason="no_balance_or_history")
    name, lang = row["name"], (row["lang"] or "hi")
    bal = ledger.balance(cid)
    en = lang == "en"
    today = datetime.fromtimestamp(now, IST).strftime("%d %b %Y")
    lines = []
    for e in ents[-n:]:
        d = datetime.fromtimestamp(e.ts, IST).strftime("%d %b")
        kind = ("credit given" if en else "udhaar") if e.kind == "add_udhaar" else ("paid" if en else "jama")
        lines.append(f"• {d}: {kind} {format_inr(e.amount_paise)}")
    pays = [e for e in ents if e.kind == "record_payment"]
    if en:
        text = (f"{shop} — account summary for {name} ({today})\n" + "\n".join(lines)
                + f"\nOutstanding: {format_inr(max(bal, 0))}\nPrepared by Munim, {shop}'s AI assistant. "
                  "If anything looks wrong, tell the shop or reply \"wrong\"; reply \"stop\" to stop these messages.")
        voice = (f"Hello {name}. This is Munim, an AI assistant for {shop}. Your account shows "
                 f"{paise_to_words_en(max(bal, 0))} outstanding.")
        if pays:
            d = int((now - pays[-1].ts) // 86400)
            voice += f" Your last payment was {rupees_to_words_en(d)} days ago, {paise_to_words_en(pays[-1].amount_paise)}."
        voice += " If anything looks wrong, please tell the shop."
        return Statement(True, text, voice, "en-IN")
    text = (f"{shop} — {name} ji ka khata ({today})\n" + "\n".join(lines)
            + f"\nBaaki: {format_inr(max(bal, 0))}\nYeh Munim (dukaan ke AI sahayak) ne banaya hai. Kuch galat lage to dukaan par "
              "bataiye ya \"galat\" likhiye; sandesh band karne ke liye \"ruko\".")
    voice = (f"Namaste {name} ji. Yeh {shop} ki taraf se Munim, ek AI sahayak bol raha hai. "
             f"Aapke khate mein {paise_to_words(max(bal, 0))} baaki dikh rahe hain.")
    if pays:
        d = int((now - pays[-1].ts) // 86400)
        voice += f" Aapki aakhri payment {rupees_to_words(d)} din pehle, {paise_to_words(pays[-1].amount_paise)} ki thi."
    voice += " Kuch galat lage to dukaan par bata dijiye."
    return Statement(True, text, voice, "hi-en")
