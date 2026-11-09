"""Owner dashboard API + zero-build PWA (/app). The API key acts as the shop OWNER, so it is shown only to the owner's own browser
(stored in localStorage when available). Endpoints: /v1/dashboard/summary, /passport, /model. The PWA renders everything with
textContent (no innerHTML) and has no third-party scripts. No weights are exposed by /model."""
from __future__ import annotations

import os
import time
from typing import Callable

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from munim.insights import engine as E
from munim.insights.staff_audit import audit_staff

STATIC = os.path.join(os.path.dirname(__file__), "static")


def make_router(registry, auth: Callable[[str | None], str]) -> APIRouter:
    r = APIRouter()

    @r.get("/v1/dashboard/summary")
    def summary(x_api_key: str | None = Header(default=None)):
        t = registry.tenant(auth(x_api_key))
        L, now = t.ledger, time.time()
        buckets = {k: 0 for k, _, _ in E.BUCKETS}
        for c in L.customers():
            for k, v in E.aging(L, c.id, now).items():
                buckets[k] += v
        ok, _, msg = L.verify()
        try:
            flags = [f if isinstance(f, str) else getattr(f, "code", str(f)) for f in audit_staff(L, now)]
        except Exception:
            flags = []
        return {"shop": t.name, "outstanding_paise": E.total_outstanding(L),
                "top_debtors": [{"name": c.name, "paise": b} for c, b in E.top_debtors(L, 5)],
                "aging": buckets, "weekly": E.weekly_flow(L, now, 8), "chain_ok": ok, "chain_msg": msg, "staff_flags": flags[:10]}

    @r.get("/v1/dashboard/passport")
    def passport(x_api_key: str | None = Header(default=None)):
        t = registry.tenant(auth(x_api_key))
        try:
            from munim.passport import cashflow
        except ImportError:
            raise HTTPException(501, "passport not available")
        return cashflow.build(t.ledger, time.time()).as_dict()

    @r.get("/v1/dashboard/model")
    def model(x_api_key: str | None = Header(default=None)):
        auth(x_api_key)
        m = registry.load_global_model()
        p = registry.global_model_path
        if m is None:
            return {"trained": False}
        return {"trained": True, "n_features": len(getattr(m, "w", []) or []), "updated": os.path.getmtime(p),
                "note": "experimental: no demonstrated skill on simulated data"}

    @r.get("/app")
    def app_page():
        return FileResponse(os.path.join(STATIC, "app.html"), media_type="text/html")

    @r.get("/app/manifest.webmanifest")
    def manifest():
        return JSONResponse({"name": "Munim", "short_name": "Munim", "start_url": "/app", "display": "standalone", "background_color": "#faf7f2",
                             "theme_color": "#1f6f50", "icons": []}, media_type="application/manifest+json")

    @r.get("/app/sw.js")
    def sw():
        return FileResponse(os.path.join(STATIC, "sw.js"), media_type="application/javascript")

    return r
