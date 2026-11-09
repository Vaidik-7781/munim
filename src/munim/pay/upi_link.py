"""UPI collect helpers: a pay link + QR for a reminder or counter. Amount is exact decimal from integer paise."""
from __future__ import annotations

import io
import re
from decimal import Decimal
from urllib.parse import quote

import qrcode

_VPA = re.compile(r"^[A-Za-z0-9._-]{2,64}@[A-Za-z][A-Za-z0-9]{1,31}$")


def valid_vpa(vpa: str) -> bool:
    return bool(_VPA.fullmatch(vpa.strip()))


def upi_uri(vpa: str, payee: str, amount_paise: int, note: str = "Khata") -> str:
    if not valid_vpa(vpa):
        raise ValueError("invalid UPI id")
    if amount_paise <= 0:
        raise ValueError("amount must be positive")
    am = (Decimal(amount_paise) / Decimal(100)).quantize(Decimal("0.01"))
    return (f"upi://pay?pa={quote(vpa.strip(), safe='@')}&pn={quote(payee[:40])}&am={am}&cu=INR&tn={quote(note[:40])}")


def qr_png(uri: str) -> bytes:
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
