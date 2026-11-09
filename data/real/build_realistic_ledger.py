"""Build a ledger from REAL Indian grocery prices (Kaggle BigBasket) + real Indian first/last names.

    pip install kagglehub pandas
    python data/real/fetch_kaggle.py                       # downloads both datasets (needs Kaggle auth)
    python data/real/build_realistic_ledger.py --products PATH.csv [--names PATH] [--out data/munim_real.db]

WHAT IS REAL: item prices (INR) and name vocabulary. WHAT IS MODELLED: who buys what when, and repayment
behaviour (archetypes below). No public dataset of actual kirana udhaar exists; we say so rather than pretend.
Shopkeepers round baskets to ₹5/₹10, so do we.
"""
from __future__ import annotations

import csv
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from munim.govern.governor import IST  # noqa: E402
from munim.ledger.chain import Ledger  # noqa: E402

PRICE_COLS = ("sale_price", "price", "selling_price", "mrp", "market_price")
FALLBACK_FIRST = ["Ramesh", "Sunita", "Mohan", "Anil", "Kavita", "Rajesh", "Pooja", "Suresh", "Meena", "Amit", "Geeta",
                  "Vijay", "Sanjay", "Rekha", "Prakash", "Lata", "Neha", "Om", "Radha", "Santosh", "Hari", "Savitri"]
FALLBACK_LAST = ["Sharma", "Verma", "Gupta", "Yadav", "Mishra", "Patel", "Singh", "Das", "Sahu", "Panda", "Nayak", "Jha"]


def load_prices(path: str, lo: float = 5, hi: float = 600) -> list[float]:
    """Auto-detects the price column (schema not verified offline). Keeps staple-range prices only."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in (rd.fieldnames or [])}
        col = next((cols[c] for c in PRICE_COLS if c in cols), None)
        if col is None:
            raise SystemExit(f"no price column among {PRICE_COLS}; found {list(cols)}")
        out = []
        for row in rd:
            try:
                p = float(str(row[col]).replace(",", ""))
            except ValueError:
                continue
            if lo <= p <= hi:
                out.append(p)
    if len(out) < 10:
        raise SystemExit("too few usable prices")
    return out


def load_names(path: str | None, rng: random.Random, n: int) -> list[str]:
    firsts, lasts = list(FALLBACK_FIRST), list(FALLBACK_LAST)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            sample = [ln.strip() for ln in fh if ln.strip()]
        words = [w for ln in sample[1:] for w in ln.replace(",", " ").split() if w.isalpha() and 3 <= len(w) <= 12 and w[0].isupper() or w.isalpha() and 3 <= len(w) <= 12]
        if len(words) > 100:
            firsts = [w.title() for w in words[:2000]]
    seen, out = set(), []
    while len(out) < n:
        nm = f"{rng.choice(firsts)} {rng.choice(lasts)}"
        if nm not in seen:
            seen.add(nm)
            out.append(nm)
    return out


@dataclass
class Archetype:
    name: str
    weight: float
    visits_per_week: float
    pay_prob: float        # chance a given settlement-day pays something
    pay_fraction: tuple[float, float]
    monthly_cycle: bool    # pays in a burst after day ~1-5 (salary)


ARCHETYPES = [
    Archetype("regular", 0.40, 2.0, 0.55, (0.5, 1.0), False),
    Archetype("salary_cycle", 0.30, 1.5, 0.85, (0.8, 1.0), True),
    Archetype("slow", 0.20, 1.0, 0.18, (0.2, 0.5), False),
    Archetype("defaulter", 0.10, 0.7, 0.04, (0.1, 0.3), False),
]


def basket(prices: list[float], rng: random.Random) -> int:
    k = rng.choices([1, 2, 3, 4, 5, 6, 8], weights=[8, 14, 18, 16, 12, 8, 4])[0]
    total = sum(rng.choice(prices) for _ in range(k))
    step = rng.choice([5, 10])
    return max(step, int(round(total / step) * step))


def build(prices: list[float], names: list[str], out: str, days: int = 120, seed: int = 2610) -> Ledger:
    if os.path.exists(out):
        os.remove(out)
    rng = random.Random(seed)
    t0 = datetime(2026, 7, 1, 9, 0, tzinfo=IST)

    class Clk:
        t = t0.timestamp()

        def __call__(self):
            return self.t

    clk = Clk()
    led = Ledger(out, "real-secret", clock=clk)
    custs = []
    for nm in names:
        a = rng.choices(ARCHETYPES, weights=[x.weight for x in ARCHETYPES])[0]
        custs.append((led.add_customer(nm), a))
    for d in range(days):
        day = t0 + timedelta(days=d)
        for c, a in custs:
            if rng.random() < a.visits_per_week / 7:
                clk.t = (day + timedelta(minutes=rng.randint(0, 600))).timestamp()
                led.add_udhaar(c.id, basket(prices, rng) * 100, src="bigbasket_basket", arch=a.name)
            settle = (a.monthly_cycle and day.day in (2, 3, 4, 5)) or (not a.monthly_cycle and day.weekday() == 5)
            if settle and rng.random() < a.pay_prob:
                bal = led.balance(c.id)
                if bal > 0:
                    frac = rng.uniform(*a.pay_fraction)
                    pay = min(bal, max(500, int(bal * frac) // 500 * 500))
                    clk.t = (day + timedelta(hours=18, minutes=rng.randint(0, 120))).timestamp()
                    led.record_payment(c.id, pay, src="settlement", arch=a.name)
    clk.t = (t0 + timedelta(days=days, hours=1)).timestamp()
    return led


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--products", required=True)
    ap.add_argument("--names")
    ap.add_argument("--out", default="data/munim_real.db")
    ap.add_argument("--customers", type=int, default=40)
    ap.add_argument("--days", type=int, default=120)
    a = ap.parse_args()
    rng = random.Random(1)
    led = build(load_prices(a.products), load_names(a.names, rng, a.customers), a.out, a.days)
    ok, _, msg = led.verify()
    print(f"{len(led.customers())} customers, {len(led.entries())} entries -> {a.out}; chain {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
