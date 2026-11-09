"""Multi-tenant registry: many shops, one process.

Layout:  <root>/registry.db            shops, members (chat -> shop + role), invites
         <root>/shops/<shop_id>.db     one hash-chained ledger per shop (SQLite is the right size for one kirana)
Each shop's ledger HMAC secret is derived from the master secret and the shop id, so pseudonyms never correlate
across shops and a leaked shop DB does not expose another shop's refs.
Scaling path (documented in README): shard shop files across volumes; swap SQLite for Postgres behind Ledger only if
a single shop outgrows it. A chat belongs to at most one shop (simplification).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable

from munim.config import Settings
from munim.ledger.chain import Ledger
from munim.ml.credit_model import CreditModel
from munim.ml.federated import run_round
from munim.service import MunimService

from .quota import GnaniQuota
from .roles import CUSTOMER, OWNER, STAFF

SCHEMA = """
CREATE TABLE IF NOT EXISTS shops(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL, vpa TEXT, active INTEGER DEFAULT 1, pincode TEXT);
CREATE TABLE IF NOT EXISTS members(chat_id INTEGER PRIMARY KEY, shop_id TEXT NOT NULL, role TEXT NOT NULL,
  customer_id INTEGER, label TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_members_shop ON members(shop_id);
CREATE TABLE IF NOT EXISTS api_keys(key_hash TEXT PRIMARY KEY, shop_id TEXT NOT NULL, created_at REAL, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS invites(token_hash TEXT PRIMARY KEY, shop_id TEXT NOT NULL, role TEXT NOT NULL,
  customer_id INTEGER, expires REAL NOT NULL, used INTEGER DEFAULT 0);
"""


@dataclass
class Member:
    chat_id: int
    shop_id: str
    role: str
    customer_id: int | None
    label: str | None


class Tenant:
    def __init__(self, shop_id: str, name: str, ledger: Ledger, service: MunimService, cfg: Settings, vpa: str | None,
                 pay_path: str | None = None, secret: str = "", clock: Callable[[], float] = time.time):
        self.shop_id, self.name, self.ledger, self.service, self.cfg, self.vpa = shop_id, name, ledger, service, cfg, vpa
        self._pay_path, self._secret, self._clock, self._payables = pay_path, secret, clock, None
        self._aux = None

    @property
    def aux(self):
        """Inventory / mandates / insurance / settings store (separate file; see munim.store)."""
        if self._aux is None:
            from munim.store import Aux
            self._aux = Aux((self._pay_path or ":memory:").replace("_payables.db", "_aux.db"))
        return self._aux

    @property
    def payables(self) -> Ledger:
        """Suppliers book (lazy second ledger file for this shop)."""
        if self._payables is None:
            self._payables = Ledger(self._pay_path, self._secret + ":payables", clock=self._clock)
        return self._payables


class Registry:
    def __init__(self, root: str, master_secret: str, cfg: Settings, clock: Callable[[], float] = time.time,
                 quota: GnaniQuota | None = None):
        os.makedirs(os.path.join(root, "shops"), exist_ok=True)
        self.root, self._master, self.cfg, self._clock = root, master_secret.encode(), cfg, clock
        self.quota = quota or GnaniQuota(clock=clock)
        self._db = sqlite3.connect(os.path.join(root, "registry.db"), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        if "pincode" not in {r["name"] for r in self._db.execute("PRAGMA table_info(shops)")}:
            self._db.execute("ALTER TABLE shops ADD COLUMN pincode TEXT")
        self._lock = threading.RLock()
        self._cache: dict[str, Tenant] = {}

    # ---- shops -------------------------------------------------------------
    def _secret(self, shop_id: str) -> str:
        return hmac.new(self._master, f"shop:{shop_id}".encode(), hashlib.sha256).hexdigest()

    def create_shop(self, name: str, owner_chat: int) -> Tenant:
        name = " ".join(name.split())[:60]
        if len(name) < 2:
            raise ValueError("shop name too short")
        with self._lock:
            if self.membership(owner_chat) is not None:
                raise ValueError("chat already belongs to a shop")
            shop_id = secrets.token_hex(5)
            self._db.execute("INSERT INTO shops(id,name,created_at) VALUES (?,?,?)", (shop_id, name, self._clock()))
            self._db.execute("INSERT INTO members(chat_id,shop_id,role,created_at) VALUES (?,?,?,?)",
                             (owner_chat, shop_id, OWNER, self._clock()))
        return self.tenant(shop_id)

    def shops_created_since(self, ts: float) -> int:
        return self._db.execute("SELECT COUNT(*) FROM shops WHERE created_at>=?", (ts,)).fetchone()[0]

    def owner_chats(self) -> list[tuple[str, int]]:
        return [(r["shop_id"], r["chat_id"]) for r in self._db.execute(
            "SELECT shop_id, chat_id FROM members WHERE role=?", (OWNER,)).fetchall()]

    def shop_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM shops WHERE active=1").fetchone()[0]

    def tenant(self, shop_id: str) -> Tenant:
        with self._lock:
            t = self._cache.get(shop_id)
            if t is not None:
                return t
            row = self._db.execute("SELECT * FROM shops WHERE id=? AND active=1", (shop_id,)).fetchone()
            if row is None:
                raise KeyError(shop_id)
            led = Ledger(os.path.join(self.root, "shops", f"{shop_id}.db"), self._secret(shop_id), clock=self._clock)
            svc = MunimService(led, self.cfg, row["name"], clock=self._clock)
            t = self._cache[shop_id] = Tenant(shop_id, row["name"], led, svc, self.cfg, row["vpa"],
                                              os.path.join(self.root, "shops", f"{shop_id}_payables.db"),
                                              self._secret(shop_id), self._clock)
            svc.aux_factory = lambda t=t: t.aux
            from munim import extras
            svc.extras_cb = lambda chat, data, t=t, reg=self: extras.callback(reg, t, chat, data)
            return t

    def set_vpa(self, shop_id: str, vpa: str) -> None:
        with self._lock:
            self._db.execute("UPDATE shops SET vpa=? WHERE id=?", (vpa, shop_id))
            if shop_id in self._cache:
                self._cache[shop_id].vpa = vpa

    def set_pincode(self, shop_id: str, pincode: str) -> None:
        if not (pincode.isdigit() and len(pincode) == 6):
            raise ValueError("pincode must be 6 digits")
        self._db.execute("UPDATE shops SET pincode=? WHERE id=?", (pincode, shop_id))

    def pincode(self, shop_id: str) -> str | None:
        r = self._db.execute("SELECT pincode FROM shops WHERE id=?", (shop_id,)).fetchone()
        return r["pincode"] if r else None

    def shop_ids(self) -> list[str]:
        return [r["id"] for r in self._db.execute("SELECT id FROM shops WHERE active=1").fetchall()]

    # ---- members -----------------------------------------------------------
    def membership(self, chat_id: int) -> Member | None:
        r = self._db.execute("SELECT * FROM members WHERE chat_id=?", (chat_id,)).fetchone()
        return Member(r["chat_id"], r["shop_id"], r["role"], r["customer_id"], r["label"]) if r else None

    def tenant_for(self, chat_id: int) -> tuple[Tenant, Member] | None:
        m = self.membership(chat_id)
        return (self.tenant(m.shop_id), m) if m else None

    def chats(self, shop_id: str, role: str) -> list[int]:
        return [r["chat_id"] for r in self._db.execute(
            "SELECT chat_id FROM members WHERE shop_id=? AND role=?", (shop_id, role)).fetchall()]

    def add_member(self, shop_id: str, chat_id: int, role: str, customer_id: int | None = None,
                   label: str | None = None) -> Member:
        with self._lock:
            if self.membership(chat_id) is not None:
                raise ValueError("chat already belongs to a shop")
            self._db.execute("INSERT INTO members(chat_id,shop_id,role,customer_id,label,created_at) VALUES (?,?,?,?,?,?)",
                             (chat_id, shop_id, role, customer_id, label, self._clock()))
        return self.membership(chat_id)  # type: ignore[return-value]

    def remove_member(self, chat_id: int) -> None:
        self._db.execute("DELETE FROM members WHERE chat_id=? AND role!=?", (chat_id, OWNER))

    def staff_labels(self, shop_id: str) -> dict[int, str]:
        return {r["chat_id"]: r["label"] for r in self._db.execute(
            "SELECT chat_id,label FROM members WHERE shop_id=? AND role=?", (shop_id, STAFF)).fetchall()}

    # ---- API keys (for non-Telegram front-ends: WhatsApp gateway, web app, IVR) ----------
    def create_api_key(self, shop_id: str) -> str:
        key = "mk_" + secrets.token_urlsafe(24)
        self._db.execute("INSERT INTO api_keys(key_hash,shop_id,created_at) VALUES (?,?,?)", (self._h(key), shop_id, self._clock()))
        return key

    def shop_for_api_key(self, key: str) -> str | None:
        r = self._db.execute("SELECT shop_id FROM api_keys WHERE key_hash=? AND revoked=0", (self._h(key),)).fetchone()
        return r["shop_id"] if r else None

    def revoke_api_keys(self, shop_id: str) -> None:
        self._db.execute("UPDATE api_keys SET revoked=1 WHERE shop_id=?", (shop_id,))

    # ---- invites (customers and staff) ------------------------------------
    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def make_invite(self, shop_id: str, role: str, customer_id: int | None = None, ttl_s: float = 7 * 86400) -> str:
        if role not in (CUSTOMER, STAFF):
            raise ValueError("invites are for customers or staff")
        token = secrets.token_urlsafe(12)
        self._db.execute("INSERT INTO invites(token_hash,shop_id,role,customer_id,expires) VALUES (?,?,?,?,?)",
                         (self._h(token), shop_id, role, customer_id, self._clock() + ttl_s))
        return token

    def redeem_invite(self, token: str, chat_id: int, label: str | None = None) -> Member | None:
        """One-time. Returns the new Member, or None if unknown / used / expired / chat already linked."""
        with self._lock:
            r = self._db.execute("SELECT * FROM invites WHERE token_hash=?", (self._h(token),)).fetchone()
            if r is None or r["used"] or r["expires"] < self._clock() or self.membership(chat_id) is not None:
                return None
            self._db.execute("UPDATE invites SET used=1 WHERE token_hash=?", (r["token_hash"],))
            return self.add_member(r["shop_id"], chat_id, r["role"], r["customer_id"], label)

    # ---- federated learning (weights only) ---------------------------------
    @property
    def global_model_path(self) -> str:
        return os.path.join(self.root, "global_model.json")

    def load_global_model(self) -> CreditModel | None:
        try:
            with open(self.global_model_path, encoding="utf-8") as fh:
                return CreditModel.from_json(fh.read())
        except (OSError, ValueError):
            return None

    def federated_round(self, rounds: int = 2, **kw) -> CreditModel | None:
        """Each shop trains on its own ledger; only weights are averaged and saved. Shops with too little data abstain."""
        ids = [r["id"] for r in self._db.execute("SELECT id FROM shops WHERE active=1").fetchall()]
        g = run_round([self.tenant(i).ledger for i in ids], self.load_global_model(), rounds=rounds, **kw)
        if g is not None:
            with open(self.global_model_path, "w", encoding="utf-8") as fh:
                fh.write(g.to_json())
        return g
