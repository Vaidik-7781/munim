"""Gnani (Vachana) HTTP client.

Guarantees (see PRD §Gnani rules):
  * ONE request in flight per key (Gnani returns 429 beyond that) — enforced by a
    priority gate; interactive (voice-note) calls jump ahead of batch (benchmark) calls.
  * Retries on 429/500/502/503 with 0.5/1/2 s + jitter; 504 retried once.
  * 401/403 are fatal; 402 means credits exhausted.
  * Fresh X-API-Request-ID per attempt; every call is logged to the credit meter.
  * The API key is only ever sent as the X-API-Key-ID header and never logged.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx

log = logging.getLogger("munim.gnani")

PRIORITY_INTERACTIVE = 0
PRIORITY_NORMAL = 5
PRIORITY_BATCH = 9


class GnaniError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class GnaniAuthError(GnaniError):
    """401/403 — fatal, do not retry."""


class GnaniCreditsExhausted(GnaniError):
    """402 — out of credits."""


class GnaniBadRequest(GnaniError):
    """400/422 — our request is wrong (e.g. voice/model mismatch); do not retry."""


class GnaniUnavailable(GnaniError):
    """Retries exhausted (429/5xx/timeouts)."""


class CreditBudgetGuard(GnaniError):
    """Internal meter says we are at/below the configured floor."""


@dataclass
class CreditMeter:
    """Internal estimate. Real per-call cost is calibrated by scripts/g0_check.py."""

    remaining: float = 5000.0
    floor: float = 300.0
    cost_per_call: dict[str, float] = field(default_factory=lambda: {"stt": 1.0, "tts": 1.0})
    calls: list[dict[str, Any]] = field(default_factory=list)

    def check(self, kind: str, priority: int) -> None:
        # Floor protects the demo budget: only interactive calls may dip below it.
        if self.remaining - self.cost_per_call.get(kind, 1.0) < self.floor and priority > PRIORITY_INTERACTIVE:
            raise CreditBudgetGuard(
                f"credit floor reached ({self.remaining:.0f} left, floor {self.floor:.0f})"
            )
        if self.remaining <= 0:
            raise GnaniCreditsExhausted("internal credit meter at zero", 402)

    def record(self, kind: str, request_id: str, status: int, seconds: float) -> None:
        cost = self.cost_per_call.get(kind, 1.0) if 200 <= status < 300 else 0.0
        self.remaining -= cost
        self.calls.append(
            {"t": time.time(), "kind": kind, "request_id": request_id, "status": status,
             "seconds": round(seconds, 3), "cost": cost, "remaining": self.remaining}
        )

    def summary(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        for c in self.calls:
            by_kind[c["kind"]] = by_kind.get(c["kind"], 0) + 1
        return {"remaining": self.remaining, "calls": len(self.calls), "by_kind": by_kind}


class PriorityGate:
    """Single-slot async gate with priorities (lower number = served first, FIFO within)."""

    def __init__(self) -> None:
        self._busy = False
        self._heap: list[tuple[int, int, asyncio.Future]] = []
        self._counter = itertools.count()

    async def acquire(self, priority: int) -> None:
        if not self._busy and not self._heap:
            self._busy = True
            return
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (priority, next(self._counter), fut))
        try:
            await fut
        except asyncio.CancelledError:
            if not fut.done():
                fut.cancel()
            raise

    def release(self) -> None:
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(None)  # hand the slot straight to the next waiter
                return
        self._busy = False

    @property
    def waiting(self) -> int:
        return sum(1 for _, _, f in self._heap if not f.done())


RETRY_STATUSES = {429, 500, 502, 503}
BACKOFF = (0.5, 1.0, 2.0)


class GnaniClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.vachana.ai",
        *,
        meter: Optional[CreditMeter] = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout: float = 30.0,
    ) -> None:
        self._key = api_key
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout)
        self._gate = PriorityGate()
        self._sleep = sleep
        self.meter = meter or CreditMeter()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def queue_depth(self) -> int:
        return self._gate.waiting

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key-ID": self._key, "X-API-Request-ID": uuid.uuid4().hex}

    async def _request(
        self, kind: str, method: str, path: str, priority: int, **kwargs: Any
    ) -> httpx.Response:
        if not self._key:
            raise GnaniAuthError("GNANI_API_KEY_ID is not set", 401)
        self.meter.check(kind, priority)
        await self._gate.acquire(priority)
        try:
            attempt = 0
            gateway_retried = False
            while True:
                headers = self._headers()
                rid = headers["X-API-Request-ID"]
                t0 = time.perf_counter()
                try:
                    resp = await self._http.request(method, path, headers=headers, **kwargs)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    self.meter.record(kind, rid, 0, time.perf_counter() - t0)
                    if attempt >= len(BACKOFF):
                        raise GnaniUnavailable(f"transport failure: {exc!r}") from exc
                    await self._sleep(BACKOFF[attempt] + random.uniform(0, 0.25))
                    attempt += 1
                    continue
                self.meter.record(kind, rid, resp.status_code, time.perf_counter() - t0)
                st = resp.status_code
                if 200 <= st < 300:
                    return resp
                body = resp.text[:500]
                if st in (401, 403):
                    raise GnaniAuthError(f"auth failed ({st})", st, body)
                if st == 402:
                    raise GnaniCreditsExhausted("credits exhausted (402)", st, body)
                if st in (400, 422):
                    raise GnaniBadRequest(f"bad request ({st}): {body}", st, body)
                if st == 504:
                    if gateway_retried:
                        raise GnaniUnavailable("gateway timeout (504) twice", st, body)
                    gateway_retried = True
                    await self._sleep(BACKOFF[0] + random.uniform(0, 0.25))
                    continue
                if st in RETRY_STATUSES:
                    if attempt >= len(BACKOFF):
                        raise GnaniUnavailable(f"gave up after {attempt + 1} tries ({st})", st, body)
                    await self._sleep(BACKOFF[attempt] + random.uniform(0, 0.25))
                    attempt += 1
                    continue
                raise GnaniError(f"unexpected status {st}", st, body)
        finally:
            self._gate.release()

    # --- raw endpoints ---------------------------------------------------------------

    async def stt_raw(
        self,
        audio: bytes,
        *,
        language_code: str,
        model: str | None = "prisma-v3.0",
        fmt: str = "transcribe",
        keyterms: list[str] | None = None,
        topic: str | None = None,
        previous_text: str | None = None,
        filename: str = "audio.wav",
        priority: int = PRIORITY_INTERACTIVE,
        extra: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        data: dict[str, str] = {"language_code": language_code}
        if model:
            data["model"] = model
            data["format"] = fmt
        if keyterms:
            data["keyterms"] = json.dumps(keyterms[:200], ensure_ascii=False)
        if topic:
            data["topic"] = topic
        if previous_text:
            data["previous_text"] = previous_text
        if extra:
            data.update(extra)
        files = {"audio_file": (filename, audio, "application/octet-stream")}
        resp = await self._request("stt", "POST", "/stt/v3", priority, data=data, files=files)
        return resp.json()

    async def tts_raw(
        self,
        text: str,
        *,
        voice: str,
        language: str = "hi-en",
        model: str = "timbre-v3.0",
        speed: float = 1.0,
        container: str = "ogg",
        priority: int = PRIORITY_INTERACTIVE,
    ) -> bytes:
        speed = max(0.85, min(1.15, speed))
        payload = {
            "text": text,
            "voice": voice,
            "model": model,
            "language": language,
            "speed": speed,
            "audio_config": {"container": container},
        }
        resp = await self._request(
            "tts", "POST", "/api/v1/tts/inference", priority, json=payload
        )
        return resp.content
