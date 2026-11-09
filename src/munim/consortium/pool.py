"""FMCG group-buying consortium: pooled demand signals -> bulk RFQ -> quote evaluation. Humans send, shops pay directly.

Privacy design (enforced + tested):
  * shops are pseudonymous (HMAC of shop id) inside the pool store; the pool never holds names, customers or ledger data —
    only per-SKU weekly UNITS SOLD (voice-derived stock events).
  * an aggregate is released only if >= K distinct shops contributed for that (pincode, sku, week) AND no single shop is
    more than 60 % of the units (dominance rule). Below that, the answer is "not enough shops", never a partial number.
  * per-shop commitment quantities are visible to the shop itself and to the coordinator, never to other shops; a distributor
    would see only the pooled total and shop count.
  * this is k-anonymity + a dominance rule, NOT differential privacy. Optional Laplace noise (off by default) is a knob only.
Money: Munim never holds or routes funds. Each shop pays the distributor directly after confirming the final price.
Negotiation: the agent computes targets/walk-away deterministically and DRAFTS messages for the coordinator; it never contacts
a distributor by itself. Reality check: a pincode has hundreds, not 10,000, kiranas — pooling across a few adjacent pincodes
is the realistic unit; reaching DMart-like terms is an aspiration, not something this code can promise.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta

K_MIN = 5
DOMINANCE = 0.60
COLD = ("cold drink", "soft drink", "cola", "juice", "lassi", "chaas", "water", "soda", "ice cream", "sharbat", "nimbu")

SCHEMA = """
CREATE TABLE IF NOT EXISTS velocity(week TEXT, pin TEXT, sku_key TEXT, shop TEXT, units INTEGER, PRIMARY KEY(week,pin,sku_key,shop));
CREATE TABLE IF NOT EXISTS rfq(id INTEGER PRIMARY KEY AUTOINCREMENT, pin TEXT, sku_key TEXT, dim TEXT, status TEXT, created REAL,
  deadline TEXT, reference_paise INTEGER, final_paise INTEGER, final_distributor TEXT);
CREATE TABLE IF NOT EXISTS commit_(rfq_id INTEGER, shop TEXT, qty INTEGER, status TEXT, ts REAL, PRIMARY KEY(rfq_id,shop));
CREATE TABLE IF NOT EXISTS quotes(id INTEGER PRIMARY KEY AUTOINCREMENT, rfq_id INTEGER, distributor TEXT, price_paise INTEGER, min_qty INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS alias(src TEXT PRIMARY KEY, dst TEXT);
"""


def week_of(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


class Weather:                                             # adapter: anything with max_temp(pin, day) -> float|None
    def max_temp(self, pin: str, day: date) -> float | None:
        return None


class ManualWeather(Weather):
    def __init__(self, temps: dict[str, float]):
        self.temps = temps

    def max_temp(self, pin: str, day: date) -> float | None:
        return self.temps.get(pin)


def weather_multiplier(sku_key: str, tmax: float | None) -> float:
    if tmax is None or not any(w in sku_key for w in COLD):
        return 1.0
    return 1.5 if tmax >= 40 else 1.3 if tmax >= 37 else 1.15 if tmax >= 34 else 1.0


@dataclass
class Aggregate:
    ok: bool
    units: int | None
    shops: int
    why: str = ""


class Pool:
    def __init__(self, registry, clock=time.time, noise: float = 0.0):
        self.reg, self.clock, self.noise = registry, clock, noise
        self.db = sqlite3.connect(os.path.join(registry.root, "pool.db"), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def shop(self, shop_id: str) -> str:
        return hmac.new(self.reg._master, f"pool:{shop_id}".encode(), hashlib.sha256).hexdigest()[:16]

    def key(self, sku_key: str) -> str:
        r = self.db.execute("SELECT dst FROM alias WHERE src=?", (sku_key,)).fetchone()
        return r["dst"] if r else sku_key

    def add_alias(self, src: str, dst: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO alias VALUES (?,?)", (src, dst))

    # ---- contribute / aggregate --------------------------------------------------
    def contribute(self, shop_id: str, weeks: int = 4) -> int:
        """Push this shop's last `weeks` of per-SKU weekly sales (units) into the pool. Needs a pincode. Returns rows written."""
        pin = self.reg.pincode(shop_id)
        if not pin:
            return 0
        from munim.inventory.stock import Inventory
        t = self.reg.tenant(shop_id)
        inv = Inventory(t.aux, self.clock)
        n, today = 0, date.fromtimestamp(self.clock())
        undone = {r["reverses"] for r in t.aux.db.execute("SELECT reverses FROM stock_events WHERE kind='reversal'")}
        for s in inv.skus():
            for w in range(weeks):
                start = week_of(today) if w == 0 else week_of(today - timedelta(weeks=w))
                a = time.mktime(date.fromisoformat(start).timetuple())
                rows = t.aux.db.execute("SELECT id,delta FROM stock_events WHERE sku_id=? AND kind='sale' AND ts>=? AND ts<?",
                                        (s.id, a, a + 7 * 86400)).fetchall()
                u = sum(-r["delta"] for r in rows if r["id"] not in undone)
                if u > 0:
                    self.db.execute("INSERT OR REPLACE INTO velocity VALUES (?,?,?,?,?)", (start, pin, self.key(s.key), self.shop(shop_id), u))
                    n += 1
        return n

    def aggregate(self, pin: str, sku_key: str, week: str) -> Aggregate:
        rows = self.db.execute("SELECT units FROM velocity WHERE pin=? AND sku_key=? AND week=?", (pin, self.key(sku_key), week)).fetchall()
        us = [r["units"] for r in rows]
        if len(us) < K_MIN:
            return Aggregate(False, None, len(us), f"need at least {K_MIN} shops, have {len(us)}")
        tot = sum(us)
        if max(us) > DOMINANCE * tot:
            return Aggregate(False, None, len(us), "one shop dominates this number")
        if self.noise:
            tot = max(0, tot + int(round(_laplace(self.noise))))
        return Aggregate(True, tot, len(us))

    def series(self, pin: str, sku_key: str, weeks: int = 8, upto: date | None = None) -> list[tuple[str, int]]:
        upto = upto or date.fromtimestamp(self.clock())
        out = []
        for w in range(weeks - 1, -1, -1):
            wk = week_of(upto - timedelta(weeks=w))
            a = self.aggregate(pin, sku_key, wk)
            if a.ok:
                out.append((wk, a.units))
        return out

    # ---- demand ---------------------------------------------------------------------
    def forecast(self, pin: str, sku_key: str, weather: Weather | None = None, alpha: float = 0.5) -> dict:
        s = self.series(pin, sku_key)
        if len(s) < 3:
            return {"ok": False, "why": "fewer than 3 weeks of releasable pooled data"}
        base = s[0][1]
        for _, u in s[1:]:
            base = alpha * u + (1 - alpha) * base
        tmax = (weather or Weather()).max_temp(pin, date.fromtimestamp(self.clock()) + timedelta(days=3))
        mult = weather_multiplier(sku_key, tmax)
        pred = int(round(base * mult))
        return {"ok": True, "baseline": int(round(base)), "predicted": pred, "multiplier": mult, "tmax": tmax,
                "surge": pred >= 1.25 * base, "weeks": len(s)}

    def suggest_for_shop(self, shop_id: str, sku_key: str, current_stock: int, own_weekly: int, f: dict) -> int:
        """What THIS shop might order: own velocity scaled by the pooled surge, minus stock, capped at 2x own weekly sales."""
        if not f.get("ok") or own_weekly <= 0:
            return 0
        scale = f["predicted"] / max(1, f["baseline"])
        want = int(own_weekly * scale) - max(0, current_stock)
        return max(0, min(want, 2 * own_weekly))

    # ---- RFQ ---------------------------------------------------------------------------
    def open_rfq(self, pin: str, sku_key: str, dim: str, deadline_iso: str, reference_paise: int) -> int:
        cur = self.db.execute("INSERT INTO rfq(pin,sku_key,dim,status,created,deadline,reference_paise) VALUES (?,?,?,?,?,?,?)",
                              (pin, self.key(sku_key), dim, "open", self.clock(), deadline_iso, reference_paise))
        return cur.lastrowid

    def join(self, rfq_id: int, shop_id: str, qty: int) -> None:
        r = self.db.execute("SELECT * FROM rfq WHERE id=?", (rfq_id,)).fetchone()
        if r is None or r["status"] != "open" or self.reg.pincode(shop_id) != r["pin"] or qty <= 0:
            raise ValueError("cannot join this RFQ")
        self.db.execute("INSERT OR REPLACE INTO commit_ VALUES (?,?,?,?,?)", (rfq_id, self.shop(shop_id), qty, "committed", self.clock()))

    def withdraw(self, rfq_id: int, shop_id: str) -> None:
        self.db.execute("UPDATE commit_ SET status='withdrawn' WHERE rfq_id=? AND shop=? AND status='committed'", (rfq_id, self.shop(shop_id)))

    def totals(self, rfq_id: int) -> tuple[int, int]:
        r = self.db.execute("SELECT COALESCE(SUM(qty),0) q, COUNT(*) n FROM commit_ WHERE rfq_id=? AND status IN ('committed','confirmed')", (rfq_id,)).fetchone()
        return r["q"], r["n"]

    def add_quote(self, rfq_id: int, distributor: str, price_paise: int, min_qty: int = 0) -> int:
        if price_paise <= 0:
            raise ValueError("price must be positive")
        return self.db.execute("INSERT INTO quotes(rfq_id,distributor,price_paise,min_qty,ts) VALUES (?,?,?,?,?)",
                               (rfq_id, distributor[:40], price_paise, min_qty, self.clock())).lastrowid

    def evaluate(self, rfq_id: int) -> dict:
        """Deterministic: discount we can credibly ask grows with pooled volume vs a typical single shop; walk-away is >=2 % under reference."""
        r = self.db.execute("SELECT * FROM rfq WHERE id=?", (rfq_id,)).fetchone()
        qty, n = self.totals(rfq_id)
        if n == 0:
            return {"ok": False, "why": "no shops joined"}
        ref = r["reference_paise"]
        ratio = qty / max(1, qty / n)                                           # == n shops; kept explicit for readability
        d = min(0.12, 0.02 * math.log2(1 + ratio))
        target, walk = int(ref * (1 - d)), int(ref * 0.98)
        out = []
        for q in self.db.execute("SELECT * FROM quotes WHERE rfq_id=? ORDER BY price_paise", (rfq_id,)):
            if q["min_qty"] and qty < q["min_qty"]:
                act = "reject_min_qty_not_met"
            elif q["price_paise"] <= target:
                act = "accept"
            elif q["price_paise"] <= walk:
                act = "counter"
            else:
                act = "reject_above_walk_away"
            out.append({"distributor": q["distributor"], "price_paise": q["price_paise"], "action": act,
                        "counter_paise": target if act == "counter" else None})
        return {"ok": True, "qty": qty, "shops": n, "reference_paise": ref, "target_paise": target, "walk_away_paise": walk, "quotes": out}

    def draft_counter(self, rfq_id: int, distributor: str) -> str:
        ev = self.evaluate(rfq_id)
        q = next((x for x in ev.get("quotes", []) if x["distributor"] == distributor), None)
        if not q or q["action"] != "counter":
            return ""
        r = self.db.execute("SELECT * FROM rfq WHERE id=?", (rfq_id,)).fetchone()
        return (f"Namaste. {ev['shops']} dukaan milkar {ev['qty']} {r['dim']} {r['sku_key']} ek saath le rahi hain (pincode {r['pin']}). "
                f"Aapka rate Rs {q['price_paise'] / 100:.2f} hai; is volume par Rs {q['counter_paise'] / 100:.2f} par baat ho sakti hai? "
                "Bhugtan har dukaan seedha karegi. [Munim draft — coordinator bhejega]")

    def finalize(self, rfq_id: int, distributor: str, price_paise: int) -> None:
        self.db.execute("UPDATE rfq SET status='final', final_paise=?, final_distributor=? WHERE id=?", (price_paise, distributor, rfq_id))

    def confirm(self, rfq_id: int, shop_id: str) -> dict:
        """Binding step for ONE shop, after it has seen the final price. Returns that shop's own PO draft."""
        r = self.db.execute("SELECT * FROM rfq WHERE id=?", (rfq_id,)).fetchone()
        c = self.db.execute("SELECT * FROM commit_ WHERE rfq_id=? AND shop=?", (rfq_id, self.shop(shop_id))).fetchone()
        if r is None or r["status"] != "final" or c is None or c["status"] != "committed":
            raise ValueError("nothing to confirm")
        self.db.execute("UPDATE commit_ SET status='confirmed' WHERE rfq_id=? AND shop=?", (rfq_id, self.shop(shop_id)))
        return {"distributor": r["final_distributor"], "sku": r["sku_key"], "qty": c["qty"], "dim": r["dim"],
                "price_paise": r["final_paise"], "total_paise": c["qty"] * r["final_paise"],
                "note": "Bhugtan seedha distributor ko karein; Munim paisa nahi rakhta."}


def _laplace(scale: float) -> float:
    import random
    u = random.random() - 0.5
    return -scale * math.copysign(1, u) * math.log(1 - 2 * abs(u))
