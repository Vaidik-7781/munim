"""ONDC sync: opt-in, incremental, owner-approved. The network side is an adapter (Seller NP / sandbox).

Rules: nothing leaves the shop until the owner confirms the exact public listing once (`/ondc publish`); after that,
only item-level changes are pushed. Pushing is non-blocking for the ledger: a failed push is queued in `ondc_outbox`
and retried; it never affects udhaar. Orders arriving from the network become `sale` stock events (src=ondc).
`HttpSink` is UNVERIFIED (we have no Seller-NP credentials): it posts JSON to a URL you configure.
"""
from __future__ import annotations

import json
from typing import Protocol

import httpx

from munim.inventory.stock import Inventory
from munim.ondc.beckn import Catalog, build_catalog


class Sink(Protocol):
    def push(self, payload: dict) -> str: ...


class SandboxSink:
    """Records what WOULD be sent. Default; used in tests and demos."""
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def push(self, payload: dict) -> str:
        self.sent.append(payload)
        return f"sandbox-{len(self.sent)}"


class HttpSink:
    def __init__(self, url: str, token: str, transport: httpx.BaseTransport | None = None):
        self.url, self.token, self._t = url, token, transport

    def push(self, payload: dict) -> str:
        with httpx.Client(transport=self._t, timeout=10) as c:
            r = c.post(self.url, json=payload, headers={"Authorization": f"Bearer {self.token}"})
        r.raise_for_status()
        return str(r.json().get("ref", "ok"))


def preview(tenant, pincode: str | None, buffer_pct: int = 20) -> Catalog:
    inv = Inventory(tenant.aux, tenant._clock)
    return build_catalog(tenant.shop_id, tenant.name, pincode or "", inv, buffer_pct=buffer_pct)


def enable(tenant) -> None:
    tenant.aux.put("ondc_enabled", True)
    tenant.aux.put("ondc_hashes", {})


def disable(tenant) -> None:
    tenant.aux.put("ondc_enabled", False)


def sync(tenant, pincode: str | None, sink: Sink, buffer_pct: int = 20) -> dict:
    """Push only changed items. Items that became unpublishable are pushed with count 0 so the storefront hides them."""
    if not tenant.aux.get("ondc_enabled", False):
        return {"status": "disabled"}
    if not pincode:
        return {"status": "needs_pincode"}
    cat = preview(tenant, pincode, buffer_pct)
    old: dict[str, str] = tenant.aux.get("ondc_hashes", {})
    new = cat.item_hashes()
    prov = cat.payload["message"]["catalog"]["bpp/providers"][0]
    changed = [it for it in prov["items"] if old.get(it["id"]) != new[it["id"]]]
    gone = [k for k in old if k not in new]
    if not changed and not gone:
        return {"status": "unchanged", "published": len(new)}
    delta = json.loads(json.dumps(cat.payload))
    delta["message"]["catalog"]["bpp/providers"][0]["items"] = changed + [
        {"id": k, "quantity": {"available": {"count": "0"}, "maximum": {"count": "0"}}} for k in gone]
    try:
        ref = sink.push(delta)
        status = "pushed"
    except Exception as exc:                                    # network trouble must never hurt the ledger
        tenant.aux.db.execute("INSERT INTO ondc_outbox(ts,kind,payload,status) VALUES (?,?,?,?)",
                              (tenant._clock(), "catalog_delta", json.dumps(delta), "pending"))
        return {"status": "queued", "error": type(exc).__name__}
    tenant.aux.put("ondc_hashes", new)
    tenant.aux.db.execute("INSERT INTO ondc_outbox(ts,kind,payload,status,ref) VALUES (?,?,?,?,?)",
                          (tenant._clock(), "catalog_delta", json.dumps(delta), status, ref))
    return {"status": status, "changed": len(changed), "removed": len(gone), "ref": ref}


def apply_order(tenant, lines: list[tuple[str, int]]) -> list[str]:
    """Network order -> stock sale events. lines: [(item_id 'shop-skuid', qty_packs)]. Returns problems (never raises)."""
    inv, problems = Inventory(tenant.aux, tenant._clock), []
    for item_id, packs in lines:
        try:
            sid = int(item_id.rsplit("-", 1)[1])
            s = inv.get(sid)
            if s is None or packs <= 0:
                raise ValueError
            inv.sale(sid, packs * max(1, s.price_per), src="ondc")
        except (ValueError, IndexError):
            problems.append(item_id)
    return problems
