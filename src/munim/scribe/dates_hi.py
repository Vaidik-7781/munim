"""Hindi/Hinglish date phrases for promise-to-pay ("shukravar tak", "parson", "15 tarikh tak", "do din mein").

Returns a date plus an `ambiguous` note when the phrase can mean two things (kal = tomorrow/yesterday).
In a PROMISE context the future reading is used, and the bot shows the resolved date for confirmation.
Never guesses silently: unknown phrases return None and the bot asks "kab tak?".
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

from unidecode import unidecode

from .indic_numbers import parse_amount

WEEKDAYS = {
    "somvar": 0, "somwar": 0, "monday": 0, "mon": 0, "सोमवार": 0,
    "mangalvar": 1, "mangalwar": 1, "tuesday": 1, "tue": 1, "मंगलवार": 1,
    "budhvar": 2, "budhwar": 2, "wednesday": 2, "wed": 2, "बुधवार": 2,
    "guruvar": 3, "guruwar": 3, "brihaspativar": 3, "veervar": 3, "thursday": 3, "thu": 3, "गुरुवार": 3, "बृहस्पतिवार": 3,
    "shukravar": 4, "shukrawar": 4, "friday": 4, "fri": 4, "शुक्रवार": 4,
    "shanivar": 5, "shaniwar": 5, "saturday": 5, "sat": 5, "शनिवार": 5,
    "ravivar": 6, "raviwar": 6, "itwar": 6, "itvar": 6, "sunday": 6, "sun": 6, "रविवार": 6, "इतवार": 6,
}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12})
MONTHS.update({"janvari": 1, "farvari": 2, "march": 3, "april": 4, "mai": 5, "june": 6, "julai": 7, "agast": 8, "sitambar": 9,
               "aktubar": 10, "navambar": 11, "disambar": 12})


@dataclass
class When:
    day: date
    phrase: str
    ambiguous: str | None = None


def _norm(t: str) -> str:
    return " ".join(t.lower().replace(",", " ").split())


def _end_of_month(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _next_dom(today: date, dom: int) -> date | None:
    """Next occurrence of day-of-month `dom` (today counts); rolls to following months, skipping short months."""
    y, m = today.year, today.month
    for _ in range(14):
        if dom <= calendar.monthrange(y, m)[1]:
            cand = date(y, m, dom)
            if cand >= today:
                return cand
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return None


def parse_when(text: str, today: date) -> When | None:
    t = unidecode(text).lower() if not any("ऀ" <= c <= "ॿ" for c in text) else text.lower()
    t = _norm(t)
    # relative words ---------------------------------------------------------------
    if re.search(r"\b(parso|parson|parsoon)\b|परसों", t):
        return When(today + timedelta(days=2), "parson", "parson can also mean 2 days ago; future assumed")
    if re.search(r"\bnarson\b|नरसों", t):
        return When(today + timedelta(days=3), "narson", "narson can also mean 3 days ago; future assumed")
    if re.search(r"\b(kal)\b|कल", t):
        return When(today + timedelta(days=1), "kal", "kal can also mean yesterday; future assumed")
    if re.search(r"\b(aaj|ajj)\b|आज", t):
        return When(today, "aaj")
    if re.search(r"agle\s+hafte|next\s+week|agla\s+hafta|अगले हफ्ते", t):
        return When(today + timedelta(days=7), "agle hafte")
    if re.search(r"hafte\s+bhar\s+(mein|me)|ek\s+hafte\s+(mein|me)|hafte\s+mein", t):
        return When(today + timedelta(days=7), "hafte bhar mein")
    if re.search(r"mahine\s+(ke\s+)?(end|ant|akhir|aakhir)|month\s*end|महीने के अंत", t):
        return When(_end_of_month(today), "mahine ke end")
    if re.search(r"agle\s+mahine|next\s+month", t):
        nm = today.replace(day=1) + timedelta(days=32)
        return When(nm.replace(day=1), "agle mahine")
    # "N din mein/baad" ----------------------------------------------------------------
    m = re.search(r"(\S+)\s+din\s+(mein|me|baad|bad|tak)", t)
    if m:
        n = _small_int(m.group(1))
        if n is not None and 0 < n <= 60:
            return When(today + timedelta(days=n), f"{n} din")
    # weekday ------------------------------------------------------------------------------
    for tok in re.findall(r"[a-zऀ-ॿ]+", t):
        if tok in WEEKDAYS:
            wd = WEEKDAYS[tok]
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7      # "somvar tak" said on a Monday means next Monday
            return When(today + timedelta(days=delta), tok)
    # "15 tarikh" / "15 ko" / "15 november" ----------------------------------------------
    m = re.search(r"(\d{1,2})\s*(tarikh|tareekh|taarikh|तारीख|ko|tak)?\s*(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")?\b", t)
    if m and (m.group(2) in ("tarikh", "tareekh", "taarikh", "तारीख") or m.group(3)):
        dom = int(m.group(1))
        if 1 <= dom <= 31:
            if m.group(3):
                mon = MONTHS[m.group(3)]
                y = today.year if (mon, dom) >= (today.month, today.day) else today.year + 1
                try:
                    return When(date(y, mon, dom), f"{dom} {m.group(3)}")
                except ValueError:
                    return None
            d = _next_dom(today, dom)
            return When(d, f"{dom} tarikh") if d else None
    return None


def _small_int(tok: str) -> int | None:
    if tok.isdigit():
        return int(tok)
    v = parse_amount(tok)
    return v // 100 if v is not None else None


WEEKDAY_HI = ["Somvar", "Mangalvar", "Budhvar", "Guruvar", "Shukravar", "Shanivar", "Ravivar"]


def describe(d: date) -> str:
    return f"{WEEKDAY_HI[d.weekday()]} {d.day} {calendar.month_abbr[d.month]}"


_STRIP = [
    r"\b\S+\s+din\s+(mein|me|baad|bad|tak)\b", r"\b\d{1,2}\s*(tarikh|tareekh|taarikh|तारीख)(\s+(ko|tak))?",
    r"\b\d{1,2}\s+(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b",
    r"\bmahine\s+(ke\s+)?(end|ant|akhir|aakhir)\b", r"\bagle\s+(hafte|mahine)\b", r"\bhafte\s+(bhar\s+)?(mein|me)\b",
    r"\b(parson|parso|narson|kal|aaj)\b", r"(परसों|नरसों|कल|आज)",
] + [r"\b" + w + r"\b" for w in WEEKDAYS if w.isascii()] + [w for w in WEEKDAYS if not w.isascii()]


def strip_date_phrases(text: str) -> str:
    """Remove date words so e.g. '15 tarikh' or 'teen din mein' are not misread as money."""
    for pat in _STRIP:
        text = re.sub(pat, " _ ", text, flags=re.I)
    return text
