"""Distributor invoice -> structured lines -> stock-in + payable + due-date reminder. Vision/LLM proposes, code validates.

Flow: PDF/photo -> (PDF text layer via pypdf if present, else the image goes to the vision model — opt-in, the invoice leaves
the machine) -> JSON lines -> OUR arithmetic checks (qty x rate ~ line amount, lines + tax ~ total, exact-decimal parsing)
-> owner confirms once -> (a) payables entry, (b) stock-in for lines matched to known SKUs, (c) a dated reminder row.
Any arithmetic mismatch is shown to the owner instead of being "fixed". UNVERIFIED: real distributor invoice layouts vary
enormously (HUL/ITC e-invoices have QR + IRN; we do not parse the e-invoice JSON here); the default vision model id is a guess.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from munim.ingest.rows import to_paise
from munim.inventory.stock import Inventory
from munim.inventory.voice import UNIT
from munim.llm.client import LLMClient

SYSTEM = ("You read one Indian distributor/wholesale invoice. Return ONLY JSON: {\"supplier\":str,\"invoice_no\":str,"
          "\"invoice_date\":\"YYYY-MM-DD\"|null,\"payment_terms_days\":int|null,\"total\":str,\"tax_total\":str|null,"
          "\"lines\":[{\"desc\":str,\"qty\":str,\"unit\":str,\"rate\":str,\"amount\":str,\"mrp\":str|null,\"hsn\":str|null,"
          "\"gst_pct\":str|null}]}. Copy numbers exactly as printed (strings). Do not compute, guess or fix anything. "
          "If a field is not printed use null.")


@dataclass
class Line:
    desc: str
    qty: Decimal | None
    unit: str
    rate_paise: int | None
    amount_paise: int | None
    mrp_paise: int | None
    hsn: str | None
    gst_pct: int | None
    problems: list[str] = field(default_factory=list)
    sku_id: int | None = None
    base_qty: int | None = None
    dim: str | None = None


@dataclass
class Invoice:
    supplier: str
    invoice_no: str
    invoice_date: date | None
    terms_days: int | None
    total_paise: int | None
    tax_paise: int | None
    lines: list[Line]
    problems: list[str] = field(default_factory=list)
    digest: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems and all(not l.problems for l in self.lines) and bool(self.lines)

    @property
    def due(self) -> date | None:
        return self.invoice_date + timedelta(days=self.terms_days) if self.invoice_date and self.terms_days else None


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def validate(raw: dict, inv: Inventory | None = None) -> Invoice:
    lines: list[Line] = []
    for r in (raw.get("lines") or [])[:200]:
        if not isinstance(r, dict):
            continue
        qty = _dec(r.get("qty"))
        rate, amt, mrp = to_paise(r.get("rate")), to_paise(r.get("amount")), to_paise(r.get("mrp")) if r.get("mrp") else None
        gst = _dec(r.get("gst_pct"))
        ln = Line(str(r.get("desc", ""))[:80], qty, str(r.get("unit", "")).lower(), rate, amt, mrp,
                  (str(r["hsn"])[:8] if r.get("hsn") else None), int(gst) if gst is not None and gst == gst.to_integral() else None)
        if qty is None or qty <= 0:
            ln.problems.append("bad_qty")
        if rate is None:
            ln.problems.append("bad_rate")
        if amt is None:
            ln.problems.append("bad_amount")
        if qty and rate is not None and amt is not None:
            expect = int((qty * rate).to_integral_value())
            if abs(expect - amt) > max(100, expect // 100):           # tolerance: Rs 1 or 1 %
                ln.problems.append("qty_x_rate_mismatch")
        if inv is not None and qty and ln.desc:
            m = inv.find(ln.desc)
            u = UNIT.get(ln.unit)
            if m.status == "match" and m.sku:
                ln.sku_id = m.sku.id
                if u and u[0] == m.sku.dim and qty == qty.to_integral_value():
                    ln.dim, ln.base_qty = u[0], int(qty) * u[1]
                else:
                    ln.problems.append("unit_unknown_or_mismatch")
        lines.append(ln)
    total, tax = to_paise(raw.get("total")), (to_paise(raw.get("tax_total")) if raw.get("tax_total") else None)
    d = None
    try:
        d = date.fromisoformat(str(raw.get("invoice_date"))) if raw.get("invoice_date") else None
    except ValueError:
        pass
    terms = raw.get("payment_terms_days")
    terms = int(terms) if isinstance(terms, (int, str)) and str(terms).isdigit() and 0 < int(terms) <= 120 else None
    iv = Invoice(str(raw.get("supplier", ""))[:60], str(raw.get("invoice_no", ""))[:30], d, terms, total, tax, lines)
    if total is None:
        iv.problems.append("no_total")
    else:
        s = sum(l.amount_paise or 0 for l in lines) + (tax or 0)
        s_no_tax = sum(l.amount_paise or 0 for l in lines)
        if abs(s - total) > max(200, total // 100) and abs(s_no_tax - total) > max(200, total // 100):
            iv.problems.append("lines_do_not_sum_to_total")
    if len(iv.supplier) < 2:
        iv.problems.append("no_supplier")
    iv.digest = hashlib.sha256(f"{iv.supplier}|{iv.invoice_no}|{total}".encode()).hexdigest()[:20]
    return iv


def pdf_text(data: bytes) -> str | None:
    try:
        from pypdf import PdfReader
        t = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages[:3])
        return t if len(re.findall(r"\d", t)) > 20 else None
    except Exception:
        return None


async def read_invoice(llm: LLMClient, data: bytes, mime: str, inv: Inventory | None = None) -> Invoice:
    """Text-layer PDFs go as text (cheaper, no image leaves the machine); everything else needs the owner's scan opt-in."""
    if mime == "application/pdf":
        t = pdf_text(data)
        if t:
            return validate(await llm.chat_json(SYSTEM, t[:12000], max_tokens=1800), inv)
        raise ValueError("scanned PDF: send a clear photo of the page instead")
    return validate(await llm.chat_json_image(SYSTEM, "Extract this invoice.", data, mime, max_tokens=1800), inv)


def commit(tenant, iv: Invoice, by: str | None = None) -> dict:
    """After the owner's one confirm: payable + stock-in + due row. Idempotent on the invoice digest."""
    from munim.books import payables
    if tenant.aux.db.execute("SELECT 1 FROM payable_due WHERE invoice_hash=?", (iv.digest,)).fetchone():
        return {"duplicate": True}
    msg = payables.record(tenant.payables, iv.supplier, "bought", iv.total_paise or 0, by=by)
    inv = Inventory(tenant.aux, tenant._clock)
    stocked = 0
    for l in iv.lines:
        if l.sku_id and l.base_qty and not l.problems:
            inv.restock(l.sku_id, l.base_qty, src="invoice", note=iv.invoice_no)
            stocked += 1
    due = iv.due or (date.fromtimestamp(tenant._clock()) + timedelta(days=15))
    tenant.aux.db.execute("INSERT INTO payable_due(ts,supplier,amount_paise,due_iso,invoice_hash) VALUES (?,?,?,?,?)",
                          (tenant._clock(), iv.supplier, iv.total_paise or 0, due.isoformat(), iv.digest))
    return {"duplicate": False, "payables": msg, "stocked_lines": stocked, "unmatched": [l.desc for l in iv.lines if not l.sku_id], "due": due.isoformat()}
