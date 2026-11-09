"""Dispute evidence pack: everything the chain knows about one customer's account, with provenance and verification.

Purpose: when a customer says "galat", the shopkeeper can show WHAT was recorded, WHEN, HOW (voice/text/batch/UPI/import),
by whom (owner / helper label), with which uncertainty flags, and that the history has not been edited (hash-chain check).
Weak evidence (low audio quality, name guessed from context, scanned handwriting, customer-claimed payments) is surfaced
honestly rather than hidden. Names are never stored in the chain; the pack prints the customer's name from the local DB.
"""
from __future__ import annotations

import io
from datetime import datetime

from munim.ledger.chain import MONEY_KINDS, Ledger
from munim.scribe.indic_numbers import format_inr
from munim.timeutil import IST

WEAK = {"low_snr": "awaaz saaf nahi thi", "short_clip": "bahut chhoti clip", "customer_inferred": "naam pichli baat se samjha",
        "name_disagreement": "naam alag suna", "amount_disagreement": "do baar alag raqam suni", "b_missing": "dobara jaanch nahi hui",
        "outlier": "raqam aam se zyada", "language_switch": "bhasha alag lagi"}
SRC_HI = {"voice": "voice note", "text": "likhkar", "batch": "ek saath dictation", "upi": "UPI message", "csv": "CSV import",
          "scan": "paper khata scan (haath ki likhai)", "customer_voice": "customer ka dava"}


def build(ledger: Ledger, customer_id: int, shop: str, now: float) -> tuple[str, list[str]]:
    row = ledger.customer_row(customer_id)
    ok, bad_seq, msg = ledger.verify()
    ents = ledger.entries(customer_id)
    rev = {e.reverses_seq: e for e in ents if e.kind == "reversal"}
    lines = [f"{shop} — khata saboot: {row['name']}", f"Taiyaar: {datetime.fromtimestamp(now, IST).strftime('%d %b %Y %H:%M')} IST", "",
             f"Chain jaanch: {'✅ koi badlaav nahi mila' if ok else '❌ GADBAD (entry #' + str(bad_seq) + ')'} — {msg}", ""]
    weak: list[str] = []
    for e in ents:
        d = datetime.fromtimestamp(e.ts, IST).strftime("%d %b %Y %H:%M")
        if e.kind in MONEY_KINDS:
            kind = "udhaar" if e.kind == "add_udhaar" else "jama"
            src = SRC_HI.get(e.meta.get("src", ""), e.meta.get("src", "?"))
            by = e.meta.get("by", "maalik")
            tail = f"  [wapas li gayi #{rev[e.seq].seq}]" if e.seq in rev else ""
            lines.append(f"#{e.seq}  {d}  {kind} {format_inr(e.amount_paise)}  — {src}, {by}, hash {e.hash[:8]}{tail}")
            flags = [WEAK[f] for f in (e.meta.get("flags") or []) if f in WEAK]
            if e.meta.get("src") in ("scan", "customer_voice"):
                flags.append(SRC_HI[e.meta["src"]])
            if flags and e.seq not in rev:
                weak.append(f"#{e.seq}: {', '.join(flags)}" + ("; phir bhi aapne haan dabaya" if e.meta.get("confirmed") else ""))
        elif e.kind == "reversal":
            lines.append(f"#{e.seq}  {d}  wapas (#{e.reverses_seq}) — {e.meta.get('reason', '')}")
        else:
            lines.append(f"#{e.seq}  {d}  {e.kind}")
    live = [e for e in ents if e.kind in MONEY_KINDS and e.seq not in rev]
    bal = ledger.balance(customer_id)
    lines += ["", f"Kul udhaar: {format_inr(sum(e.amount_paise for e in live if e.kind == 'add_udhaar'))}",
              f"Kul jama:   {format_inr(sum(e.amount_paise for e in live if e.kind == 'record_payment'))}",
              f"Baaki:      {format_inr(bal)}", ""]
    if weak:
        lines.append("Kamzor saboot (dhyaan se dekhein):")
        lines += [f"• {w}" for w in weak]
    else:
        lines.append("Kisi entry par koi shaq ka nishaan nahi mila.")
    lines += ["", "Yeh saboot sirf record dikhata hai; faisla customer se baat karke hi karein. Hash badalne par chain toot jaati hai."]
    return "\n".join(lines), weak


def to_pdf(text: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    c.setFont("Helvetica", 9)
    for line in text.split("\n"):
        for chunk in [line[i:i + 105] for i in range(0, max(len(line), 1), 105)]:
            if y < 40:
                c.showPage()
                c.setFont("Helvetica", 9)
                y = 800
            # Helvetica has no Devanagari/₹: degrade to ASCII-safe so the PDF never fails
            c.drawString(36, y, chunk.replace("₹", "Rs ").encode("latin-1", "replace").decode("latin-1"))
            y -= 12
    c.save()
    return buf.getvalue()
