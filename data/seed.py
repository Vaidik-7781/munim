"""Synthetic demo data: 25 invented customers, 90 days of udhaar. NO real people or shops.

Usage: python data/seed.py [path]   (default data/munim_demo.db)
Deterministic (seeded) so screenshots and tests are reproducible.
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from munim.govern.governor import IST  # noqa: E402
from munim.ledger.chain import Ledger  # noqa: E402

NAMES = [
    "Ramesh Sharma", "Dinesh Sharma", "Sunita Verma", "Mohan Das", "Anil Gupta", "Kavita Singh",
    "Rajesh Yadav", "Pooja Mishra", "Suresh Patel", "Meena Devi", "Amit Tiwari", "Geeta Kumari",
    "Vijay Pandey", "Sanjay Jha", "Rekha Sahu", "Prakash Nayak", "Lata Mohanty", "Bablu Khan",
    "Neha Agarwal", "Om Prakash", "Radha Behera", "Santosh Roy", "Farida Begum", "Hari Lal",
    "Savitri Panda",
]
UDHAAR = [50, 60, 80, 100, 120, 150, 200, 250, 300, 350, 400, 500, 600, 750, 1000, 1200, 1500]


def seed(path: str = "data/munim_demo.db", secret: str = "demo-secret", days: int = 90, seed_val: int = 2610) -> Ledger:
    if os.path.exists(path):
        os.remove(path)
    rng = random.Random(seed_val)
    t0 = datetime(2026, 8, 1, 9, 0, tzinfo=IST)

    class FakeClock:
        t = t0.timestamp()

        def __call__(self):
            return self.t

    clock = FakeClock()
    led = Ledger(path, secret, clock=clock)
    custs = [led.add_customer(n) for n in NAMES]
    consenting = set(rng.sample(range(len(custs)), 15))
    for day in range(days):
        clock.t = (t0 + timedelta(days=day, minutes=rng.randint(0, 600))).timestamp()
        for _ in range(rng.randint(2, 6)):
            c = rng.choice(custs)
            clock.t += rng.randint(60, 3000)
            if rng.random() < 0.68:
                led.add_udhaar(c.id, rng.choice(UDHAAR) * 100, src="seed")
            else:
                bal = led.balance(c.id)
                if bal > 0:
                    portion = rng.choice([0.25, 0.5, 1.0])
                    pay = max(100, int(bal * portion) // 100 * 100)
                    led.record_payment(c.id, min(pay, bal), src="seed")
    for i in consenting:
        led.grant_consent(custs[i].id)
    # demo of the ledger's safety nets
    led.open_dispute(custs[7].id, None, "synthetic dispute for demo")
    led.set_flag(custs[9].id, paused=1)
    clock.t = datetime(2026, 10, 31, 10, 0, tzinfo=IST).timestamp()
    return led


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "data/munim_demo.db"
    L = seed(out)
    ok, _, msg = L.verify()
    total = sum(max(0, L.balance(c.id)) for c in L.customers())
    print(f"seeded {len(L.customers())} customers, {len(L.entries())} entries -> {out}")
    print(f"chain: {msg}; outstanding = {total / 100:,.0f} rupees")
