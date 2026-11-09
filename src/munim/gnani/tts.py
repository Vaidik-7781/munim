"""TTS presets + a credit-aware speaker.

Rules baked in (PRD §Gnani rules):
  * amounts are ALWAYS words before TTS (the caller passes voice_text built by paise_to_words);
  * voices are model-specific — Timbre v3.0 voices only;
  * any Gnani failure degrades to text-only; it never breaks a ledger write.
"""
from __future__ import annotations

import logging
import re
from collections import OrderedDict

from .client import PRIORITY_INTERACTIVE, GnaniClient, GnaniError

log = logging.getLogger("munim.tts")

TIMBRE_V3_VOICES = {
    "female": ["Shivani", "Lakshmi", "Kalpana", "Chetana", "Vasudha", "Riyaa", "Tarika"],
    "male": ["Avinash", "Shubham", "Prakash", "Devansh", "Yashraj", "Harshad"],
}
DEFAULT_VOICE = "Shivani"
ALL_VOICES = TIMBRE_V3_VOICES["female"] + TIMBRE_V3_VOICES["male"]

_DIGITS = re.compile(r"\d")


class Speaker:
    def __init__(self, client: GnaniClient, voice: str = DEFAULT_VOICE, language: str = "hi-en",
                 cache_size: int = 64):
        if voice not in ALL_VOICES:
            raise ValueError(f"{voice!r} is not a Timbre v3.0 voice")
        self.client, self.voice, self.language = client, voice, language
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_size = cache_size

    def set_voice(self, voice: str) -> None:
        if voice not in ALL_VOICES:
            raise ValueError(f"{voice!r} is not a Timbre v3.0 voice")
        self.voice = voice

    async def speak(self, text: str, speed: float = 1.0, language: str | None = None,
                    voice: str | None = None) -> bytes | None:
        if not text:
            return None
        if _DIGITS.search(text):
            # Defensive: digit strings are mispronounced by TTS. Better silent than wrong money.
            log.warning("refusing to speak text containing digits")
            return None
        lang, vc = language or self.language, voice or self.voice
        if vc not in ALL_VOICES:
            raise ValueError(f"{vc!r} is not a Timbre v3.0 voice")
        key = f"{vc}|{lang}|{speed}|{text}"
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        try:
            audio = await self.client.tts_raw(text, voice=vc, language=lang,
                                              speed=speed, container="ogg",
                                              priority=PRIORITY_INTERACTIVE)
        except GnaniError as exc:
            log.warning("TTS unavailable (%s) — degrading to text", type(exc).__name__)
            return None
        self._cache[key] = audio
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return audio
