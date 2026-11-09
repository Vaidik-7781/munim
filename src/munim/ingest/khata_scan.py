"""Paper-khata scan: a photo of a handwritten page -> proposed rows, via a vision LLM.

This is the RISKIEST input in the product (a model reading handwritten numbers), so:
  * opt-in (MUNIM_SCAN=1) because the photo leaves the machine to the LLM provider;
  * the model only transcribes (name, amount, kind) — the amount string is re-parsed by OUR exact decimal parser;
  * rows with doubtful values are dropped, every surviving row is flagged 'scan se padha', and the owner reviews the
    whole list and confirms once; nothing is written before that;
  * at most 40 rows per photo.
"""
from __future__ import annotations

from munim.llm.client import LLMClient, LLMError

from .rows import Plan, Row, attach_customers, clean_name, to_paise

MAX_SCAN_ROWS = 40
SYSTEM = (
    "You transcribe a photographed handwritten shop credit ledger page (khata). Return JSON only: "
    '{"rows": [{"name": "<customer name as written>", "amount": "<digits only as written, no currency>", '
    '"kind": "udhaar" | "payment"}]}. Copy numbers exactly as written; if a figure is unclear, omit the row. '
    "Never guess, never add rows, never compute totals."
)


async def scan_page(ledger, llm: LLMClient, image: bytes, mime: str = "image/jpeg") -> Plan:
    plan = Plan()
    if not llm.enabled:
        plan.rejected.append(("scan", "AI key set nahi hai"))
        return plan
    try:
        raw = await llm.chat_json_image(SYSTEM, "Transcribe this khata page.", image, mime, max_tokens=1500)
    except LLMError:
        plan.rejected.append(("scan", "photo padhi nahi ja saki — saaf roshni mein dobara khinchein ya CSV use karein"))
        return plan
    items = raw.get("rows") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        plan.rejected.append(("scan", "photo se koi row nahi mili"))
        return plan
    for it in items[:MAX_SCAN_ROWS]:
        if not isinstance(it, dict):
            continue
        name, paise = clean_name(it.get("name", "")), to_paise(it.get("amount", ""))
        if name is None or paise is None:
            plan.rejected.append((f"{str(it.get('name'))[:25]} / {str(it.get('amount'))[:12]}", "naam ya raqam saaf nahi — chhoda"))
            continue
        kind = "record_payment" if str(it.get("kind", "")).lower().startswith("pay") else "add_udhaar"
        plan.rows.append(Row(name, paise, kind, None, ["scan se padha — raqam zaroor milayein"]))
    if len(items) > MAX_SCAN_ROWS:
        plan.rejected.append(("scan", f"sirf pehli {MAX_SCAN_ROWS} row li gayi"))
    return attach_customers(ledger, plan)
