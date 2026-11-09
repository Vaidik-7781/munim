"""WhatsApp Cloud API (Meta) channel: webhook router that feeds the SAME MunimService as Telegram. UNVERIFIED against live Meta/Gupshup/Twilio.

Security: X-Hub-Signature-256 HMAC on every POST (401 otherwise); message-id replay protection; phone numbers are never stored — a
keyed hash is the principal. Linking: the owner issues a one-time code (`make_link_code`, e.g. via /wa on Telegram) and sends
"link CODE" from WhatsApp; customers are linked only through the existing invite flow (their own consent), not here.
Business-initiated messages need pre-approved templates and fall outside the 24-hour customer-service window — out of scope here (docs/WHATSAPP.md).
Buttons: WhatsApp allows 3 reply buttons (title <= 20 chars) or a list of up to 10 rows; `to_interactive` converts a Reply.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from typing import Awaitable, Callable

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

WA_CHAT_BASE = 8_000_000_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS wa_links(principal TEXT PRIMARY KEY, shop_id TEXT NOT NULL, role TEXT NOT NULL, label TEXT);
CREATE TABLE IF NOT EXISTS wa_seen(msg_id TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS wa_codes(code_hash TEXT PRIMARY KEY, shop_id TEXT, role TEXT, exp REAL, label TEXT);
"""
Send = Callable[[str, dict], Awaitable[None]]


class CloudSender:
    """Real sender (UNVERIFIED): POST /{phone_number_id}/messages with a Bearer token."""
    def __init__(self, token: str, phone_number_id: str, transport: httpx.AsyncBaseTransport | None = None):
        self.token, self.pid, self._tr = token, phone_number_id, transport

    async def __call__(self, to: str, message: dict) -> None:
        async with httpx.AsyncClient(transport=self._tr, timeout=15) as h:
            r = await h.post(f"https://graph.facebook.com/v20.0/{self.pid}/messages", headers={"Authorization": f"Bearer {self.token}"},
                             json={"messaging_product": "whatsapp", "to": to, **message})
            r.raise_for_status()


def to_interactive(text: str, buttons: list[tuple[str, str]]) -> dict:
    if not buttons:
        return {"type": "text", "text": {"body": text[:4000]}}
    if len(buttons) <= 3:
        return {"type": "interactive", "interactive": {"type": "button", "body": {"text": text[:1024]},
                "action": {"buttons": [{"type": "reply", "reply": {"id": d[:256], "title": t[:20]}} for t, d in buttons]}}}
    rows = [{"id": d[:200], "title": t[:24]} for t, d in buttons[:10]]
    return {"type": "interactive", "interactive": {"type": "list", "body": {"text": text[:1024]},
            "action": {"button": "Chunein", "sections": [{"title": "Vikalp", "rows": rows}]}}}


def make_router(registry, send: Send | None = None, verify_token: str = "", app_secret: str = "", on_voice=None, clock=time.time) -> APIRouter:
    db = sqlite3.connect(os.path.join(registry.root, "wa.db"), check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    router = APIRouter()
    sender: Send = send or (lambda to, m: _noop())

    def principal(phone: str) -> str:
        return hmac.new(registry._master, f"wa:{phone}".encode(), hashlib.sha256).hexdigest()

    def chat_id(p: str) -> int:
        return WA_CHAT_BASE + int(p[:8], 16)

    def make_link_code(shop_id: str, role: str = "owner", label: str | None = None) -> str:
        code = secrets.token_hex(3).upper()
        db.execute("INSERT INTO wa_codes VALUES (?,?,?,?,?)", (hashlib.sha256(code.encode()).hexdigest(), shop_id, role, clock() + 900, label))
        return code

    router.make_link_code = make_link_code                              # type: ignore[attr-defined]

    @router.get("/v1/whatsapp")
    async def verify(request: Request):
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and verify_token and hmac.compare_digest(q.get("hub.verify_token", ""), verify_token):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403, "bad verify token")

    @router.post("/v1/whatsapp")
    async def hook(request: Request, x_hub_signature_256: str = Header("")):
        raw = await request.body()
        want = "sha256=" + hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not app_secret or not hmac.compare_digest(x_hub_signature_256, want):
            raise HTTPException(401, "bad signature")
        try:
            data = __import__("json").loads(raw)
            msgs = [m for e in data.get("entry", []) for c in e.get("changes", []) for m in c.get("value", {}).get("messages", [])]
        except ValueError:
            raise HTTPException(400, "bad json")
        for m in msgs:
            try:
                db.execute("INSERT INTO wa_seen VALUES (?,?)", (m["id"], clock()))
            except (sqlite3.IntegrityError, KeyError):
                continue                                                 # replay / malformed
            await _handle(m)
        db.execute("DELETE FROM wa_seen WHERE ts<?", (clock() - 86400,))
        return {"ok": True}

    async def _handle(m: dict) -> None:
        phone = m.get("from", "")
        p = principal(phone)
        kind = m.get("type")
        text = (m.get("text") or {}).get("body", "") if kind == "text" else ""
        link = db.execute("SELECT * FROM wa_links WHERE principal=?", (p,)).fetchone()
        if link is None:
            parts = text.strip().split()
            if len(parts) == 2 and parts[0].lower() == "link":
                r = db.execute("SELECT * FROM wa_codes WHERE code_hash=? AND exp>?", (hashlib.sha256(parts[1].upper().encode()).hexdigest(), clock())).fetchone()
                if r:
                    db.execute("DELETE FROM wa_codes WHERE code_hash=?", (r["code_hash"],))
                    db.execute("INSERT INTO wa_links VALUES (?,?,?,?)", (p, r["shop_id"], r["role"], r["label"]))
                    await sender(phone, {"type": "text", "text": {"body": "✅ Jud gaye. Ab yahin entry likhein."}})
                    return
            await sender(phone, {"type": "text", "text": {"body": "Pehle Telegram se /wa dabakar code lein, phir yahan likhein: link CODE"}})
            return
        t = registry.tenant(link["shop_id"])
        chat = chat_id(p)
        if link["role"] == "staff":
            t.service.actors[chat] = link["label"] or "staff"
        if kind == "interactive":
            it = m["interactive"]
            data = (it.get("button_reply") or it.get("list_reply") or {}).get("id", "")
            reply = t.service.on_callback(chat, data)
        elif kind == "text":
            reply = t.service.on_typed(chat, text)
        elif kind == "audio" and on_voice is not None:
            reply = await on_voice(t, chat, m["audio"])
        else:
            reply = type("R", (), {"text": "Abhi text likhein (ya Telegram par voice bhejein).", "buttons": []})()
        await sender(phone, to_interactive(reply.text, reply.buttons))

    return router


async def _noop() -> None:
    return None
