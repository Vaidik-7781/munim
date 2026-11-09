"""Build an ONDC/Beckn retail catalog (domain ONDC:RET10, grocery) from the voice-derived inventory.

UNVERIFIED against a live ONDC network: field names follow the public Beckn 1.2 / ONDC retail spec as we understand it
(`message.catalog.bpp/providers[].items[]`), but a real listing goes through a registered Seller Network Participant that
owns the signing keys, registry subscription, KYC (GST/FSSAI/bank) and the select/init/confirm order flow. This module only
produces the catalog + stock part, and — more importantly — decides what is SAFE to publish:

  * an item is hidden unless it has a price, an MRP (legal-metrology), a manufacturer/packer, a recent physical count
    ('fresh') and stock above a safety buffer. Overselling on a 10-minute-delivery network costs seller rating; showing
    less than we have is always the safer error.
  * counts are derived from voice sales, so they are approximate: the buffer (default 20 %) absorbs un-dictated sales.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal

from munim.inventory.stock import Inventory, Sku

UNIT_NAME = {"g": "gram", "ml": "millilitre", "pc": "unit"}


def money(paise: int) -> str:
    return str((Decimal(paise) / 100).quantize(Decimal("0.01")))


def blockers(inv: Inventory, s: Sku, buffer_pct: int) -> list[str]:
    out = []
    if not s.ondc:
        out.append("owner_off")
    if s.price_paise is None:
        out.append("no_price")
    if s.mrp_paise is None:
        out.append("no_mrp")
    elif s.price_paise is not None and s.price_paise > s.mrp_paise:
        out.append("price_above_mrp")
    if not s.manufacturer:
        out.append("no_manufacturer")
    if inv.confidence(s) != "fresh":
        out.append("count_not_fresh")
    if _available(s, buffer_pct) <= 0:
        out.append("out_of_stock")
    return out


def _available(s: Sku, buffer_pct: int) -> int:
    pack = max(1, s.price_per)
    safe = max(0, s.stock) * (100 - buffer_pct) // 100
    return safe // pack


@dataclass
class Catalog:
    payload: dict
    published: list[int] = field(default_factory=list)
    hidden: list[tuple[Sku, list[str]]] = field(default_factory=list)

    def item_hashes(self) -> dict[str, str]:
        return {it["id"]: hashlib.sha256(json.dumps(it, sort_keys=True).encode()).hexdigest()[:16]
                for it in self.payload["message"]["catalog"]["bpp/providers"][0]["items"]}


def build_catalog(shop_id: str, shop_name: str, pincode: str, inv: Inventory, *, buffer_pct: int = 20,
                  gps: str | None = None) -> Catalog:
    items, published, hidden = [], [], []
    for s in inv.skus():
        why = blockers(inv, s, buffer_pct)
        if why:
            hidden.append((s, why))
            continue
        avail, pack = _available(s, buffer_pct), max(1, s.price_per)
        items.append({
            "id": f"{shop_id}-{s.id}",
            "descriptor": {"name": s.name, "code": f"4:{s.key.replace(' ', '_')}"},
            "quantity": {"unitized": {"measure": {"unit": UNIT_NAME[s.dim], "value": str(pack)}},
                         "available": {"count": str(avail)}, "maximum": {"count": str(min(avail, 10))}},
            "price": {"currency": "INR", "value": money(s.price_paise), "maximum_value": money(s.mrp_paise)},
            "category_id": "Grocery", "fulfillment_id": "F1", "location_id": "L1",
            "@ondc/org/returnable": False, "@ondc/org/cancellable": True, "@ondc/org/time_to_ship": "PT60M",
            "@ondc/org/available_on_cod": False, "@ondc/org/seller_pickup_return": False,
            "@ondc/org/statutory_reqs_packaged_commodities": {
                "manufacturer_or_packer_name": s.manufacturer, "common_or_generic_name_of_commodity": s.name,
                "net_quantity_or_measure_of_commodity_in_pkg": f"{pack} {UNIT_NAME[s.dim]}"},
        })
        published.append(s.id)
    loc = {"id": "L1", "address": {"area_code": pincode}, **({"gps": gps} if gps else {})}
    payload = {"message": {"catalog": {
        "bpp/descriptor": {"name": shop_name},
        "bpp/providers": [{"id": shop_id, "descriptor": {"name": shop_name}, "locations": [loc],
                           "fulfillments": [{"id": "F1", "type": "Delivery"}, {"id": "F2", "type": "Self-Pickup"}],
                           "items": items}]}}}
    return Catalog(payload, published, hidden)


WHY_HI = {"owner_off": "aapne band kiya", "no_price": "daam nahi", "no_mrp": "MRP nahi", "price_above_mrp": "daam MRP se zyada",
          "no_manufacturer": "banane wale ka naam nahi", "count_not_fresh": "pichhle 14 din mein ginti/maal nahi",
          "out_of_stock": "stock kam/khatam"}
