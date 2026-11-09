"""SMS/bank-credit forwarder endpoint: an Android forwarder (or a bank webhook) posts credit SMS; Munim queues PROBABLE settlements.

UNVERIFIED / CAVEATS: reading SMS needs Android SMS permission, which Google Play restricts (sideload/enterprise distribution only);
the production-safe alternatives are bank/PA webhooks or the Account Aggregator route (munim.lending.aa). Only CREDITS are kept:
debits, OTP/PIN/CVV messages and anything unparseable are dropped before anything is stored; what survives is amount, a parsed
payer-name token and a keyed hash of the UPI reference. Matching is deterministic (munim.agent.reconcile); NOTHING is written to the
money chain until the owner taps approve (one daily digest).
"""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, Header, HTTPException, Request

from munim.agent import reconcile as R
from munim.edge.devices import DeviceBook
from munim.scribe.upi import parse_upi

SENSITIVE = re.compile(r"\b(otp|cvv|pin|password|one\s*time)\b", re.I)


def process_sms(ledger, body: str, now: float) -> dict:
    if SENSITIVE.search(body):
        return {"status": "dropped_sensitive"}
    cr = parse_upi(body)
    if cr is None:
        return {"status": "dropped_not_credit"}
    tok = ledger.pseudo("upi", cr.ref) if cr.ref else None
    return R.queue(ledger, cr.amount_paise, cr.payer, tok, now, src="sms")


def make_router(registry, devices: DeviceBook | None = None, clock=time.time) -> APIRouter:
    book = devices or DeviceBook(registry, "sms", clock, rate_per_min=120)
    router = APIRouter()
    router.sms_devices = book                                          # type: ignore[attr-defined]

    @router.post("/v1/sms")
    async def sms(request: Request, x_dev: str = Header(""), x_ts: int = Header(0), x_sig: str = Header("")):
        raw = await request.body()
        if len(raw) > 4000:
            raise HTTPException(413, "too large")
        row, why = book.authenticate(x_dev, x_ts, raw, x_sig)
        if row is None:
            raise HTTPException(429 if why == "rate" else 401, why)
        try:
            import json
            body = str(json.loads(raw).get("body", ""))
        except ValueError:
            raise HTTPException(400, "bad json")
        t = registry.tenant(row["shop_id"])
        out = process_sms(t.ledger, body, clock())
        book.count(row["shop_id"], out["status"])
        return {"status": out["status"]}

    return router
