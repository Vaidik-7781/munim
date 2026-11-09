"""Backup / portability: a zip with the full hash-chained history (+ the owner's own customer list) that anyone can
verify WITHOUT the secret, and that can rebuild a lost shop database.

ledger.jsonl is exactly the chain (pseudonymous refs, no names); customers.json holds the names and is the owner's own data.
The HMAC secret is never exported. Restore only into an EMPTY ledger of the same shop (same derived secret), and only if
the chain verifies — a tampered backup is refused."""
from __future__ import annotations

import io
import json
import zipfile

from munim.ledger.chain import GENESIS, Ledger, LedgerError


def export_zip(ledger: Ledger, shop_name: str, now: float) -> bytes:
    rows = [dict(r) for r in ledger._db.execute("SELECT * FROM entries ORDER BY seq").fetchall()]
    custs = [dict(r) for r in ledger._db.execute("SELECT * FROM customers ORDER BY id").fetchall()]
    ok, _, msg = ledger.verify()
    manifest = {"shop": shop_name, "exported_at": now, "entries": len(rows), "customers": len(custs),
                "head": rows[-1]["hash"] if rows else GENESIS, "chain_ok": ok, "chain_msg": msg, "format": 1}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr("ledger.jsonl", "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows))
        z.writestr("customers.json", json.dumps(custs, ensure_ascii=False, indent=2))
    return buf.getvalue()


def _load(data: bytes) -> tuple[dict, list[dict], list[dict]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        manifest = json.loads(z.read("manifest.json"))
        text = z.read("ledger.jsonl").decode("utf-8")
        rows = [json.loads(l) for l in text.split("\n") if l.strip()]
        custs = json.loads(z.read("customers.json"))
    return manifest, rows, custs


def verify_export(data: bytes) -> tuple[bool, str]:
    """Recompute the chain from the file alone (no secret needed)."""
    try:
        manifest, rows, _ = _load(data)
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        return False, f"not a valid export ({type(exc).__name__})"
    prev = GENESIS
    for i, r in enumerate(rows, start=1):
        meta = json.loads(r["meta"]) if isinstance(r["meta"], str) else r["meta"]
        if r["seq"] != i or r["prev_hash"] != prev:
            return False, f"chain broken at #{r['seq']}"
        h = Ledger._digest(r["seq"], r["ts"], r["kind"], r["customer_ref"], r["amount_paise"], r["reverses_seq"], meta, prev)
        if h != r["hash"]:
            return False, f"entry #{r['seq']} was edited"
        prev = h
    if manifest.get("head") != prev:
        return False, "head hash does not match the manifest (entries removed or added)"
    return True, f"ok: {len(rows)} entries, head {prev[:12]}"


def restore(ledger: Ledger, data: bytes) -> str:
    ok, msg = verify_export(data)
    if not ok:
        raise LedgerError("refusing to restore: " + msg)
    if ledger._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] or ledger._db.execute("SELECT COUNT(*) FROM customers").fetchone()[0]:
        raise LedgerError("refusing to restore into a non-empty ledger")
    _, rows, custs = _load(data)
    with ledger._lock:
        ledger._db.execute("BEGIN IMMEDIATE")
        try:
            for c in custs:
                cols = list(c.keys())
                ledger._db.execute(f"INSERT INTO customers({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [c[k] for k in cols])
            for r in rows:
                ledger._db.execute(
                    "INSERT INTO entries(seq,ts,kind,customer_ref,amount_paise,reverses_seq,meta,prev_hash,hash) VALUES (?,?,?,?,?,?,?,?,?)",
                    (r["seq"], r["ts"], r["kind"], r["customer_ref"], r["amount_paise"], r["reverses_seq"],
                     r["meta"] if isinstance(r["meta"], str) else json.dumps(r["meta"]), r["prev_hash"], r["hash"]))
            ledger._db.execute("COMMIT")
        except Exception:
            ledger._db.execute("ROLLBACK")
            raise
    ok2, _, m2 = ledger.verify()
    if not ok2:
        raise LedgerError("restored chain failed verification: " + m2)
    return m2
