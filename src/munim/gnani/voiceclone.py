"""Voice cloning (`vachana-vc-v1`): the shopkeeper's OWN voice for reminders — consent-gated, revocable, never for content changes.

UNVERIFIED: Gnani's cloning API shape is unknown to us. Path/field names are configurable (GNANI_VC_PATH, default /api/v1/vc/clone;
GNANI_VC_TTS_PATH default /api/v1/vc/tts) and the response is expected to carry a voice id (`voice_id`). Product rules enforced here:
  (a) cloning needs a recorded explicit-consent clip + a stored consent record (sha256 of the clip, scope, timestamp);
  (b) only the shopkeeper's own voice — the consent clip must be spoken by the person enrolling (checked by the owner's attestation;
      we cannot verify identity from audio, and say so);
  (c) a cloned voice only changes HOW text is spoken: content still comes from govern.genreminder/governor, must contain the AI
      disclosure token, and the customer is told a synthetic voice is used (a cloned voice that hides that it is synthetic is deceptive);
  (d) revoke = delete the voice id remotely (best effort) and locally, instantly falling back to a stock Timbre voice;
  (e) any failure falls back to the stock voice — a reminder never fails because cloning did.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

import httpx

from .client import GnaniClient, GnaniError
from .tts import Speaker

DISCLOSURE = re.compile(r"\bAI\b")
CONSENT_PHRASE_HI = "Main apni awaaz Munim ko apne customers ke liye reminder bolne ke liye istemaal karne ki ijaazat deta hoon."


@dataclass
class VoiceConsent:
    shop_id: str
    sha256: str
    scope: str
    ts: float
    voice_id: str | None = None


class VoiceClone:
    def __init__(self, client: GnaniClient, aux, speaker: Speaker, clock, transport: httpx.AsyncBaseTransport | None = None):
        self.client, self.aux, self.speaker, self.clock, self._tr = client, aux, speaker, clock, transport
        self.path = os.environ.get("GNANI_VC_PATH") or "/api/v1/vc/clone"
        self.tts_path = os.environ.get("GNANI_VC_TTS_PATH") or "/api/v1/vc/tts"

    def consent(self) -> VoiceConsent | None:
        d = self.aux.get("vc_consent")
        return VoiceConsent(**d) if d else None

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.client._http.base_url, transport=self._tr, timeout=30,
                                 headers={"X-API-Key-ID": self.client._key})

    async def enroll(self, shop_id: str, sample: bytes, consent_clip: bytes, owner_attests_own_voice: bool) -> VoiceConsent:
        if not owner_attests_own_voice:
            raise PermissionError("only the shopkeeper's own voice may be cloned")
        if len(consent_clip) < 2000 or len(sample) < 20000:
            raise ValueError("need a consent clip and at least ~10 s of sample audio")
        async with self._http() as h:
            r = await h.post(self.path, files={"audio_file": ("sample.wav", sample, "application/octet-stream"),
                                               "consent_file": ("consent.wav", consent_clip, "application/octet-stream")},
                             data={"model": "vachana-vc-v1"})
        if r.status_code != 200 or not r.json().get("voice_id"):
            raise GnaniError(f"voice clone failed ({r.status_code})", r.status_code)
        c = VoiceConsent(shop_id, hashlib.sha256(consent_clip).hexdigest(), "reminders_to_own_customers", self.clock(), r.json()["voice_id"])
        self.aux.put("vc_consent", c.__dict__)
        return c

    async def revoke(self) -> None:
        c = self.consent()
        self.aux.put("vc_consent", None)                                   # local first: fallback is instant
        if c and c.voice_id:
            try:
                async with self._http() as h:
                    await h.delete(f"{self.path}/{c.voice_id}")
            except httpx.HTTPError:
                pass

    async def speak_reminder(self, text: str, language: str = "hi-en") -> tuple[bytes | None, str]:
        """-> (audio, voice_kind). Refuses text without the AI disclosure; falls back to stock voice on any problem."""
        if not DISCLOSURE.search(text):
            raise ValueError("reminder text must carry the AI disclosure")
        c = self.consent()
        if c and c.voice_id:
            try:
                async with self._http() as h:
                    r = await h.post(self.tts_path, json={"text": text, "voice_id": c.voice_id, "language": language, "model": "vachana-vc-v1"})
                if r.status_code == 200 and r.content:
                    return r.content, "cloned"
            except httpx.HTTPError:
                pass
        return await self.speaker.speak(text, language=language), "stock"
