"""Local throughput check for the multi-shop core (no network, no Gnani): N shops, M typed entries each.

    python bench/scale_bench.py [shops=200] [entries=60]
Measures what THIS machine does with the pure-Python/SQLite path (parse -> audit -> chain write). It says nothing about
Gnani latency or Telegram limits, which dominate in production.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from munim.config import Settings  # noqa: E402
from munim.tenancy.registry import Registry  # noqa: E402


def main(shops: int = 200, entries: int = 60) -> None:
    reg = Registry(tempfile.mkdtemp(), "bench", Settings())
    t0 = time.perf_counter()
    tenants = [reg.create_shop(f"Shop {i:04d}", 1000 + i) for i in range(shops)]
    for t in tenants:
        for n in ("Ramesh Sharma", "Sunita Verma", "Mohan Das", "Gita Rani"):
            t.ledger.add_customer(n)
    t1 = time.perf_counter()
    done = 0
    for k in range(entries):
        for i, t in enumerate(tenants):
            name = ("Ramesh", "Sunita", "Mohan", "Gita")[(k + i) % 4]
            r = t.service.on_typed(1000 + i, f"{name} ko {100 + k} rupaye udhaar likh do")
            if t.service.pending.get(1000 + i):
                t.service.on_callback(1000 + i, "yes")
            done += 1
    t2 = time.perf_counter()
    ok = all(t.ledger.verify()[0] for t in tenants[:20])
    print(f"shops={shops}  setup={t1 - t0:.2f}s  messages={done}  time={t2 - t1:.2f}s  "
          f"throughput={done / (t2 - t1):.0f} msg/s  chains_verified(sample 20)={ok}")


if __name__ == "__main__":
    a = sys.argv[1:]
    main(int(a[0]) if a else 200, int(a[1]) if len(a) > 1 else 60)
