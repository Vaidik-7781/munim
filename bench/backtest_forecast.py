"""Rolling-origin backtest of the collections forecast on a ledger (default: the realistic BigBasket-priced simulation).

    python bench/backtest_forecast.py [path_to_ledger.db]     # or builds a simulated one if none given
For each weekly cut-off t: use ONLY data <= t, forecast next 14 days, compare with what actually came in.
Reports: per-customer Brier score vs a constant-rate baseline, and total-collection error (MAPE) vs a naive
'same as the last 14 days' baseline.  NOTE: simulated payers follow the simulator's archetypes, so this validates the
pipeline and leakage-free evaluation, NOT real-world accuracy. Run it on a real shop's ledger for that.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import statistics  # noqa: E402

from munim.insights.forecast import DAY, customer_forecast  # noqa: E402
from munim.ledger.chain import Ledger  # noqa: E402

H = 14


def backtest(led: Ledger, weeks: int = 8) -> dict:
    ents = {c.id: [e for e in led.entries(c.id) if e.kind in ("add_udhaar", "record_payment")] for c in led.customers()}
    last = max(e.ts for es in ents.values() for e in es)
    cuts = [last - (k * 7 + H) * DAY for k in range(weeks)][::-1]
    brier_m, brier_b, errs_m, errs_n, rows = [], [], [], [], []
    base_rate = None
    for cut in cuts:
        pred_total = actual_total = naive_total = 0.0
        outcomes = []
        for cid, es in ents.items():
            hist = [e for e in es if e.ts <= cut]
            bal = sum(e.amount_paise if e.kind == "add_udhaar" else -e.amount_paise for e in hist)
            if bal <= 0:
                continue
            pays = [e for e in hist if e.kind == "record_payment"]
            f = customer_forecast(cid, [e.ts for e in pays], [e.amount_paise for e in pays], bal, cut, H)
            fut = sum(e.amount_paise for e in es if e.kind == "record_payment" and cut < e.ts <= cut + H * DAY)
            paid = 1.0 if fut > 0 else 0.0
            outcomes.append((f.p, paid))
            pred_total += f.expected
            actual_total += min(fut, bal)
        naive_total = sum(e.amount_paise for es in ents.values() for e in es
                          if e.kind == "record_payment" and cut - H * DAY < e.ts <= cut)
        if not outcomes:
            continue
        if base_rate is None:
            base_rate = statistics.mean(o for _, o in outcomes)
        brier_m += [(p - o) ** 2 for p, o in outcomes]
        brier_b += [(base_rate - o) ** 2 for _, o in outcomes]
        if actual_total > 0:
            errs_m.append(abs(pred_total - actual_total) / actual_total)
            errs_n.append(abs(naive_total - actual_total) / actual_total)
        rows.append((pred_total, actual_total, naive_total))
    return {"n_cutoffs": len(rows), "brier_model": statistics.mean(brier_m), "brier_constant": statistics.mean(brier_b),
            "mape_model": statistics.mean(errs_m), "mape_naive": statistics.mean(errs_n)}


if __name__ == "__main__":
    if len(sys.argv) > 1:
        led = Ledger(sys.argv[1], "x")
    else:
        from data.real.build_realistic_ledger import FALLBACK_FIRST, build, load_names  # noqa
        import random
        prices = [20, 35, 45, 60, 75, 99, 120, 140, 18, 55, 30, 250, 12, 80, 65]
        led = build(prices, load_names(None, random.Random(1), 40), "/tmp/backtest.db", days=240)
    r = backtest(led)
    print({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
