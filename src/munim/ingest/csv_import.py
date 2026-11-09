"""CSV / spreadsheet export import: name + outstanding amount (+ optional date). Phone numbers and other columns are ignored
on purpose (never stored). Deterministic; no AI involved."""
from __future__ import annotations

import csv
import io

from .rows import MAX_ROWS, Plan, Row, attach_customers, clean_name, to_paise

NAME_COLS = ("name", "customer", "naam", "party", "party name", "customer name")
AMT_COLS = ("amount", "balance", "baaki", "udhaar", "outstanding", "due", "bakaya")
DATE_COLS = ("date", "tareekh", "as of", "as_of")
MAX_BYTES = 300_000


def parse_csv(ledger, data: bytes) -> Plan:
    plan = Plan()
    if len(data) > MAX_BYTES:
        plan.rejected.append(("file", f"bahut bada file (max {MAX_BYTES // 1000} KB)"))
        return plan
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    sample = text[:2000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rd = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in rd if any(c.strip() for c in r)]
    if not rows:
        plan.rejected.append(("file", "khaali file"))
        return plan
    head = [h.strip().lower() for h in rows[0]]
    ni = next((i for i, h in enumerate(head) if h in NAME_COLS), None)
    ai = next((i for i, h in enumerate(head) if h in AMT_COLS), None)
    di = next((i for i, h in enumerate(head) if h in DATE_COLS), None)
    body = rows[1:]
    if ni is None or ai is None:
        # headerless two-column file: name, amount
        if len(rows[0]) >= 2 and to_paise(rows[0][1]) is not None:
            ni, ai, di, body = 0, 1, None, rows
        else:
            plan.rejected.append(("header", "naam aur raqam ka column nahi mila (name, amount likhein)"))
            return plan
    for i, r in enumerate(body[:MAX_ROWS], start=2):
        if max(ni, ai) >= len(r):
            plan.rejected.append((f"line {i}", "adhoori line"))
            continue
        name, paise = clean_name(r[ni]), to_paise(r[ai])
        if name is None:
            plan.rejected.append((f"line {i}: {r[ni][:30]!r}", "naam saaf nahi"))
        elif paise is None:
            plan.rejected.append((f"line {i}: {name}", f"raqam {r[ai][:15]!r} sahi nahi (sirf positive, ₹1 crore tak)"))
        else:
            plan.rows.append(Row(name, paise, "add_udhaar", r[di].strip() if di is not None and di < len(r) else None))
    if len(body) > MAX_ROWS:
        plan.rejected.append(("file", f"sirf pehli {MAX_ROWS} line li gayi"))
    return attach_customers(ledger, plan)
