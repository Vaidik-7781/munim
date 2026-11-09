"""Programmable udhaar: a customer-consented, capped, revocable standing instruction to settle udhaar from FUTURE inflows.

WHAT THIS IS: a deterministic contract state machine (proposed -> active -> exhausted | expired | revoked) whose terms are
hashed into the shop's hash chain. When an authenticated rail event says "customer X received <credit>", the engine computes
a bounded sweep and asks the RAIL to execute it; the udhaar payment is recorded ONLY after the rail confirms settlement.

WHAT THIS IS NOT (read before demoing):
  * There is no public e-rupee / DBT programmability API a kirana can call. `Rail` is an adapter; `SandboxRail` simulates one.
    A real rail would be the customer's own bank/PSP executing a customer-signed mandate (UPI AutoPay-style e-mandate) or an
    e-rupee programmable-wallet pilot run by a bank. Neither is integrated or verified here.
  * It does NOT guarantee zero default risk. Customers can revoke, run out of inflows, or bank-switch. We report a *coverage
    estimate*, never a guarantee, and never let it replace the risk view.
  * Sweeping government benefits (DBT) is legally and ethically sensitive. DBT sources are OFF unless the customer explicitly
    opts in, and the sweep is hard-capped so the household always keeps the majority (default 25 %, never above 40 %).

Safeguards (enforced in code, tested): customer self-linked consent required to propose; only the customer can accept;
revocation is instant (customer says stop, owner revokes, or a dispute opens); disputed accounts never sweep; per-sweep, total
and percentage caps; expiry <= 365 days; idempotent on rail reference; authenticated events only; customer is told after every sweep.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Protocol

DAY = 86400.0
HARD_CAP_PCT = 40
DEFAULT_CAP_PCT = 25
SOURCES = ("UPI_INFLOW", "WAGE", "DBT")


@dataclass
class Mandate:
    id: int
    customer_id: int
    status: str
    cap_pct: int
    per_sweep_paise: int
    total_cap_paise: int
    swept_paise: int
    sources: list[str]
    expires: float | None
    rail_ref: str | None
    terms_hash: str | None


@dataclass
class CreditEvent:
    mandate_id: int
    rail_ref: str                  # unique id of the inflow on the rail (idempotency key)
    credit_paise: int
    source: str                    # one of SOURCES
    ts: float


class Rail(Protocol):
    def register(self, m: Mandate) -> str: ...
    def verify_event(self, raw: bytes, sig: str) -> CreditEvent | None: ...
    def execute(self, m: Mandate, paise: int, ref: str) -> bool: ...
    def cancel(self, m: Mandate) -> None: ...


class SandboxRail:
    """Simulated rail for demos/tests: HMAC-signed webhook events; execution always settles unless `fail` is set."""
    def __init__(self, secret: bytes = b"sandbox-rail"):
        self.secret, self.fail, self.executed, self.cancelled = secret, False, [], []

    def register(self, m: Mandate) -> str:
        return f"sbx-mandate-{m.id}"

    def sign(self, ev: CreditEvent) -> tuple[bytes, str]:
        raw = json.dumps(ev.__dict__, sort_keys=True).encode()
        return raw, hmac.new(self.secret, raw, hashlib.sha256).hexdigest()

    def verify_event(self, raw: bytes, sig: str) -> CreditEvent | None:
        if not hmac.compare_digest(sig, hmac.new(self.secret, raw, hashlib.sha256).hexdigest()):
            return None
        try:
            return CreditEvent(**json.loads(raw))
        except (TypeError, ValueError):
            return None

    def execute(self, m: Mandate, paise: int, ref: str) -> bool:
        if self.fail:
            return False
        self.executed.append((m.id, paise, ref))
        return True

    def cancel(self, m: Mandate) -> None:
        self.cancelled.append(m.id)


def _m(r) -> Mandate:
    return Mandate(r["id"], r["customer_id"], r["status"], r["cap_pct"], r["per_sweep_paise"], r["total_cap_paise"],
                   r["swept_paise"], json.loads(r["sources"]), r["expires"], r["rail_ref"], r["terms_hash"])


def terms_hash(customer_ref: str, cap_pct: int, per: int, total: int, sources: list[str], days: int) -> str:
    body = {"c": customer_ref, "pct": cap_pct, "per": per, "total": total, "src": sorted(sources), "days": days}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]


class MandateError(Exception):
    pass


class Engine:
    def __init__(self, tenant, rail: Rail):
        self.t, self.rail, self.L, self.db = tenant, rail, tenant.ledger, tenant.aux.db

    # ---- lifecycle -----------------------------------------------------------
    def _self_linked(self, cid: int) -> bool:
        return any(e.kind == "consent_granted" and e.meta.get("how") == "customer_self_linked" for e in self.L.entries(cid))

    def get(self, mid: int) -> Mandate | None:
        r = self.db.execute("SELECT * FROM mandates WHERE id=?", (mid,)).fetchone()
        return _m(r) if r else None

    def for_customer(self, cid: int, *statuses: str) -> Mandate | None:
        q = "SELECT * FROM mandates WHERE customer_id=?" + (f" AND status IN ({','.join('?' * len(statuses))})" if statuses else "") + " ORDER BY id DESC LIMIT 1"
        r = self.db.execute(q, (cid, *statuses)).fetchone()
        return _m(r) if r else None

    def propose(self, cid: int, *, cap_pct: int = DEFAULT_CAP_PCT, per_sweep_paise: int = 100_000,
                total_cap_paise: int | None = None, sources: list[str] | None = None, days: int = 180) -> Mandate:
        if not self._self_linked(cid):
            raise MandateError("customer must link themselves first (invite link): no mandate without their own consent")
        if self.L.is_disputed(cid):
            raise MandateError("account is disputed")
        if not (1 <= cap_pct <= HARD_CAP_PCT):
            raise MandateError(f"cap must be 1–{HARD_CAP_PCT}% so the household keeps most of every inflow")
        sources = sources or ["UPI_INFLOW", "WAGE"]
        if any(s not in SOURCES for s in sources):
            raise MandateError("unknown source")
        if not (1 <= days <= 365) or per_sweep_paise <= 0:
            raise MandateError("bad terms")
        bal = self.L.balance(cid)
        total = min(total_cap_paise or max(bal, 0), max(bal, 0)) if bal > 0 else 0
        if total <= 0:
            raise MandateError("nothing outstanding to secure")
        if self.for_customer(cid, "proposed", "active"):
            raise MandateError("customer already has an open mandate")
        th = terms_hash(self.L.ref(cid), cap_pct, per_sweep_paise, total, sources, days)
        cur = self.db.execute(
            "INSERT INTO mandates(customer_id,status,cap_pct,per_sweep_paise,total_cap_paise,sources,created,expires,terms_hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, "proposed", cap_pct, per_sweep_paise, total, json.dumps(sources), self.t._clock(), self.t._clock() + days * DAY, th))
        self.L.append("mandate_proposed", cid, 0, meta={"id": cur.lastrowid, "terms": th, "pct": cap_pct})
        return self.get(cur.lastrowid)  # type: ignore[return-value]

    def accept(self, mid: int, by_customer: bool) -> Mandate:
        m = self.get(mid)
        if not by_customer:
            raise MandateError("only the customer can accept")
        if m is None or m.status != "proposed" or (m.expires and m.expires < self.t._clock()):
            raise MandateError("no open proposal")
        ref = self.rail.register(m)
        self.db.execute("UPDATE mandates SET status='active', accepted_ts=?, rail_ref=? WHERE id=?", (self.t._clock(), ref, mid))
        self.L.append("mandate_active", m.customer_id, 0, meta={"id": mid, "terms": m.terms_hash, "rail": hashlib.sha256(ref.encode()).hexdigest()[:12]})
        return self.get(mid)  # type: ignore[return-value]

    def revoke(self, mid: int, by: str) -> bool:
        m = self.get(mid)
        if m is None or m.status not in ("proposed", "active"):
            return False
        self.db.execute("UPDATE mandates SET status='revoked' WHERE id=?", (mid,))
        if m.status == "active":
            self.rail.cancel(m)
        self.L.append("mandate_revoked", m.customer_id, 0, meta={"id": mid, "by": by})
        return True

    def revoke_customer(self, cid: int, by: str) -> int:
        n = 0
        for r in self.db.execute("SELECT id FROM mandates WHERE customer_id=? AND status IN ('proposed','active')", (cid,)).fetchall():
            n += self.revoke(r["id"], by)
        return n

    # ---- the inflow -> sweep step ---------------------------------------------
    def sweep_amount(self, m: Mandate, credit_paise: int, balance: int) -> int:
        return max(0, min(balance, credit_paise * m.cap_pct // 100, m.per_sweep_paise, m.total_cap_paise - m.swept_paise))

    def on_event(self, ev: CreditEvent) -> dict:
        m = self.get(ev.mandate_id)
        if m is None or m.status != "active":
            return {"status": "ignored", "why": "no_active_mandate"}
        now = self.t._clock()
        if m.expires and m.expires < now:
            self.db.execute("UPDATE mandates SET status='expired' WHERE id=?", (m.id,))
            return {"status": "ignored", "why": "expired"}
        if ev.source not in m.sources:
            return {"status": "ignored", "why": "source_not_allowed"}
        if ev.credit_paise <= 0 or self.L.is_disputed(m.customer_id) or self.L.customer_row(m.customer_id)["opted_out"]:
            return {"status": "ignored", "why": "blocked"}
        if self.db.execute("SELECT 1 FROM sweeps WHERE rail_ref=?", (ev.rail_ref,)).fetchone():
            return {"status": "ignored", "why": "duplicate_event"}
        paise = self.sweep_amount(m, ev.credit_paise, self.L.balance(m.customer_id))
        if paise <= 0:
            if m.swept_paise >= m.total_cap_paise:
                self.db.execute("UPDATE mandates SET status='exhausted' WHERE id=?", (m.id,))
            return {"status": "nothing_to_sweep"}
        if not self.rail.execute(m, paise, ev.rail_ref):
            return {"status": "rail_failed", "paise": paise}                 # nothing recorded: money did not move
        e = self.L.record_payment(m.customer_id, paise, src="mandate", mandate=m.id, rail=hashlib.sha256(ev.rail_ref.encode()).hexdigest()[:16], confirmed=True)
        self.db.execute("INSERT INTO sweeps(mandate_id,ts,credit_paise,sweep_paise,rail_ref,ledger_seq) VALUES (?,?,?,?,?,?)",
                        (m.id, now, ev.credit_paise, paise, ev.rail_ref, e.seq))
        self.db.execute("UPDATE mandates SET swept_paise=swept_paise+? WHERE id=?", (paise, m.id))
        if m.swept_paise + paise >= m.total_cap_paise:
            self.db.execute("UPDATE mandates SET status='exhausted' WHERE id=?", (m.id,))
        return {"status": "swept", "paise": paise, "seq": e.seq, "customer_id": m.customer_id, "balance": self.L.balance(m.customer_id)}

    def on_webhook(self, raw: bytes, sig: str) -> dict:
        ev = self.rail.verify_event(raw, sig)
        return {"status": "rejected", "why": "bad_signature"} if ev is None else self.on_event(ev)

    # ---- honest exposure view ------------------------------------------------------
    def coverage(self) -> dict:
        """Share of outstanding udhaar that sits under an ACTIVE mandate's remaining cap. An estimate of *capacity*, not a guarantee."""
        total = sum(max(0, self.L.balance(c.id)) for c in self.L.customers())
        covered = 0
        for r in self.db.execute("SELECT * FROM mandates WHERE status='active'").fetchall():
            m = _m(r)
            covered += min(max(0, self.L.balance(m.customer_id)), m.total_cap_paise - m.swept_paise)
        return {"outstanding_paise": total, "covered_paise": covered,
                "covered_pct": int(100 * covered / total) if total else 0,
                "note": "capacity under active mandates; customers can revoke and inflows may not arrive — not a default guarantee"}
