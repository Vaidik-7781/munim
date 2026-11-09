"""LLM as a THIRD OPINION on what was said. It can only add doubt, never certainty:

  * it proposes {intent, customer_text, amount_text}; the amount is then recomputed by OUR parser from amount_text;
  * a disagreement with the rule-based reading raises `llm_disagreement` => the shopkeeper is asked;
  * agreement changes nothing (no extra trust), so a hallucinating LLM cannot cause a silent write.
Transcripts contain customer names, so this is OFF by default (MUNIM_LLM_SPANS=1) and disclosed in the README.
"""
from __future__ import annotations

from datetime import date

from munim.llm.client import LLMClient, LLMError

from .extract import INTENTS, Extraction
from .indic_numbers import parse_amounts
from .names import Customer, resolve

SYSTEM = (
    "You label one Hindi/Hinglish sentence spoken by a kirana shopkeeper about a customer's credit (udhaar) ledger. "
    "Return JSON: {\"intent\": one of " + ", ".join(INTENTS) + ", \"customer_text\": the customer's name EXACTLY as spoken or \"\", "
    "\"amount_text\": the money words/digits EXACTLY as spoken or \"\"}. Do NOT convert numbers. Do NOT guess missing parts."
)


class LLMSpans:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def propose(self, transcript: str, customers: list[Customer], today: date | None = None) -> Extraction | None:
        try:
            raw = await self.llm.chat_json(SYSTEM, transcript, max_tokens=120)
        except LLMError:
            return None
        intent = raw.get("intent")
        if intent not in INTENTS:
            return None
        res = resolve(str(raw.get("customer_text") or ""), customers)
        spans = [s for s in parse_amounts(str(raw.get("amount_text") or "")) if s.valid and not s.is_quantity]
        return Extraction(intent, transcript, res, spans, [], ["llm"])
