"""REST front door: lets any channel (a WhatsApp gateway, a web app, an IVR bridge) talk to a shop's Munim brain.

Auth: per-shop API key (X-API-Key). The key acts as that shop's OWNER principal, so keep it server-side.
Every request goes through the same MunimService (two-ear audit, Governor, hash chain) as Telegram — the API adds no
shortcut around any safety rule. Rate-limited per key. Run:  uvicorn munim.api.server:app  (needs `pip install munim[api]`).
"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from munim.config import load_settings
from munim.ops.metrics import render as render_metrics
from munim.tenancy.registry import Registry

API_CHAT_BASE = 9_000_000_000_000          # keeps API principals far from Telegram chat ids inside a shop's service


class MessageIn(BaseModel):
    principal: int = Field(ge=0, lt=10**9, description="your stable id for the person/session talking")
    text: str = Field(min_length=1, max_length=2000)


class CommandIn(BaseModel):
    principal: int = Field(ge=0, lt=10**9)
    name: str = Field(min_length=1, max_length=16)
    args: str = Field(default="", max_length=500)


class CallbackIn(BaseModel):
    principal: int = Field(ge=0, lt=10**9)
    data: str = Field(min_length=1, max_length=64)


class ReplyOut(BaseModel):
    text: str
    voice_text: str | None = None
    buttons: list[tuple[str, str]] = []
    wrote: bool = False
    needs_plan: bool = False


def create_app(registry: Registry | None = None, rate_per_min: int = 60) -> FastAPI:
    cfg = load_settings()
    reg = registry or Registry(cfg.root_dir, cfg.hmac_secret, cfg)
    app = FastAPI(title="Munim API", version="1")
    hits: dict[str, deque] = defaultdict(deque)

    def auth(request_key: str | None) -> str:
        if not request_key:
            raise HTTPException(401, "missing X-API-Key")
        shop = reg.shop_for_api_key(request_key)
        if shop is None:
            raise HTTPException(401, "invalid API key")
        now = time.time()
        q = hits[request_key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= rate_per_min:
            raise HTTPException(429, "rate limit")
        q.append(now)
        return shop

    def out(r) -> ReplyOut:
        return ReplyOut(text=r.text, voice_text=r.voice_text, buttons=r.buttons, wrote=r.wrote, needs_plan=r.action == "plan")

    from munim import extras
    from munim.api.dashboard import make_router as dash_router
    app.include_router(dash_router(reg, auth))
    if os.environ.get("MUNIM_WA_APP_SECRET"):
        from munim.bot.whatsapp_bot import CloudSender, make_router as wa_router
        sender = CloudSender(os.environ.get("MUNIM_WA_TOKEN", ""), os.environ.get("MUNIM_WA_PHONE_ID", ""))
        app.include_router(wa_router(reg, sender, os.environ.get("MUNIM_WA_VERIFY_TOKEN", ""), os.environ["MUNIM_WA_APP_SECRET"]))
    if os.environ.get("MUNIM_EDGE", "0") == "1":
        from munim.edge.ingest import make_router as edge_router
        er = edge_router(reg, None)
        extras.SERVICES.edge_devices = er.edge_devices
        app.include_router(er)
    if os.environ.get("MUNIM_SMS", "0") == "1":
        from munim.ingest.sms_listener import make_router as sms_router
        sr = sms_router(reg)
        extras.SERVICES.sms_devices = sr.sms_devices
        app.include_router(sr)

    @app.post("/v1/command", response_model=ReplyOut)
    async def command(body: CommandIn, x_api_key: str | None = Header(default=None)):
        if body.name not in extras.EXTRA_COMMANDS:
            raise HTTPException(404, "unknown command")
        t = reg.tenant(auth(x_api_key))
        return out(await extras.run(reg, t, None, API_CHAT_BASE + body.principal, body.name, body.args))

    @app.post("/v1/rail/webhook")
    async def rail_webhook(request: Request, x_shop_key: str | None = Header(default=None), x_signature: str = Header("")):
        t = reg.tenant(auth(x_shop_key))
        from munim.payments.mandate import Engine
        res = Engine(t, extras.SERVICES.rail).on_webhook(await request.body(), x_signature)
        if isinstance(res, dict) and res.get("why") == "bad_signature":
            raise HTTPException(401, "bad signature")             # the rail should see a failure, not a 200
        return res

    @app.get("/healthz")
    def health():
        return {"ok": True, "shops": reg.shop_count()}

    @app.post("/v1/message", response_model=ReplyOut)
    def message(body: MessageIn, x_api_key: str | None = Header(default=None)):
        t = reg.tenant(auth(x_api_key))
        return out(t.service.on_typed(API_CHAT_BASE + body.principal, body.text))

    @app.post("/v1/callback", response_model=ReplyOut)
    def callback(body: CallbackIn, x_api_key: str | None = Header(default=None)):
        t = reg.tenant(auth(x_api_key))
        return out(t.service.on_callback(API_CHAT_BASE + body.principal, body.data))

    @app.get("/v1/verify")
    def verify(x_api_key: str | None = Header(default=None)):
        ok, seq, msg = reg.tenant(auth(x_api_key)).ledger.verify()
        return {"ok": ok, "bad_seq": seq, "message": msg}

    @app.get("/metrics")
    def metrics(request: Request, x_admin_token: str | None = Header(default=None)):
        want = os.environ.get("MUNIM_ADMIN_TOKEN", "")
        if not want or x_admin_token != want:
            raise HTTPException(401, "admin token required")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(render_metrics(reg))

    return app


def __getattr__(name: str):                 # `uvicorn munim.api.server:app` builds the app lazily
    if name == "app":
        return create_app()
    raise AttributeError(name)
