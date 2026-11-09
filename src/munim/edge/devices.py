"""Device auth for headless senders (edge mic, SMS forwarder): per-device HMAC, no secret at rest, replay/stale rejection.

The device secret is derived from the registry master secret (HMAC(master, '<prefix>:<device_id>')), so the DB stores only
ids, labels and flags. `provision` returns the secret ONCE. Signature = HMAC(secret, "device|ts|sha256(body)").
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from collections import defaultdict, deque

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(device_id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, label TEXT, revoked INTEGER DEFAULT 0, muted INTEGER DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS nonces(sig TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS counters(shop_id TEXT, day TEXT, k TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(shop_id,day,k));
"""


class DeviceBook:
    def __init__(self, registry, prefix: str, clock=time.time, rate_per_min: int = 30, max_skew: float = 60.0):
        self.reg, self.prefix, self.clock, self.rate, self.skew = registry, prefix, clock, rate_per_min, max_skew
        self.db = sqlite3.connect(os.path.join(registry.root, f"{prefix}.db"), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._hits: dict[str, deque] = defaultdict(deque)

    def _secret(self, device_id: str) -> bytes:
        return hmac.new(self.reg._master, f"{self.prefix}:{device_id}".encode(), hashlib.sha256).digest()

    def provision(self, shop_id: str, label: str) -> tuple[str, str]:
        did = f"{self.prefix[:3]}_{secrets.token_hex(4)}"
        self.db.execute("INSERT INTO devices VALUES (?,?,?,?,?,?)", (did, shop_id, label[:40], 0, 0, self.clock()))
        return did, self._secret(did).hex()

    def revoke(self, device_id: str) -> None:
        self.db.execute("UPDATE devices SET revoked=1 WHERE device_id=?", (device_id,))

    def set_muted(self, shop_id: str, muted: bool) -> None:
        self.db.execute("UPDATE devices SET muted=? WHERE shop_id=?", (int(muted), shop_id))

    def sign(self, device_id: str, ts: int, body: bytes) -> str:
        msg = f"{device_id}|{ts}|{hashlib.sha256(body).hexdigest()}".encode()
        return hmac.new(self._secret(device_id), msg, hashlib.sha256).hexdigest()

    def authenticate(self, device_id: str, ts: int, body: bytes, sig: str):
        """-> (row, None) or (None, reason). Reasons never reveal which check failed to the caller beyond 'auth'."""
        r = self.db.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        now = self.clock()
        if r is None or r["revoked"] or abs(now - ts) > self.skew or not hmac.compare_digest(sig, self.sign(device_id, ts, body)):
            return None, "auth"
        try:
            self.db.execute("INSERT INTO nonces VALUES (?,?)", (sig, now))
        except sqlite3.IntegrityError:
            return None, "auth"                                            # replay
        self.db.execute("DELETE FROM nonces WHERE ts<?", (now - 3600,))
        q = self._hits[device_id]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.rate:
            return None, "rate"
        q.append(now)
        return r, None

    def count(self, shop_id: str, k: str) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(self.clock()))
        self.db.execute("INSERT INTO counters VALUES (?,?,?,1) ON CONFLICT(shop_id,day,k) DO UPDATE SET n=n+1", (shop_id, day, k))

    def stats(self, shop_id: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.db.execute("SELECT k, SUM(n) n FROM counters WHERE shop_id=? GROUP BY k", (shop_id,)):
            out[r["k"]] = r["n"]
        return out
