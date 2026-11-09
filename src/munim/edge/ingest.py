"""Edge-mic ingest: post-wake-word clips from a cheap ESP32 mic -> Prisma -> ledger PROPOSAL the owner taps once.

UNVERIFIED: no hardware was built or tested; the firmware in edge/firmware is a reference sketch without a trained wake-word.
Privacy design (enforced + tested; rationale in docs/EDGE.md). The blueprint's "continuously streams ambient counter
interactions" was REJECTED: streaming customers' conversations to a cloud is a DPDP/consent problem and a cost problem. Instead:
  (a) the device sends only short clips (<= 12 s) that START after an on-device wake word; clips without `wake_ms` are refused;
  (b) audio is processed in memory and never stored; (c) the transcript must start with the wake word AND contain a ledger
  intent/amount, otherwise it is dropped and ONLY an anonymous counter increments; (d) accepted utterances go through the
  normal two-ear Auditor with src="edge", which forces an owner confirm (no silent ledger writes); (e) owner mute switch,
  per-device rate limit, signed + replay-protected requests, revocable devices.
"""
from __future__ import annotations

import re
import struct
from typing import Awaitable, Callable

from fastapi import APIRouter, Header, HTTPException, Request

from munim.edge.devices import DeviceBook
from munim.scribe.extract import extract
from munim.scribe.indic_numbers import parse_amount

WAKE = re.compile(r"^\s*(hey\s+|ok\s+)?(munim|munimji|munim\s*ji|मुनीम\s*जी|मुनीम)\b[\s,.:!-]*", re.I)
MAX_SECONDS = 12.0
LEDGER_INTENTS = {"add_udhaar", "record_payment", "balance_query", "promise_to_pay"}


def wav_seconds(b: bytes) -> float | None:
    if len(b) < 44 or b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        return None
    try:
        rate, = struct.unpack_from("<I", b, 24)
        bits, = struct.unpack_from("<H", b, 34)
        ch, = struct.unpack_from("<H", b, 22)
        return (len(b) - 44) / max(1, rate * ch * bits // 8)
    except struct.error:
        return None


def segregate(transcript: str, customers) -> tuple[str, str | None]:
    """-> ('accept', command_text) | ('drop_no_wake', None) | ('drop_no_intent', None)."""
    m = WAKE.match(transcript)
    if not m:
        return "drop_no_wake", None
    cmd = transcript[m.end():].strip()
    ex = extract(cmd, customers)
    if ex.intent in LEDGER_INTENTS and (ex.amount_paise or ex.intent in ("balance_query", "promise_to_pay")):
        return "accept", cmd
    if parse_amount(cmd) and ex.intent in ("add_udhaar", "record_payment"):
        return "accept", cmd
    return "drop_no_intent", None


Notify = Callable[[int, object], Awaitable[None]]


def make_router(registry, client=None, listener_fn=None, notify: Notify | None = None, devices: DeviceBook | None = None,
                clock=None) -> APIRouter:
    import time
    book = devices or DeviceBook(registry, "edge", clock or time.time)
    router = APIRouter()
    router.edge_devices = book                                          # type: ignore[attr-defined]

    async def default_listener(audio: bytes, tenant):
        from munim.listen.listener import listen
        return await listen(client, audio, tenant.ledger.customers())

    @router.post("/v1/edge/clip")
    async def clip(request: Request, x_edge_device: str = Header(""), x_edge_ts: int = Header(0), x_edge_sig: str = Header(""),
                   wake_ms: int = 0, peak_db: float = 0.0):
        body = await request.body()
        if len(body) > 600_000:
            raise HTTPException(413, "clip too large")
        row, why = book.authenticate(x_edge_device, x_edge_ts, body, x_edge_sig)
        if row is None:
            raise HTTPException(429 if why == "rate" else 401, why)
        shop = row["shop_id"]
        sec = wav_seconds(body)
        if row["muted"]:
            book.count(shop, "dropped_muted")
            return {"status": "muted"}
        if not wake_ms or sec is None or sec > MAX_SECONDS:
            book.count(shop, "rejected")
            raise HTTPException(400, "device must send post-wake-word clips up to 12 s (WAV PCM16)")
        t = registry.tenant(shop)
        heard = await (listener_fn or default_listener)(body, t)
        a = heard.a.transcript if heard and heard.a else ""
        verdict, cmd = segregate(a, t.ledger.customers())
        if verdict != "accept":
            book.count(shop, verdict)
            return {"status": "dropped"}                                # nothing about the clip is retained
        owners = registry.chats(shop, "owner")
        if not owners:
            book.count(shop, "rejected")
            return {"status": "no_owner"}
        b = heard.b.transcript if heard.b else None
        b_cmd = segregate(b, t.ledger.customers())[1] if b else None
        reply = t.service.on_heard(owners[0], cmd, b_cmd, snr=heard.prepared.snr_db, dur=heard.prepared.seconds, src="edge")
        book.count(shop, "accepted")
        if notify:
            await notify(owners[0], reply)
        return {"status": "accepted", "needs_owner_tap": bool(reply.buttons)}

    return router
