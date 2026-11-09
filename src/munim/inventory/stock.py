"""Voice-derived inventory: an append-only stock event log per shop, integer base units (g / ml / pcs).

Why events, not a counter: every sale/restock is reversible (a reversal event), the sell-through velocity falls out of the
log, and a wrong voice entry never silently destroys the count. Stock accuracy is *approximate by nature* (voice sales
are not scanned): every SKU carries `verified_ts` (last physical count) and ONDC publishing hides stale/uncounted items.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from rapidfuzz import fuzz

from munim.store import Aux

DAY = 86400.0
GENERIC_DIM = {"atta": "g", "chawal": "g", "daal": "g", "cheeni": "g", "namak": "g", "poha": "g", "suji": "g", "besan": "g",
               "maida": "g", "jeera": "g", "haldi": "g", "mirch": "g", "tel": "ml", "doodh": "ml", "ghee": "g", "chai": "g",
               "sabun": "pc", "maggi": "pc", "biscuit": "pc", "bread": "pc", "anda": "pc", "agarbatti": "pc"}
SYN = {"aata": "atta", "gehu": "atta", "rice": "chawal", "dal": "daal", "dhal": "daal", "oil": "tel", "sugar": "cheeni",
       "chini": "cheeni", "salt": "namak", "milk": "doodh", "tea": "chai", "chay": "chai", "egg": "anda", "eggs": "anda",
       "soap": "sabun", "noodles": "maggi", "biscuits": "biscuit", "biskut": "biscuit", "आटा": "atta", "चावल": "chawal",
       "दाल": "daal", "तेल": "tel", "चीनी": "cheeni", "नमक": "namak", "दूध": "doodh", "चाय": "chai", "मैगी": "maggi"}
FILLER = {"of", "ka", "ki", "ke", "ko", "the", "a", "an", "ek", "stock", "stocked", "sold", "sale", "becha", "bechi", "bika",
          "aaya", "aayi", "aaye", "gaya", "gayi", "hai", "mein", "me", "se", "aur", "bhi", "abhi", "ab", "total", "count"}


def norm_name(s: str) -> str:
    toks = [SYN.get(t, t) for t in re.findall(r"[\wऀ-ॿ]+", s.lower())]
    return " ".join(t for t in toks if t not in FILLER and not t.isdigit())


@dataclass
class Sku:
    id: int
    key: str
    name: str
    dim: str
    price_paise: int | None
    price_per: int
    mrp_paise: int | None
    stock: int
    reorder: int | None
    manufacturer: str | None
    hsn: str | None
    gst_pct: int | None
    verified_ts: float | None
    ondc: bool


@dataclass
class Match:
    status: str                      # match | ask | none
    sku: Sku | None = None
    candidates: list[Sku] | None = None


def _sku(r) -> Sku:
    return Sku(r["id"], r["key"], r["name"], r["dim"], r["price_paise"], r["price_per"], r["mrp_paise"], r["stock"],
               r["reorder"], r["manufacturer"], r["hsn"], r["gst_pct"], r["verified_ts"], bool(r["ondc"]))


class Inventory:
    def __init__(self, aux: Aux, clock: Callable[[], float] = time.time):
        self.aux, self.clock = aux, clock

    # ---- catalog --------------------------------------------------------------
    def skus(self) -> list[Sku]:
        return [_sku(r) for r in self.aux.db.execute("SELECT * FROM sku ORDER BY name")]

    def get(self, sku_id: int) -> Sku | None:
        r = self.aux.db.execute("SELECT * FROM sku WHERE id=?", (sku_id,)).fetchone()
        return _sku(r) if r else None

    def find(self, text: str) -> Match:
        q = norm_name(text)
        if not q:
            return Match("none")
        skus = self.skus()
        exact = [s for s in skus if s.key == q]
        if exact:
            return Match("match", exact[0])
        qt = set(q.split())
        subset = [s for s in skus if qt and qt <= set(s.key.split())]       # "atta" matches "aashirvaad atta"
        if len(subset) == 1:
            return Match("match", subset[0])
        if len(subset) > 1:
            return Match("ask", None, subset[:4])
        scored = sorted(((fuzz.token_set_ratio(q, s.key), s) for s in skus), key=lambda x: -x[0])
        if scored and scored[0][0] >= 88 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 8):
            return Match("match", scored[0][1])
        close = [s for sc, s in scored if sc >= 80][:4]
        return Match("ask", None, close) if close else Match("none")

    def upsert(self, name: str, dim: str, *, price_paise: int | None = None, price_per: int | None = None,
               mrp_paise: int | None = None, reorder: int | None = None, manufacturer: str | None = None,
               hsn: str | None = None, gst_pct: int | None = None) -> Sku:
        key = norm_name(name)
        if not key:
            raise ValueError("empty item name")
        if dim not in ("g", "ml", "pc"):
            raise ValueError("dim must be g, ml or pc")
        per = price_per or (1 if dim == "pc" else 1000)
        with self.aux.lock:
            r = self.aux.db.execute("SELECT * FROM sku WHERE key=?", (key,)).fetchone()
            if r is None:
                self.aux.db.execute(
                    "INSERT INTO sku(key,name,dim,price_paise,price_per,mrp_paise,reorder,manufacturer,hsn,gst_pct,created)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (key, " ".join(name.split()).title()[:60], dim, price_paise, per, mrp_paise, reorder, manufacturer, hsn, gst_pct, self.clock()))
            else:
                if r["dim"] != dim:
                    raise ValueError(f"{r['name']} is counted in {r['dim']}, not {dim}")
                self.aux.db.execute(
                    "UPDATE sku SET price_paise=COALESCE(?,price_paise), price_per=?, mrp_paise=COALESCE(?,mrp_paise),"
                    " reorder=COALESCE(?,reorder), manufacturer=COALESCE(?,manufacturer), hsn=COALESCE(?,hsn),"
                    " gst_pct=COALESCE(?,gst_pct) WHERE key=?",
                    (price_paise, per if price_paise is not None else r["price_per"], mrp_paise, reorder, manufacturer, hsn, gst_pct, key))
        return self.find(key).sku  # type: ignore[return-value]

    # ---- events ----------------------------------------------------------------
    def apply(self, sku_id: int, delta: int, kind: str, src: str = "voice", note: str = "", reverses: int | None = None) -> int:
        if kind not in ("sale", "restock", "count", "reversal", "waste"):
            raise ValueError(kind)
        if not isinstance(delta, int) or isinstance(delta, bool):
            raise ValueError("delta must be integer base units")
        with self.aux.lock:
            cur = self.aux.db.execute(
                "INSERT INTO stock_events(ts,sku_id,delta,kind,src,reverses,note) VALUES (?,?,?,?,?,?,?)",
                (self.clock(), sku_id, delta, kind, src, reverses, note[:80]))
            self.aux.db.execute("UPDATE sku SET stock=stock+? WHERE id=?", (delta, sku_id))
            if kind in ("count", "restock"):
                self.aux.db.execute("UPDATE sku SET verified_ts=? WHERE id=?", (self.clock(), sku_id))
            return cur.lastrowid

    def sale(self, sku_id: int, qty: int, src: str = "voice") -> int:
        return self.apply(sku_id, -abs(qty), "sale", src)

    def restock(self, sku_id: int, qty: int, src: str = "voice", note: str = "") -> int:
        return self.apply(sku_id, abs(qty), "restock", src, note)

    def count(self, sku_id: int, qty: int) -> int:
        s = self.get(sku_id)
        return self.apply(sku_id, qty - s.stock, "count", "voice", f"count to {qty}")

    def undo(self, event_id: int) -> bool:
        with self.aux.lock:
            r = self.aux.db.execute("SELECT * FROM stock_events WHERE id=?", (event_id,)).fetchone()
            if r is None or r["kind"] == "reversal":
                return False
            if self.aux.db.execute("SELECT 1 FROM stock_events WHERE reverses=?", (event_id,)).fetchone():
                return False
            self.apply(r["sku_id"], -r["delta"], "reversal", "undo", "", reverses=event_id)
            return True

    # ---- analytics ----------------------------------------------------------------
    def sold_units(self, sku_id: int, days: int = 28) -> int:
        since = self.clock() - days * DAY
        rows = self.aux.db.execute(
            "SELECT id, delta FROM stock_events WHERE sku_id=? AND kind='sale' AND ts>=?", (sku_id, since)).fetchall()
        undone = {r["reverses"] for r in self.aux.db.execute(
            "SELECT reverses FROM stock_events WHERE kind='reversal' AND reverses IS NOT NULL")}
        return sum(-r["delta"] for r in rows if r["id"] not in undone)

    def weekly_velocity(self, sku_id: int, weeks: int = 4) -> int:
        return self.sold_units(sku_id, weeks * 7) // max(1, weeks)

    def days_of_cover(self, sku: Sku) -> float | None:
        v = self.sold_units(sku.id, 28) / 28
        return None if v <= 0 else max(0.0, sku.stock) / v

    def low_stock(self) -> list[Sku]:
        out = []
        for s in self.skus():
            cover = self.days_of_cover(s)
            if (s.reorder is not None and s.stock <= s.reorder) or (cover is not None and cover < 3):
                out.append(s)
        return out

    def confidence(self, sku: Sku) -> str:
        """fresh (counted/restocked ≤14 d) | stale (≤30 d) | unverified. Drives ONDC visibility."""
        if sku.verified_ts is None:
            return "unverified"
        age = (self.clock() - sku.verified_ts) / DAY
        return "fresh" if age <= 14 else "stale" if age <= 30 else "unverified"


def fmt_qty(n: int, dim: str) -> str:
    if dim == "g":
        return f"{n / 1000:g} kg" if abs(n) >= 1000 else f"{n} g"
    if dim == "ml":
        return f"{n / 1000:g} L" if abs(n) >= 1000 else f"{n} ml"
    return f"{n} pc"
