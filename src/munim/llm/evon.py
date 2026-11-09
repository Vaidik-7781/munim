"""Sovereign-LLM adapter (Gnani Evon v3.3 or any local/VPS OpenAI-compatible server). UNVERIFIED: Evon's API is unknown to us;
we assume the OpenAI-compatible /chat/completions shape and make URL, key and model env-configurable
(MUNIM_LLM_PROVIDER=groq|evon|local, MUNIM_LLM_BASE_URL, MUNIM_LLM_API_KEY, MUNIM_LLM_MODEL).

Why it matters: with an on-prem/private endpoint, transcripts and photos never reach a third-party API, so `privacy_mode()`
lets the owner switch on transcript-span extraction and khata/invoice scanning WITHOUT the third-party-disclosure warning.
It does not change any guard: every LLM output is still validated and falls back to deterministic code.
"""
from __future__ import annotations

import os
from enum import Enum

from .client import LLMClient


class Provider(str, Enum):
    GROQ = "groq"
    EVON = "evon"
    LOCAL = "local"


def provider() -> Provider:
    try:
        return Provider((os.environ.get("MUNIM_LLM_PROVIDER") or "groq").lower())
    except ValueError:
        return Provider.GROQ


def privacy_mode(p: Provider | None = None) -> str:
    return "sovereign" if (p or provider()) in (Provider.EVON, Provider.LOCAL) else "third_party"


def build_llm_client(cfg) -> LLMClient:
    p = provider()
    if p is Provider.GROQ:
        return LLMClient(cfg.groq_key, model=cfg.llm_model)
    base = os.environ.get("MUNIM_LLM_BASE_URL", "")
    if not base:
        raise ValueError("MUNIM_LLM_BASE_URL is required for evon/local")
    return LLMClient(os.environ.get("MUNIM_LLM_API_KEY") or "local", base_url=base, model=os.environ.get("MUNIM_LLM_MODEL") or cfg.llm_model)
