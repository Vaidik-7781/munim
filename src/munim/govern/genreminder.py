"""Generative reminders with hard guardrails.

The LLM may only REWORD a reminder (warmer, more natural Hinglish/English) or TRANSLATE its fixed template into another
Indian language. Facts never pass through the model: name, shop and amounts are inserted by code AFTER generation into
[[TOKENS]]. Output is accepted only if every token survives exactly once, no digits/URLs appear, the AI disclosure and the
way to say stop/wrong are present, and the tone linter passes. Anything else -> the deterministic template. The result is
still only a draft the shopkeeper must approve (and the Governor re-checks at approval time).
Translations are NOT native-reviewed: the owner is told to check them before forwarding.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import yaml

from munim.govern.governor import ReminderDraft
from munim.govern.tone_lint import lint
from munim.llm.client import LLMClient, LLMError
from munim.scribe.indic_numbers import format_inr, paise_to_words_en

_SCRIPTS = os.path.join(os.path.dirname(__file__), "scripts", "gentle.yaml")

INDIC_LANGS = {"ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam", "mr": "Marathi",
               "pa": "Punjabi", "bn": "Bengali", "gu": "Gujarati"}
TOKENS_TEXT = ("[[NAME]]", "[[SHOP]]", "[[AMOUNT]]")
TOKENS_VOICE = ("[[NAME]]", "[[SHOP]]", "[[AMOUNT_WORDS]]")
_STOPWORD = re.compile(r"\b(stop|wrong|ruko|galat)\b", re.I)
_BAD = re.compile(r"https?://|www\.|@|\d")


@dataclass
class Generated:
    draft: ReminderDraft
    mode: str                  # template | rewritten | translated
    note: str = ""             # shown to the shopkeeper (e.g. translation not native-reviewed)


def _tok(s: str) -> str:
    return " ".join(str(s).split()).format(shop="[[SHOP]]", name="[[NAME]]", amount="[[AMOUNT]]",
                                           amount_words="[[AMOUNT_WORDS]]")


def base_templates(lang: str) -> tuple[str, str]:
    with open(_SCRIPTS, encoding="utf-8") as fh:
        sc = yaml.safe_load(fh)
    if lang == "en":
        return _tok(sc["en"]["text"]), _tok(sc["en"]["voice"])
    g = sc["gentle"]
    return _tok(g["hi"]), _tok(g.get("hi_voice", g["hi"]))


def _fill(s: str, *, name: str, shop: str, amount: str, words: str) -> str:
    return (s.replace("[[NAME]]", name).replace("[[SHOP]]", shop)
            .replace("[[AMOUNT_WORDS]]", words).replace("[[AMOUNT]]", amount))


def validate(out: str, tokens: tuple[str, ...], *, need_stopword: bool) -> bool:
    if not isinstance(out, str) or not (40 <= len(out) <= 520):
        return False
    if any(out.count(t) != 1 for t in tokens):
        return False
    stripped = out
    for t in ("[[NAME]]", "[[SHOP]]", "[[AMOUNT]]", "[[AMOUNT_WORDS]]"):
        stripped = stripped.replace(t, "")
    if "[[" in stripped or "]]" in stripped or _BAD.search(stripped):
        return False
    if not re.search(r"\bAI\b", stripped):                 # stricter than the linter: the AI nature must be stated outright
        return False
    if need_stopword and not _STOPWORD.search(stripped):
        return False
    return True


REWRITE_SYS = (
    "You reword a polite payment reminder from a small shop. Rules: keep EVERY [[TOKEN]] exactly once, unchanged; do not add "
    "any facts, amounts, dates, deadlines, interest, threats, urgency or pressure; keep the statement that it is from an AI "
    "assistant and the way to say wrong/stop (text) or to tell the shop (voice). Match the requested style. Reply JSON only: "
    '{"text": "...", "voice": "..."}'
)
TRANSLATE_SYS = (
    "You translate a polite shop payment reminder into {lang}, natural spoken style, respectful. Rules: keep EVERY [[TOKEN]] "
    "exactly once, unchanged; keep the words AI, Munim, stop and wrong in Latin letters (customers reply with them); do not "
    "add facts, numbers, threats or urgency. Reply JSON only: " '{{"text": "...", "voice": "..."}}'
)


class GenReminder:
    def __init__(self, llm: LLMClient):
        self.llm = llm
        self._translations: dict[str, tuple[str, str]] = {}

    async def _ask(self, system: str, text_tpl: str, voice_tpl: str, extra: str = "") -> tuple[str, str] | None:
        if not self.llm.enabled:
            return None
        user = f"{extra}\nTEXT: {text_tpl}\nVOICE: {voice_tpl}"
        try:
            raw = await self.llm.chat_json(system, user, max_tokens=500)
        except LLMError:
            return None
        t, v = raw.get("text"), raw.get("voice")
        return (t, v) if isinstance(t, str) and isinstance(v, str) else None

    async def generate(self, base: ReminderDraft, *, name: str, shop: str, customer_lang: str,
                       style: str = "warm and brief") -> Generated:
        """base = the Governor-approved template draft (already linted). Returns the best SAFE variant."""
        fill = dict(name=name, shop=shop, amount=format_inr(base.amount_paise))
        if customer_lang in INDIC_LANGS:
            return await self._translated(base, customer_lang, fill)
        lang = "en" if customer_lang == "en" else "hi"
        t_tpl, v_tpl = base_templates(lang)
        got = await self._ask(REWRITE_SYS, t_tpl, v_tpl, f"STYLE: {style}. LANGUAGE: {'English' if lang == 'en' else 'Hinglish (Hindi in Latin script)'}")
        if got and validate(got[0], TOKENS_TEXT, need_stopword=True) and validate(got[1], TOKENS_VOICE, need_stopword=False):
            words = paise_to_words_en(base.amount_paise) if lang == "en" else _words_hi(base.amount_paise)
            text = _fill(got[0], words=words, **fill)
            voice = _fill(got[1], words=words, **fill)
            if lint(text).ok and lint(voice).ok:
                return Generated(ReminderDraft(base.customer_id, text, voice, base.amount_paise, lang), "rewritten")
        return Generated(base, "template")

    async def _translated(self, base: ReminderDraft, lang: str, fill: dict) -> Generated:
        if lang not in self._translations:
            t_tpl, v_tpl = base_templates("en")
            got = await self._ask(TRANSLATE_SYS.format(lang=INDIC_LANGS[lang]), t_tpl, v_tpl)
            if not (got and validate(got[0], TOKENS_TEXT, need_stopword=True) and validate(got[1], TOKENS_VOICE, need_stopword=False)):
                return Generated(base, "template", f"{INDIC_LANGS[lang]} anuvaad nahi ban paaya — Hindi/English template use hua.")
            self._translations[lang] = got
        t_tpl, v_tpl = self._translations[lang]
        text = _fill(t_tpl, words="", **fill)
        voice = _fill(v_tpl, words=paise_to_words_en(base.amount_paise), **fill)   # amount spoken as English words (code-mixed)
        d = ReminderDraft(base.customer_id, text, voice, base.amount_paise, lang)
        return Generated(d, "translated", f"⚠️ {INDIC_LANGS[lang]} anuvaad AI ne kiya hai, kisi jaankaar se padhwa kar hi bhejein.")


def _words_hi(paise: int) -> str:
    from munim.scribe.indic_numbers import paise_to_words
    return paise_to_words(paise, "roman")
