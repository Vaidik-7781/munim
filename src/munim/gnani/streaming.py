"""Streaming STT client for the live-call path (wss://api.vachana.ai/stt/v3/stream).

UNVERIFIED: the endpoint comes from the project brief; Gnani's wire protocol is NOT documented to us. `DefaultWire` encodes our
best guess (auth header X-API-Key-ID, a JSON config frame, binary PCM16 16 kHz mono frames, JSON {type,text,is_final} replies, a
{"type":"end"} marker). If the real protocol differs, subclass `Wire` — nothing else changes. Credit accounting goes through the same
CreditMeter as the REST client; any failure surfaces as GnaniError so callers degrade (e.g. fall back to async voice notes).
Needs `pip install websockets` (extra: live).
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

from .client import PRIORITY_INTERACTIVE, GnaniClient, GnaniError

URL = "wss://api.vachana.ai/stt/v3/stream"


@dataclass
class Partial:
    text: str
    is_final: bool
    ts: float = 0.0


class Wire:
    url = URL

    def headers(self, key: str) -> dict[str, str]:
        return {"X-API-Key-ID": key}

    def config(self, language: str, model: str) -> str | None:
        return None

    def audio(self, pcm: bytes) -> bytes | str:
        return pcm

    def end(self) -> str | bytes | None:
        return None

    def decode(self, msg: str | bytes) -> Partial | None:
        return None


class DefaultWire(Wire):
    def config(self, language: str, model: str) -> str:
        return json.dumps({"type": "config", "language_code": language, "model": model, "sample_rate": 16000, "encoding": "pcm16"})

    def end(self) -> str:
        return json.dumps({"type": "end"})

    def decode(self, msg: str | bytes) -> Partial | None:
        try:
            d = json.loads(msg)
        except (TypeError, ValueError):
            return None
        if not isinstance(d, dict) or d.get("type") not in ("partial", "final", "transcript"):
            return None
        return Partial(str(d.get("text", "")), bool(d.get("is_final", d.get("type") == "final")), time.time())


def pcm_rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    s = struct.unpack(f"<{n}h", pcm[: n * 2])
    return (sum(x * x for x in s) / n) ** 0.5


class Endpointer:
    """Energy endpointing: speech started, then `silence_ms` of quiet => utterance over (frames are 20 ms of 16 kHz PCM16)."""
    def __init__(self, threshold: float = 500.0, silence_ms: int = 800, frame_ms: int = 20):
        self.th, self.need, self.frame_ms = threshold, silence_ms // frame_ms, frame_ms
        self.spoke, self.quiet = False, 0

    def push(self, pcm: bytes) -> bool:
        if pcm_rms(pcm) >= self.th:
            self.spoke, self.quiet = True, 0
        elif self.spoke:
            self.quiet += 1
        return self.spoke and self.quiet >= self.need


Connect = Callable[..., Awaitable]


class StreamingSTT:
    def __init__(self, client: GnaniClient, wire: Wire | None = None, connect: Connect | None = None, model: str = "prisma-v3.0"):
        self.client, self.wire, self.model = client, wire or DefaultWire(), model
        self._connect = connect

    async def _open(self):
        if self._connect is not None:
            return await self._connect(self.wire.url, self.wire.headers(self.client._key))
        try:
            import websockets
        except ImportError as exc:                                         # pragma: no cover
            raise GnaniError("streaming needs `pip install websockets`") from exc
        return await websockets.connect(self.wire.url, additional_headers=self.wire.headers(self.client._key), open_timeout=10)

    async def transcribe(self, frames: AsyncIterator[bytes], language: str = "hi-IN") -> AsyncIterator[Partial]:
        self.client.meter.check("stt", PRIORITY_INTERACTIVE)
        rid, t0, ws = uuid.uuid4().hex, time.perf_counter(), None
        for attempt in (0, 1):                                              # reconnect once, only before any audio was sent
            try:
                ws = await self._open()
                break
            except Exception as exc:
                if attempt:
                    self.client.meter.record("stt", rid, 0, time.perf_counter() - t0)
                    raise GnaniError(f"stream connect failed: {type(exc).__name__}") from exc
        cfg = self.wire.config(language, self.model)
        if cfg is not None:
            await ws.send(cfg)

        async def pump():
            try:
                async for f in frames:
                    await ws.send(self.wire.audio(f))
            finally:
                e = self.wire.end()
                if e is not None:
                    await ws.send(e)

        sender = asyncio.create_task(pump())
        try:
            async for msg in ws:
                p = self.wire.decode(msg)
                if p is not None:
                    yield p
                    if p.is_final and sender.done():
                        break
        except Exception as exc:
            raise GnaniError(f"stream failed: {type(exc).__name__}") from exc
        finally:
            sender.cancel()
            self.client.meter.record("stt", rid, 200, time.perf_counter() - t0)
            try:
                await ws.close()
            except Exception:
                pass
