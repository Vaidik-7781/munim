"""Customer-name resolution from spoken / transcribed text.

Rules (PRD §Scribe): strip honorifics, transliterate, fuzzy-match token sets.
  accept  : top score >= 88 AND >= 7 points over the runner-up
  ask     : 70..87, or a near-tie  -> "kaun sa Sharma?"
  none    : nothing plausible
A tie between two real customers is NEVER broken silently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from unidecode import unidecode

HONORIFICS = {
    "ji", "bhai", "bhaiya", "didi", "aunty", "auntie", "uncle", "sahab", "saab", "sahib", "madam",
    "mam", "sir", "shri", "shree", "smt", "mr", "mrs", "ms", "bhabhi", "chacha", "chachi", "mama",
    "जी", "भाई", "भैया", "दीदी", "आंटी", "अंकल", "साहब", "मैडम", "श्री",
}

# common Hinglish function words that must never be read as (part of) a name
STOPWORDS = {
    "ko", "ke", "ki", "ka", "naam", "nam", "par", "pe", "me", "mein", "udhaar", "udhar", "likh", "likho",
    "de", "diye", "diya", "liya", "liye", "hai", "hain", "ne", "se", "aur", "rupaye", "rupay", "rs",
    "kitna", "kitne", "baaki", "baki", "hisaab", "hisab", "abhi", "aaj", "kal", "jama", "wapas",
    "paise", "paisa", "yaad", "dilao", "bhejo", "ruko", "mat", "undo", "galat", "sahi", "haan", "nahi",
    "ek", "do", "teen", "sau", "hazaar", "lakh", "dedh", "dhai", "sava", "paune", "saadhe",
    "kar", "karo", "dena", "dijiye", "gaye", "gaya", "gayi", "le", "reminder", "message", "msg",
    "bhai", "ye", "yeh", "wo", "woh", "ka", "to", "bhi", "tha", "thi", "the", "chukaye", "chuka",
}


def name_key(s: str) -> str:
    """Phonetic-ish key: robust to Sharma/Sarma, Guptaa/Gupta, Devanagari/roman."""
    s = unidecode(s).lower()
    s = re.sub(r"[^a-z ]", " ", s)
    s = s.replace("nh", "ngh")  # anusvara+ha: Devanagari Singh -> "sinh" -> same key as Singh
    s = s.replace("sh", "s").replace("ph", "f").replace("w", "v").replace("z", "j")
    s = s.replace("aa", "a").replace("ee", "i").replace("oo", "u").replace("ii", "i").replace("uu", "u")
    s = s.replace("h", "")
    s = re.sub(r"(.)\1+", r"\1", s)
    return " ".join(s.split())


def _strip_honorifics(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t.lower() not in HONORIFICS]


@dataclass
class Customer:
    id: int
    name: str
    aliases: list[str] = field(default_factory=list)

    @property
    def keys(self) -> list[str]:
        out = [name_key(" ".join(_strip_honorifics(self.name.split())))]
        out += [name_key(a) for a in self.aliases]
        return [k for k in out if k]


@dataclass
class Resolution:
    status: str                       # match | ask | none
    customer: Customer | None = None
    candidates: list[tuple[Customer, float]] = field(default_factory=list)
    span: tuple[int, int] | None = None   # token window [start, end) used in the transcript
    score: float = 0.0


def _windows(tokens: list[str], max_len: int = 3):
    for i in range(len(tokens)):
        for n in range(1, max_len + 1):
            if i + n <= len(tokens):
                yield i, i + n, tokens[i : i + n]


def resolve(text: str, customers: list[Customer]) -> Resolution:
    raw_tokens = [t.strip(".,!?;:\"'()।") for t in text.split()]
    raw_tokens = [t for t in raw_tokens if t]
    scored: dict[int, tuple[float, tuple[int, int]]] = {}
    for i, j, win in _windows(raw_tokens):
        if any(w.lower() in HONORIFICS or w.lower() in STOPWORDS or any(c.isdigit() for c in w) for w in win):
            continue
        wkey = name_key(" ".join(win))
        if len(wkey.replace(" ", "")) < 3:
            continue
        for c in customers:
            for ck in c.keys:
                if wkey == ck:
                    sc = 100.0
                elif wkey in ck.split():
                    sc = 92.0   # one token of a longer name: full-name mentions must outrank it
                else:
                    sc = fuzz.ratio(wkey, ck) if " " not in wkey else fuzz.token_sort_ratio(wkey, ck)
                    # a single spoken token vs one token of a multi-token customer name
                    for part in ck.split():
                        if len(part) >= 3:
                            sc = max(sc, 0.92 * fuzz.ratio(wkey, part) if " " not in wkey else 0)
                    if len(wkey) < 4 and sc < 100:
                        sc = 0.0
                prev = scored.get(c.id)
                # prefer the longer window on ties so "Rahul Sharma" beats "Sharma"
                if prev is None or sc > prev[0] or (sc == prev[0] and (j - i) > (prev[1][1] - prev[1][0])):
                    scored[c.id] = (sc, (i, j))
    ranked = sorted(
        ((next(c for c in customers if c.id == cid), s, span) for cid, (s, span) in scored.items()),
        key=lambda x: -x[1],
    )
    ranked = [r for r in ranked if r[1] >= 70]
    if not ranked:
        return Resolution("none")
    top_c, top_s, top_span = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0.0
    if top_s >= 88 and top_s - runner >= 7:
        return Resolution("match", top_c, [(top_c, top_s)], top_span, top_s)
    near = [(c, s) for c, s, _ in ranked if top_s - s < 7][:3] or [(top_c, top_s)]
    return Resolution("ask", None, near, top_span, top_s)
