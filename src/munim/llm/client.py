"""Minimal OpenAI-compatible chat client (default: Groq, as disclosed in the PRD).

Privacy rules enforced by callers, not by hope:
  * the AGENT planner is only ever given pseudonymous ids + numeric facts (no names, no phone numbers);
  * transcript span extraction (which contains names) is OFF unless MUNIM_LLM_SPANS=1 and is disclosed in the README;
  * temperature 0, JSON mode, small max_tokens, hard timeout; any failure => deterministic fallback.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, api_key: str, base_url: str = "https://api.groq.com/openai/v1",
                 model: str = "llama-3.3-70b-versatile", *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 20.0) -> None:
        self.model = model
        self.vision_model = os.environ.get("MUNIM_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        self._key = api_key
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout)
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def chat_json_image(self, system: str, user_text: str, image: bytes, mime: str = "image/jpeg",
                              max_tokens: int = 1200, model: str | None = None) -> dict[str, Any]:
        """Vision variant (OpenAI-compatible image_url data URI). The caller must have the owner's opt-in: the photo leaves the machine."""
        import base64
        if not self._key:
            raise LLMError("no LLM key configured")
        if len(image) > 4_000_000:
            raise LLMError("image too large")
        uri = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        body = {"model": model or self.vision_model, "temperature": 0, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": [{"type": "text", "text": user_text},
                                                          {"type": "image_url", "image_url": {"url": uri}}]}]}
        try:
            r = await self._http.post("/chat/completions", json=body, headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as exc:
            raise LLMError(f"transport: {exc!r}") from exc
        self.calls += 1
        if r.status_code != 200:
            raise LLMError(f"status {r.status_code}")
        try:
            return json.loads(r.json()["choices"][0]["message"]["content"])
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise LLMError("bad response") from exc

    async def chat_json(self, system: str, user: str, max_tokens: int = 400) -> dict[str, Any]:
        if not self._key:
            raise LLMError("no LLM key configured")
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            r = await self._http.post("/chat/completions", json=body, headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as exc:
            raise LLMError(f"transport: {exc!r}") from exc
        self.calls += 1
        if r.status_code != 200:
            raise LLMError(f"status {r.status_code}")
        try:
            content = r.json()["choices"][0]["message"]["content"]
            out = json.loads(content)
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError("malformed response") from exc
        if not isinstance(out, dict):
            raise LLMError("not a JSON object")
        return out
