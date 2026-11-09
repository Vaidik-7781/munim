"""Gnani credit fairness across shops: a per-shop daily allowance inside a global daily budget.

One noisy shop must not starve the others. Counts are in Gnani calls (not credits; the real cost per call comes from
scripts/g0_check.py). Day boundaries are IST.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

from munim.timeutil import IST


class GnaniQuota:
    def __init__(self, per_shop_daily: int = 300, global_daily: int = 3000, clock: Callable[[], float] = time.time):
        self.per_shop, self.global_cap, self._clock = per_shop_daily, global_daily, clock
        self._day = self._today()
        self._used: dict[str, int] = defaultdict(int)

    def _today(self) -> int:
        return int((self._clock() + IST.utcoffset(None).total_seconds()) // 86400)

    def _roll(self) -> None:
        d = self._today()
        if d != self._day:
            self._day, self._used = d, defaultdict(int)

    @property
    def total(self) -> int:
        return sum(self._used.values())

    def remaining(self, shop: str) -> int:
        self._roll()
        return max(0, min(self.per_shop - self._used[shop], self.global_cap - self.total))

    def allow(self, shop: str, need: int = 3) -> bool:
        return self.remaining(shop) >= need

    def charge(self, shop: str, calls: int) -> None:
        self._roll()
        self._used[shop] += max(0, calls)

    def snapshot(self) -> dict:
        self._roll()
        return {"global_used": self.total, "global_cap": self.global_cap, "per_shop_cap": self.per_shop,
                "shops": dict(self._used)}
