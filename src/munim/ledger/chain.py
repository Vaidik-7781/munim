"""Append-only, hash-chained udhaar ledger (SQLite, WAL).

* Money is integer paise. Entries are never edited or deleted: undo = a *reversal* entry.
* Each entry hash = SHA-256(canonical JSON incl. prev_hash). Genesis prev_hash = 64 zeros.
* The chain holds only an HMAC pseudonym (customer_ref), never a name or phone number, so a
  customer can be erased (names dropped) without breaking verification.
* `verify()` recomputes every hash; any retroactive edit is detected and pinpointed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from munim.scribe.names import Customer

GENESIS = "0" * 64
MONEY_KINDS = {"add_udhaar", "record_payment"}
SIGN = {"add_udhaar": 1, "record_payment": -1}

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, aliases TEXT DEFAULT '[]', lang TEXT DEFAULT 'hi',
  consent TEXT DEFAULT 'none',            -- none | granted | revoked
  paused INTEGER DEFAULT 0, opted_out INTEGER DEFAULT 0, erased INTEGER DEFAULT 0,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS entries (
  seq INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,
  customer_ref TEXT NOT NULL, amount_paise INTEGER NOT NULL DEFAULT 0,
  reverses_seq INTEGER, meta TEXT NOT NULL DEFAULT '{}',
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_ref ON entries(customer_ref);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, reason TEXT, priority REAL DEFAULT 0, status TEXT DEFAULT 'pending',
  created_at REAL, decided_at REAL
);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL, ts REAL NOT NULL, kind TEXT
);
"""


def canonical(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class Entry:
    seq: int
    ts: float
    kind: str
    customer_ref: str
    amount_paise: int
    reverses_seq: int | None
    meta: dict[str, Any]
    prev_hash: str
    hash: str


class LedgerError(Exception):
    pass


class Ledger:
    def __init__(self, path: str, hmac_secret: str, clock: Callable[[], float] = time.time):
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(customers)")}
        if "credit_limit" not in cols:     # lightweight migration for older DBs
            self._db.execute("ALTER TABLE customers ADD COLUMN credit_limit INTEGER")
        self._secret = hmac_secret.encode()
        self._clock = clock
        self._lock = threading.RLock()

    def close(self) -> None:
        self._db.close()

    # -- customers ---------------------------------------------------------------------
    def ref(self, customer_id: int) -> str:
        return hmac.new(self._secret, f"cust:{customer_id}".encode(), hashlib.sha256).hexdigest()[:24]

    def add_customer(self, name: str, aliases: list[str] | None = None, lang: str = "hi") -> Customer:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO customers(name, aliases, lang, created_at) VALUES (?,?,?,?)",
                (name, json.dumps(aliases or [], ensure_ascii=False), lang, self._clock()),
            )
            return Customer(cur.lastrowid, name, aliases or [])

    def customers(self, include_erased: bool = False) -> list[Customer]:
        rows = self._db.execute(
            "SELECT id,name,aliases FROM customers" + ("" if include_erased else " WHERE erased=0")
        ).fetchall()
        return [Customer(r["id"], r["name"], json.loads(r["aliases"])) for r in rows]

    def customer_row(self, customer_id: int) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no customer {customer_id}")
        return row

    def set_flag(self, customer_id: int, **fields: Any) -> None:
        allowed = {"paused", "opted_out", "consent", "lang", "credit_limit"}
        for k in fields:
            if k not in allowed:
                raise LedgerError(f"bad field {k}")
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE customers SET {sets} WHERE id=?", (*fields.values(), customer_id))

    # -- chain -------------------------------------------------------------------------
    def _head(self) -> tuple[int, str]:
        row = self._db.execute("SELECT seq, hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
        return (row["seq"], row["hash"]) if row else (0, GENESIS)

    @staticmethod
    def _digest(seq: int, ts: float, kind: str, ref: str, amount: int, rev: int | None,
                meta: dict[str, Any], prev: str) -> str:
        body = {"seq": seq, "ts": ts, "kind": kind, "customer_ref": ref, "amount_paise": amount,
                "reverses_seq": rev, "meta": meta, "prev_hash": prev}
        return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()

    def append(self, kind: str, customer_id: int, amount_paise: int = 0, *,
               meta: dict[str, Any] | None = None, reverses_seq: int | None = None) -> Entry:
        if kind in MONEY_KINDS and amount_paise <= 0:
            raise LedgerError("money entries need a positive amount (paise)")
        if not isinstance(amount_paise, int) or isinstance(amount_paise, bool):
            raise LedgerError("amount must be integer paise")
        meta = meta or {}
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                seq0, prev = self._head()
                seq, ts = seq0 + 1, round(self._clock(), 3)
                ref = self.ref(customer_id)
                h = self._digest(seq, ts, kind, ref, amount_paise, reverses_seq, meta, prev)
                self._db.execute(
                    "INSERT INTO entries(seq,ts,kind,customer_ref,amount_paise,reverses_seq,meta,prev_hash,hash)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (seq, ts, kind, ref, amount_paise, reverses_seq, canonical(meta), prev, h),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return Entry(seq, ts, kind, ref, amount_paise, reverses_seq, meta, prev, h)

    def _row_to_entry(self, r: sqlite3.Row) -> Entry:
        return Entry(r["seq"], r["ts"], r["kind"], r["customer_ref"], r["amount_paise"],
                     r["reverses_seq"], json.loads(r["meta"]), r["prev_hash"], r["hash"])

    def entries(self, customer_id: int | None = None) -> list[Entry]:
        if customer_id is None:
            rows = self._db.execute("SELECT * FROM entries ORDER BY seq").fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM entries WHERE customer_ref=? ORDER BY seq", (self.ref(customer_id),)
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    # -- money operations --------------------------------------------------------------
    def add_udhaar(self, customer_id: int, paise: int, **meta: Any) -> Entry:
        return self.append("add_udhaar", customer_id, paise, meta=meta)

    def record_payment(self, customer_id: int, paise: int, **meta: Any) -> Entry:
        return self.append("record_payment", customer_id, paise, meta=meta)

    def _reversed_seqs(self) -> set[int]:
        rows = self._db.execute("SELECT reverses_seq FROM entries WHERE kind='reversal'").fetchall()
        return {r["reverses_seq"] for r in rows}

    def last_reversible(self, customer_id: int | None = None) -> Entry | None:
        reversed_ = self._reversed_seqs()
        for e in reversed(self.entries(customer_id)):
            if e.kind in MONEY_KINDS and e.seq not in reversed_:
                return e
        return None

    def undo_last(self, customer_id: int | None = None, reason: str = "user_undo") -> Entry | None:
        target = self.last_reversible(customer_id)
        if target is None:
            return None
        cid = self._customer_id_for_ref(target.customer_ref)
        return self.append("reversal", cid, target.amount_paise, meta={"reason": reason, "of": target.kind},
                           reverses_seq=target.seq)

    def reverse_seq(self, seq: int, reason: str = "user_undo") -> Entry | None:
        """Reverse ONE specific money entry (used for batch undo). Never deletes; no-op if already reversed."""
        if seq in self._reversed_seqs():
            return None
        target = next((e for e in self.entries() if e.seq == seq and e.kind in MONEY_KINDS), None)
        if target is None:
            return None
        cid = self._customer_id_for_ref(target.customer_ref)
        return self.append("reversal", cid, target.amount_paise, meta={"reason": reason, "of": target.kind},
                           reverses_seq=target.seq)

    def pseudo(self, label: str, value: str) -> str:
        """Keyed pseudonym for an external identifier (e.g. a UPI reference) so the chain never holds the raw value."""
        return hmac.new(self._secret, f"{label}:{value}".encode(), hashlib.sha256).hexdigest()[:24]

    def meta_token_seen(self, key: str, token: str) -> bool:
        """True if any live (non-reversed) money entry carries meta[key] == token. Token must be hex."""
        if not token.isalnum():
            raise LedgerError("token must be alphanumeric")
        rows = self._db.execute("SELECT seq FROM entries WHERE meta LIKE ?", (f'%"{key}":"{token}"%',)).fetchall()
        reversed_ = self._reversed_seqs()
        return any(r["seq"] not in reversed_ for r in rows)

    def _customer_id_for_ref(self, ref: str) -> int:
        for c in self._db.execute("SELECT id FROM customers").fetchall():
            if self.ref(c["id"]) == ref:
                return c["id"]
        raise LedgerError("unknown customer_ref")

    def balance(self, customer_id: int) -> int:
        """Outstanding udhaar in paise (positive = customer owes the shop)."""
        by_seq = {e.seq: e for e in self.entries(customer_id)}
        total = 0
        for e in by_seq.values():
            if e.kind in MONEY_KINDS:
                total += SIGN[e.kind] * e.amount_paise
            elif e.kind == "reversal" and e.reverses_seq in by_seq:
                orig = by_seq[e.reverses_seq]
                total -= SIGN[orig.kind] * orig.amount_paise
        return total

    def history_amounts(self, customer_id: int, kind: str = "add_udhaar", n: int = 20) -> list[int]:
        reversed_ = self._reversed_seqs()
        out = [e.amount_paise for e in self.entries(customer_id) if e.kind == kind and e.seq not in reversed_]
        return out[-n:]

    def recent_writes(self, window_s: float) -> list[tuple[int, str, int, float]]:
        now = self._clock()
        rows = self._db.execute(
            "SELECT * FROM entries WHERE kind IN ('add_udhaar','record_payment') AND ts>=? ORDER BY seq",
            (now - window_s,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                cid = self._customer_id_for_ref(r["customer_ref"])
            except LedgerError:
                continue
            out.append((cid, r["kind"], r["amount_paise"], now - r["ts"]))
        return out

    # -- disputes ----------------------------------------------------------------------
    def open_dispute(self, customer_id: int, seq: int | None = None, note: str = "") -> Entry:
        return self.append("dispute_open", customer_id, 0, meta={"note": note}, reverses_seq=seq)

    def resolve_dispute(self, customer_id: int, note: str = "") -> Entry:
        return self.append("dispute_resolve", customer_id, 0, meta={"note": note})

    def is_disputed(self, customer_id: int) -> bool:
        opened = resolved = 0
        for e in self.entries(customer_id):
            if e.kind == "dispute_open":
                opened += 1
            elif e.kind == "dispute_resolve":
                resolved += 1
        return opened > resolved

    # -- promises (promise-to-pay) -----------------------------------------------------
    def add_promise(self, customer_id: int, due_iso: str, amount_paise: int | None = None, **meta: Any) -> Entry:
        m = {"due": due_iso, "promised_paise": amount_paise, **meta}
        return self.append("promise", customer_id, 0, meta=m)

    def promises(self, customer_id: int) -> list[Entry]:
        return [e for e in self.entries(customer_id) if e.kind == "promise" and e.meta.get("status") != "void"]

    def set_limit(self, customer_id: int, paise: int | None) -> None:
        self.set_flag(customer_id, credit_limit=paise)

    # -- agent approvals queue (NOT part of the money chain; every decision is also chained) --
    def queue_approval(self, customer_id: int, kind: str, payload: dict[str, Any], reason: str = "",
                       priority: float = 0.0) -> int:
        with self._lock:
            dup = self._db.execute(
                "SELECT id FROM approvals WHERE customer_id=? AND kind=? AND status='pending'", (customer_id, kind)).fetchone()
            if dup:
                return dup["id"]
            cur = self._db.execute(
                "INSERT INTO approvals(customer_id,kind,payload,reason,priority,created_at) VALUES (?,?,?,?,?,?)",
                (customer_id, kind, json.dumps(payload, ensure_ascii=False), reason, priority, self._clock()))
            return cur.lastrowid

    def pending_approvals(self) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM approvals WHERE status='pending' ORDER BY priority DESC, id").fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def decide_approval(self, approval_id: int, approved: bool, by: str = "shopkeeper") -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM approvals WHERE id=? AND status='pending'", (approval_id,)).fetchone()
            if r is None:
                return None
            self._db.execute("UPDATE approvals SET status=?, decided_at=? WHERE id=?",
                             ("approved" if approved else "rejected", self._clock(), approval_id))
        item = {**dict(r), "payload": json.loads(r["payload"])}
        self.append("agent_decision", item["customer_id"], 0,
                    meta={"approval": approval_id, "kind": item["kind"], "approved": approved, "by": by})
        return item

    def log_agent_action(self, customer_id: int, tool: str, **meta: Any) -> Entry:
        return self.append("agent_action", customer_id, 0, meta={"tool": tool, **meta})

    # -- reminders log -----------------------------------------------------------------
    def log_reminder(self, customer_id: int, kind: str = "reminder") -> None:
        with self._lock:
            self._db.execute("INSERT INTO reminders(customer_id,ts,kind) VALUES (?,?,?)",
                             (customer_id, self._clock(), kind))

    def reminder_rows(self) -> list[tuple[int, float]]:
        return [(r["customer_id"], r["ts"]) for r in self._db.execute("SELECT customer_id, ts FROM reminders ORDER BY ts")]

    def reminder_times(self, customer_id: int) -> list[float]:
        rows = self._db.execute("SELECT ts FROM reminders WHERE customer_id=? ORDER BY ts", (customer_id,))
        return [r["ts"] for r in rows.fetchall()]

    # -- consent / erasure -------------------------------------------------------------
    def grant_consent(self, customer_id: int, how: str = "shopkeeper_attested") -> Entry:
        self.set_flag(customer_id, consent="granted")
        return self.append("consent_granted", customer_id, 0, meta={"how": how})

    def revoke_consent(self, customer_id: int) -> Entry:
        self.set_flag(customer_id, consent="revoked", opted_out=1)
        return self.append("consent_revoked", customer_id, 0, meta={})

    def erase_customer(self, customer_id: int) -> None:
        """Drop name/aliases (the only link from the chain's pseudonym to a person)."""
        with self._lock:
            self._db.execute(
                "UPDATE customers SET name='[erased]', aliases='[]', erased=1, opted_out=1, consent='revoked' "
                "WHERE id=?", (customer_id,))
        self.append("erasure", customer_id, 0, meta={})

    # -- verification ------------------------------------------------------------------
    def verify(self) -> tuple[bool, int | None, str]:
        prev, expect_seq = GENESIS, 1
        for r in self._db.execute("SELECT * FROM entries ORDER BY seq").fetchall():
            e = self._row_to_entry(r)
            if e.seq != expect_seq:
                return False, e.seq, f"sequence gap: expected {expect_seq}"
            if e.prev_hash != prev:
                return False, e.seq, "prev_hash mismatch"
            h = self._digest(e.seq, e.ts, e.kind, e.customer_ref, e.amount_paise, e.reverses_seq, e.meta, prev)
            if h != e.hash:
                return False, e.seq, "entry hash mismatch (content edited)"
            prev, expect_seq = e.hash, expect_seq + 1
        return True, None, f"ok: {expect_seq - 1} entries, head {prev[:12]}"


def cli_verify(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else "data/munim.db"
    led = Ledger(path, "verify-only")
    ok, seq, msg = led.verify()
    print(("VERIFIED " if ok else f"TAMPERED at seq {seq}: ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(cli_verify())
