"""Collections forecast (next H days): empirical conditional-survival model over each customer's own payment gaps.

For a customer last paid d days ago with historical gaps g_i:
    p = P(gap <= d + H | gap > d)        (empirical, from their own history)
    <2 payments on record  -> low fixed prior;   overdue beyond every past gap -> low prior
    a pending promise lifts p to at least that customer's historical promise-kept rate (default 0.6)
Expected collection = sum p * min(median payment, balance);  the interval comes from sum p(1-p)*amount^2 (normal approx).
`customer_forecast` is a PURE function so the backtest can replay history without leaking the future.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from munim.ledger.chain import Ledger

from . import engine as E

DAY = 86400.0
PRIOR_FEW, PRIOR_OVERDUE, PROMISE_DEFAULT = 0.15, 0.25, 0.6


@dataclass
class CustForecast:
    customer_id: int
    p: float
    amount: int

    @property
    def expected(self) -> float:
        return self.p * self.amount


def customer_forecast(cid: int, pay_ts: list[float], pay_amt: list[int], balance: int, now: float, horizon_days: int = 14,
                      promise_pending: bool = False, kept_rate: float | None = None) -> CustForecast:
    if balance <= 0:
        return CustForecast(cid, 0.0, 0)
    m = int(statistics.median(pay_amt)) if pay_amt else balance
    amount = min(max(m, 1), balance)
    if len(pay_ts) < 3:
        p = PRIOR_FEW
    else:
        gaps = [(b - a) / DAY for a, b in zip(pay_ts, pay_ts[1:])]
        d = (now - pay_ts[-1]) / DAY
        later = [g for g in gaps if g >= d]
        if not later:
            p = PRIOR_OVERDUE
        else:
            p = sum(1 for g in later if g <= d + horizon_days) / len(later)
    if promise_pending:
        p = max(p, PROMISE_DEFAULT if kept_rate is None else kept_rate)
    return CustForecast(cid, round(min(max(p, 0.0), 0.97), 4), amount)


def forecast(ledger: Ledger, now: float, horizon_days: int = 14) -> dict:
    rows = []
    for c, bal in E.outstanding(ledger):
        pays = [e for e in E.live_money_entries(ledger, c.id) if e.kind == "record_payment"]
        sts = [st for _, st in E.promise_statuses(ledger, c.id, now)]
        kept, broken = sts.count("kept"), sts.count("broken")
        kr = kept / (kept + broken) if kept + broken else None
        rows.append(customer_forecast(c.id, [e.ts for e in pays], [e.amount_paise for e in pays], bal, now, horizon_days,
                                      E.active_promise(ledger, c.id, now) is not None, kr))
    exp = sum(r.expected for r in rows)
    var = sum(r.p * (1 - r.p) * r.amount ** 2 for r in rows)
    sd = var ** 0.5
    return {"horizon_days": horizon_days, "expected_paise": int(exp), "low_paise": int(max(0, exp - 1.64 * sd)),
            "high_paise": int(exp + 1.64 * sd), "customers": rows}
