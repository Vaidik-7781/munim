"""'Break Munim': anyone can send a voice note to a SANDBOX ledger and see exactly what Munim heard,
what it flagged, and why it did (or did not) write. Nothing is persisted; no real data involved."""
from __future__ import annotations

import os
import tempfile

from munim.audit.auditor import AuditContext, audit
from munim.config import Settings
from munim.ledger.chain import Ledger
from munim.scribe.extract import extract
from munim.scribe.indic_numbers import format_inr

from .pii import redact

SANDBOX_NAMES = ["Ramesh Sharma", "Dinesh Sharma", "Sunita Verma", "Mohan Das", "Anil Gupta"]


def sandbox_ledger(secret: str = "sandbox") -> Ledger:
    path = os.path.join(tempfile.mkdtemp(prefix="munim_sbx_"), "sbx.db")
    led = Ledger(path, secret)
    for n in SANDBOX_NAMES:
        led.add_customer(n)
    return led


def trace(a_text: str, b_text: str | None, snr: float | None, dur: float | None, cfg: Settings,
          led: Ledger | None = None) -> str:
    led = led or sandbox_ledger()
    custs = led.customers()
    ea = extract(a_text, custs)
    eb = extract(b_text, custs) if b_text is not None else None
    res = audit(AuditContext(a=ea, b=eb, snr_db=snr, duration_s=dur), cfg)
    lines = [
        f"Pass A heard : “{redact(a_text)}”",
        f"Pass B heard : “{redact(b_text) if b_text is not None else '— unavailable —'}”",
        f"Intent       : {res.intent}",
        f"Customer     : {res.customer.name if res.customer else ('ambiguous: ' + ', '.join(c.name for c in res.candidates) if res.candidates else 'not found')}",
        f"Amount       : {format_inr(res.amount_paise) if res.amount_paise is not None else '—'}"
        + (f"  (other pass: {format_inr(res.alt_amount_paise)})" if res.alt_amount_paise else ""),
        f"Audio        : SNR {snr:.1f} dB, {dur:.1f}s" if snr is not None and dur is not None else "Audio        : —",
        f"Flags        : {', '.join(res.codes) or 'none'}",
        {"SOFT": "Decision     : ✅ would WRITE now (30 s undo)",
         "BLOCK": "Decision     : ✋ would ASK you to confirm first — nothing written",
         "ASK": "Decision     : ❓ would ask a clarifying question",
         "ACT": "Decision     : ▶️ would act (no money written)"}[res.decision],
        "Sandbox only — nothing was saved.",
    ]
    return "\n".join(lines)
