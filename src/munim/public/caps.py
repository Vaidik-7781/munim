"""Spend protection for public mode: per-user daily cap, global daily cap, kill switch."""
from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Callable

IST_OFFSET = 5.5 * 3600


class PublicCaps:
    def __init__(self, per_user: int = 3, global_cap: int = 60, clock: Callable[[], float] = time.time,
                 kill_file: str | None = None):
        self.per_user, self.global_cap, self._clock = per_user, global_cap, clock
        self.kill_file = kill_file
        self._killed = False
        self._day = self._today()
        self._user: dict[int, int] = defaultdict(int)
        self._total = 0

    def _today(self) -> int:
        return int((self._clock() + IST_OFFSET) // 86400)

    def _roll(self) -> None:
        d = self._today()
        if d != self._day:
            self._day, self._user, self._total = d, defaultdict(int), 0

    def kill(self) -> None:
        self._killed = True

    def is_killed(self) -> bool:
        return self._killed or bool(self.kill_file and os.path.exists(self.kill_file))

    def check(self, user_id: int) -> tuple[bool, str]:
        self._roll()
        if self.is_killed():
            return False, "killed"
        if self._total >= self.global_cap:
            return False, "global_cap"
        if self._user[user_id] >= self.per_user:
            return False, "user_cap"
        return True, "ok"

    def consume(self, user_id: int) -> None:
        self._roll()
        self._user[user_id] += 1
        self._total += 1

    def remaining(self, user_id: int) -> int:
        self._roll()
        return max(0, self.per_user - self._user[user_id])
