"""Learning-to-time reminders: a Thompson-sampling bandit over IST time slots.

Reward = the customer paid something within 72 h of an APPROVED reminder (observed in the shopkeeper's own ledger).
All slots lie inside the Governor's 08:00-19:00 window, so exploration can never break a conduct rule.
With little data it explores; as outcomes accumulate it concentrates on what actually works for THIS shop.
"""
from __future__ import annotations

import random
from datetime import datetime

from munim.ledger.chain import Ledger
from munim.timeutil import IST

SLOTS = {"morning": (8, 11), "midday": (11, 16), "evening": (16, 19)}
SLOT_HI = {"morning": "subah 8–11 baje", "midday": "dopahar 11–4 baje", "evening": "shaam 4–7 baje"}
WINDOW_S = 72 * 3600


def slot_of(ts: float) -> str | None:
    h = datetime.fromtimestamp(ts, IST).hour
    return next((n for n, (lo, hi) in SLOTS.items() if lo <= h < hi), None)


def slot_stats(ledger: Ledger) -> dict[str, tuple[int, int]]:
    """slot -> (successes, trials), from logged (approved) reminders and later payments."""
    pays: dict[int, list[float]] = {}
    for c in ledger.customers(include_erased=True):
        pays[c.id] = [e.ts for e in ledger.entries(c.id) if e.kind == "record_payment"]
    stats = {s: [0, 0] for s in SLOTS}
    for cid, ts in ledger.reminder_rows():
        s = slot_of(ts)
        if s is None:
            continue
        stats[s][1] += 1
        if any(ts < p <= ts + WINDOW_S for p in pays.get(cid, [])):
            stats[s][0] += 1
    return {k: (v[0], v[1]) for k, v in stats.items()}


def thompson_pick(stats: dict[str, tuple[int, int]], rng: random.Random) -> str:
    draws = {s: rng.betavariate(1 + w, 1 + (n - w)) for s, (w, n) in stats.items()}
    return max(draws, key=draws.get)


def suggest_slot(ledger: Ledger, now: float) -> str:
    return thompson_pick(slot_stats(ledger), random.Random(int(now // 86400)))   # stable within a day
