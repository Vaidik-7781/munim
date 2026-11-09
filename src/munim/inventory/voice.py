"""Parse spoken/typed stock lines: "2 kilo aashirvaad atta sold", "stocked 50 packets of Maggi", "maggi 14 rupaye packet".

Deterministic. A sale/restock is only applied when (a) an intent word is present, (b) a quantity with a unit is present,
(c) the item resolves to ONE known SKU (restocks may create a new SKU after the owner says so). Anything else asks.
Quantities: digits, English/Hindi number words, aadha/dedh/dhai/paav. Money words are NOT quantities (see price()).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

from munim.scribe.indic_numbers import parse_amount

SALE = re.compile(r"\b(sold|sale|becha|bechi|beche|bech\s*diya|bik\s*gaya|bik\s*gayi|bika|nikla|nikal\s*gaya)\b|बेचा|बिका", re.I)
RESTOCK = re.compile(r"\b(stocked|restock(ed)?|received|aaya|aayi|aaye|aa\s*gaya|aa\s*gayi|mangwaya|mangaya|maal\s*aaya|utaara|utara)\b|आया|आयी|आई", re.I)
COUNT = re.compile(r"\b(count|ginti|bacha\s*hai|bachi\s*hai|abhi\s*hai)\b", re.I)
PRICE = re.compile(r"\b(rate|price|bhav|bhaav|daam|dam|mrp)\b|(?:rupaye|rupay|rs\.?|₹)\s*(?:per|ka|ki|/)", re.I)

UNIT = {"kg": ("g", 1000), "kilo": ("g", 1000), "kilos": ("g", 1000), "kilogram": ("g", 1000), "किलो": ("g", 1000),
        "gram": ("g", 1), "grams": ("g", 1), "gm": ("g", 1), "g": ("g", 1), "ग्राम": ("g", 1), "paav": ("g", 250), "pav": ("g", 250),
        "litre": ("ml", 1000), "liter": ("ml", 1000), "ltr": ("ml", 1000), "l": ("ml", 1000), "लीटर": ("ml", 1000), "ml": ("ml", 1),
        "packet": ("pc", 1), "packets": ("pc", 1), "pkt": ("pc", 1), "pack": ("pc", 1), "packs": ("pc", 1), "पैकेट": ("pc", 1),
        "piece": ("pc", 1), "pieces": ("pc", 1), "pc": ("pc", 1), "pcs": ("pc", 1), "dabba": ("pc", 1), "dabbe": ("pc", 1),
        "bottle": ("pc", 1), "bottles": ("pc", 1), "botal": ("pc", 1), "sachet": ("pc", 1), "unit": ("pc", 1), "units": ("pc", 1),
        "dozen": ("pc", 12), "darjan": ("pc", 12), "nag": ("pc", 1), "नग": ("pc", 1)}
WORDNUM = {"ek": 1, "one": 1, "do": 2, "two": 2, "teen": 3, "three": 3, "char": 4, "four": 4, "paanch": 5, "panch": 5, "five": 5,
           "chhe": 6, "che": 6, "six": 6, "saat": 7, "seven": 7, "aath": 8, "eight": 8, "nau": 9, "nine": 9, "das": 10, "ten": 10,
           "aadha": Fraction(1, 2), "adha": Fraction(1, 2), "half": Fraction(1, 2), "dedh": Fraction(3, 2), "dhai": Fraction(5, 2),
           "डेढ़": Fraction(3, 2), "ढाई": Fraction(5, 2), "आधा": Fraction(1, 2)}


@dataclass
class StockCmd:
    kind: str                # sale | restock | count
    qty: int                 # base units
    dim: str                 # g | ml | pc
    item: str                # raw item text (caller resolves)
    raw_unit: str


def _tok_num(t: str) -> bool:
    t = t.lower()
    return bool(re.fullmatch(r"\d+(\.\d+)?", t)) or t in WORDNUM or parse_amount(t) is not None


def _number(phrase: str) -> Fraction | None:
    t = phrase.lower().strip(",")
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return Fraction(t)
    if t in WORDNUM:
        return Fraction(WORDNUM[t])
    if not all(_tok_num(x) for x in t.split()):
        return None
    p = parse_amount(t)                              # Hindi number words (paanch sau, pachas, …) via the existing parser
    return Fraction(p // 100) if p and p % 100 == 0 else None


def parse_qty(text: str) -> tuple[int, str, str, int, int] | None:
    """-> (base_qty, dim, unit_word, start_idx, end_idx) of the FIRST 'number unit' pair, on word boundaries."""
    toks = [(m.group(), m.start(), m.end()) for m in re.finditer(r"[\w.\u0900-\u097F₹]+", text)]
    for i, (w, s, e) in enumerate(toks):
        glued = re.fullmatch(r"(\d+(?:\.\d+)?)([a-zA-Z\u0900-\u097F]+)", w)       # "2kg", "500gm"
        if glued and glued.group(2).lower() in UNIT:
            dim, mult = UNIT[glued.group(2).lower()]
            return int(Fraction(glued.group(1)) * mult), dim, glued.group(2), s, e
        if w.lower() in UNIT:
            n, start = None, s
            for k in (3, 2, 1):                       # longest number phrase wins: "paanch sau" beats "sau"
                if i - k < 0:
                    continue
                cand = " ".join(t[0] for t in toks[i - k:i])
                n = _number(cand)
                if n is not None:
                    start = toks[i - k][1]
                    break
            if n is None and w.lower() in ("paav", "pav", "dozen", "darjan"):
                n = Fraction(1)
            if n is not None:
                dim, mult = UNIT[w.lower()]
                q = n * mult
                if q.denominator != 1 or q <= 0:
                    return None
                return int(q), dim, w, start, e
    return None


def parse(text: str) -> StockCmd | None:
    kind = "sale" if SALE.search(text) else "restock" if RESTOCK.search(text) else "count" if COUNT.search(text) else None
    if kind is None:
        return None
    q = parse_qty(text)
    if q is None:
        return None
    qty, dim, unit, s, e = q
    item = (text[:s] + " " + text[e:])
    for pat in (SALE, RESTOCK, COUNT):
        item = pat.sub(" ", item)
    return StockCmd(kind, qty, dim, " ".join(item.split()), unit)


def parse_price(text: str) -> tuple[str, int, int | None] | None:
    """'maggi 14 rupaye packet' / 'atta 55 per kilo' -> (item, price_paise, price_per_base_units or None)."""
    m = re.search(r"(\d+(?:\.\d{1,2})?)\s*(?:rupaye|rupay|rs\.?|₹|rupees)?", text, re.I)
    if not m:
        return None
    item_part = text[:m.start()].strip()
    after = text[m.end():].lower()
    paise = int((Fraction(m.group(1)) * 100).numerator // (Fraction(m.group(1)) * 100).denominator)
    per = None
    for w in re.findall(r"[\wऀ-ॿ]+", after):
        if w in UNIT:
            per = UNIT[w][1]
            break
    item = " ".join(w for w in item_part.split() if w.lower() not in ("price", "rate", "set", "ka", "ki", "bhav"))
    return (item, paise, per) if item else None
