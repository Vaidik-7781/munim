"""Hindi / Hinglish spoken-number parsing and formatting.

Pure logic, no I/O. All money is returned as integer PAISE — never floats.

Handles: roman + Devanagari number words 0-99, sau / hazaar / lakh / crore,
the fractional idioms (dedh, dhai, sava, paune, saadhe), Arabic + Devanagari digits,
Indian digit grouping (1,50,000), "2k", rupee markers and paise.

Ambiguity is surfaced, never guessed: a malformed run comes back with valid=False.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction

from rapidfuzz import fuzz, process
from unidecode import unidecode

# ---------------------------------------------------------------------------------------
# Vocabulary. Format: value | roman variants (first = canonical) | devanagari variants
# ---------------------------------------------------------------------------------------
_RAW = """
0|shunya,zero|शून्य
1|ek,ik|एक
2|do|दो
3|teen,tin|तीन
4|chaar,char,char|चार
5|paanch,panch,paach|पाँच,पांच,पाच
6|chhe,chhah,chheh,che,chhe|छह,छः,छे,छ
7|saat,sat|सात
8|aath,ath|आठ
9|nau,no,nav|नौ,नो
10|das,dus|दस
11|gyarah,gyaarah,igyarah,gyara|ग्यारह,ग्यारा
12|barah,baarah,bara|बारह,बारा
13|terah,tera|तेरह,तेरा
14|chaudah,chaudha,chodah|चौदह,चौदा
15|pandrah,pandra,pandrha|पंद्रह,पन्द्रह,पंद्रा
16|solah,sola|सोलह,सोला
17|satrah,sattrah,satra|सत्रह,सत्रा
18|atharah,athaarah,athara|अठारह,अठारा
19|unnees,unnis,unis,unnis|उन्नीस,उन्निस
20|bees,bis|बीस
21|ikkees,ikkis,ekkis|इक्कीस
22|baaees,baais,bayees|बाईस
23|teyees,teis,tais|तेईस
24|chaubees,chaubis,chaubbis|चौबीस
25|pachchees,pacchis,pachis,pachchis|पच्चीस,पचीस
26|chhabbees,chhabbis,chabbis|छब्बीस
27|sattaees,sattais,sataais|सत्ताईस,सताईस
28|atthaaees,atthais,athais|अट्ठाईस,अठाईस
29|untees,untis,unatis|उनतीस,उन्तीस
30|tees,tis|तीस
31|iktees,ikattis,ikatis|इकतीस
32|battees,battis,batis|बत्तीस
33|taintees,taintis,tentis|तैंतीस,तेंतीस
34|chauntees,chauntis,chautis|चौंतीस,चौतीस
35|paintees,paintis,pentis|पैंतीस,पैतीस
36|chhattees,chhattis,chattis|छत्तीस
37|saintees,saintis,sentis|सैंतीस,सैतीस
38|adtees,adtis,artis,adhtis|अड़तीस,अडतीस
39|untaalees,untalis,untaalis|उनतालीस,उन्तालीस
40|chaalees,chalis,chaalis,chaleesh|चालीस
41|iktaalees,iktalis,ikatalis|इकतालीस
42|bayaalees,bayalis,bayaalis|बयालीस
43|taintaalees,taintalis,tentalis|तैंतालीस,तेंतालीस
44|chauvaalees,chauvalis,chavalis|चौवालीस,चौंवालीस
45|paintaalees,paintalis,pentalis|पैंतालीस,पैतालीस
46|chhiyaalees,chhiyalis,chiyalis|छियालीस
47|saintaalees,saintalis,sentalis|सैंतालीस,सैतालीस
48|adtaalees,adtalis,artalis|अड़तालीस,अडतालीस
49|unchaas,unchas,unanchas|उनचास,उनपचास
50|pachaas,pachas,panchas,pachash|पचास
51|ikyaavan,ikyawan,ikyavan,ekawan|इक्यावन,इकावन
52|baavan,bavan,baaven|बावन
53|tirpan,tirpann,tirpen|तिरपन,तिरेपन
54|chauvan,chauwan,chavvan|चौवन
55|pachpan,pachpann,pachpen|पचपन
56|chhappan,chappan,chhappann|छप्पन
57|sattaavan,sattavan,satavan|सत्तावन
58|atthaavan,atthavan,athavan|अट्ठावन,अठावन
59|unsath,unasath,unsaath|उनसठ
60|saath,sath|साठ
61|iksath,iksaath,ekasath|इकसठ
62|baasath,basath,baasaath|बासठ
63|tirsath,tirsaath,tirasath|तिरसठ,तिरेसठ
64|chaunsath,chaunsaath,chausath|चौंसठ,चौसठ
65|painsath,painsaath,pausath|पैंसठ,पैसठ
66|chhiyaasath,chhiyasath,chiyasath|छियासठ
67|sadsath,sarsath,adsath67|सड़सठ,सरसठ
68|adsath,arsath,adhsath|अड़सठ,अडसठ
69|unhattar,unahattar,unhatar|उनहत्तर,उन्हत्तर
70|sattar,satar,sattarh|सत्तर
71|ikhattar,ikahattar,ikhatar|इकहत्तर
72|bahattar,bahatar,bahatthar|बहत्तर
73|tihattar,tihatar,tehattar|तिहत्तर
74|chauhattar,chauhatar,chohattar|चौहत्तर
75|pachhattar,pachattar,pachahattar,pachhatar|पचहत्तर
76|chhihattar,chihattar,chhihatar|छिहत्तर
77|satahattar,sathattar,satattar|सतहत्तर
78|athahattar,athhattar,athattar|अठहत्तर
79|unaasi,unyasi,unasi|उनासी,उन्यासी
80|assi,asi|अस्सी
81|ikyaasi,ikyasi,ekyasi|इक्यासी
82|bayaasi,bayasi,beyasi|बयासी
83|tiraasi,tirasi,tirashi|तिरासी
84|chauraasi,chaurasi,chorasi|चौरासी
85|pachaasi,pachasi,pachaaasi|पचासी
86|chhiyaasi,chiyasi,chhiyasi|छियासी
87|sattaasi,sattasi,satasi|सत्तासी
88|atthaasi,atthasi,athasi|अट्ठासी,अठासी
89|navaasi,navasi,navashi|नवासी
90|nabbe,nabbay,nabbey|नब्बे
91|ikyaanave,ikyanave,ekanave|इक्यानवे,इक्यानबे
92|baanave,banave,baanawe|बानवे,बानबे
93|tiraanave,tiranave,tiranawe|तिरानवे,तिरानबे
94|chauraanave,chauranave,chauranawe|चौरानवे,चौरानबे
95|pachaanave,pachanave,pachanawe|पचानवे,पचानबे
96|chhiyaanave,chhiyanave,chiyanave|छियानवे,छियानबे
97|sattaanave,sattanave,satanave|सत्तानवे,सत्तानबे
98|atthaanave,atthanave,athanave|अट्ठानवे,अट्ठानबे,अठानवे
99|ninyaanave,ninyanave,ninnanave|निन्यानवे,निन्यानबे
"""

_SCALES = {
    "sau": (100, ["sau", "saw", "sou"], ["सौ"]),
    "hazaar": (1000, ["hazaar", "hazar", "hajar", "hajaar", "hazaaar"], ["हज़ार", "हजार", "हज़ार"]),
    "lakh": (100_000, ["lakh", "laakh", "lac", "lack"], ["लाख"]),
    "crore": (10_000_000, ["crore", "karod", "karor", "cr", "kroor"], ["करोड़", "करोड", "करोङ"]),
}

# modifiers: kind -> (roman variants, devanagari variants)
_MODS = {
    "dedh": (["dedh", "derh", "dedha", "der"], ["डेढ़", "डेढ"]),
    "dhai": (["dhai", "dhaai", "dhayi", "adhai", "dhaee"], ["ढाई", "अढ़ाई", "अढाई"]),
    "sava": (["sava", "sawa", "sawaa", "savaa"], ["सवा"]),
    "paune": (["paune", "paun", "pauney"], ["पौने", "पौन"]),
    "saadhe": (["saadhe", "sadhe", "sade", "saarhe", "saade"], ["साढ़े", "साढे", "साडे"]),
}

_CURRENCY = {
    "rupaye", "rupay", "rupaya", "rupiye", "rupees", "rupee", "rs", "rs.", "inr", "rupe",
    "₹", "रुपये", "रुपए", "रुपया", "रुपै",
}
_PAISE = {"paise", "paisa", "paisey", "पैसे", "पैसा"}
# quantity / time units: a number right before one of these is NOT a money amount
_UNITS = {
    "kilo", "kg", "kgs", "gram", "grams", "gm", "packet", "packets", "pkt", "litre", "liter",
    "ltr", "dozen", "darjan", "piece", "pieces", "pcs", "baar", "log", "din", "dino", "tarikh",
    "minute", "minutes", "ghanta", "ghante", "baje", "bje", "bajey", "mahine", "mahina", "saal",
    "hafte", "hafta", "bottle", "botal", "peti", "bori", "kilogram", "लीटर", "किलो", "पैकेट",
    "बजे", "दिन", "बार",
}

# 'do' is both "two" and the verb "give/put": after these verbs it is NOT a number
_DO_VERBS = {"likh", "likhna", "de", "kar", "karo", "dijiye", "rakh", "jod", "kat", "ghata", "likho",
             "लिख", "दे", "कर", "रख", "जोड़"}

_DEVA_STRIP = {0x093C, 0x0901, 0x0902, 0x0903}  # nukta, chandrabindu, anusvara, visarga
_DEVA_DIGITS = {ord("०") + i: str(i) for i in range(10)}


def _is_deva(s: str) -> bool:
    return any("ऀ" <= c <= "ॿ" for c in s)


def _deva_key(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if ord(c) not in _DEVA_STRIP)
    return unicodedata.normalize("NFC", s)


def _roman_key(s: str) -> str:
    s = unidecode(s).lower()
    s = re.sub(r"[^a-z]", "", s)
    s = s.replace("chh", "ch").replace("w", "v").replace("z", "j")
    s = s.replace("aa", "a").replace("ee", "i").replace("ii", "i").replace("oo", "u").replace("uu", "u")
    s = re.sub(r"(.)\1+", r"\1", s)
    return s


# --- tables ------------------------------------------------------------------------------
WORD_VALUE: dict[str, int] = {}     # roman key -> 0..99
DEVA_VALUE: dict[str, int] = {}     # devanagari key -> 0..99
ROMAN_CANON: dict[int, str] = {}    # 0..99 -> canonical roman spelling (for TTS/echo)
DEVA_CANON: dict[int, str] = {}     # 0..99 -> canonical devanagari spelling


def _build() -> None:
    for line in _RAW.strip().splitlines():
        val_s, roman_s, deva_s = line.split("|")
        val = int(val_s)
        romans = [r for r in roman_s.split(",") if not r.endswith("67")]
        devas = deva_s.split(",")
        ROMAN_CANON[val] = romans[0]
        DEVA_CANON[val] = devas[0]
        for r in romans:
            k = _roman_key(r)
            if k in WORD_VALUE and WORD_VALUE[k] != val:
                raise ValueError(f"number-word key collision: {r!r} ({k}) {WORD_VALUE[k]} vs {val}")
            WORD_VALUE[k] = val
        for d in devas:
            DEVA_VALUE[_deva_key(d)] = val


_SCALE_ROMAN: dict[str, int] = {}
_SCALE_DEVA: dict[str, int] = {}
_MOD_ROMAN: dict[str, str] = {}
_MOD_DEVA: dict[str, str] = {}
_CURRENCY_KEYS: set[str] = set()


def _build_aux() -> None:
    for _name, (val, rs, ds) in _SCALES.items():
        for r in rs:
            _SCALE_ROMAN[_roman_key(r)] = val
        for d in ds:
            _SCALE_DEVA[_deva_key(d)] = val
    for kind, (rs, ds) in _MODS.items():
        for r in rs:
            _MOD_ROMAN[_roman_key(r)] = kind
        for d in ds:
            _MOD_DEVA[_deva_key(d)] = kind


_build()
_build_aux()
# "der" is a legit alias of dedh but also a plain English word: keep exact-only (len<5 skips fuzzy)

_FUZZY_KEYS = [k for k in WORD_VALUE if len(k) >= 5]
_FUZZY_CUTOFF = 90
_FUZZY_MARGIN = 6


def _fuzzy_word(key: str) -> int | None:
    """Fuzzy match for long Hinglish spellings ('panchhattar'). Exact-only for short tokens."""
    if len(key) < 5:
        return None
    hits = process.extract(key, _FUZZY_KEYS, scorer=fuzz.ratio, limit=3, score_cutoff=_FUZZY_CUTOFF)
    if not hits:
        return None
    best_key, best_score, _ = hits[0]
    best_val = WORD_VALUE[best_key]
    for k2, s2, _ in hits[1:]:
        if WORD_VALUE[k2] != best_val and best_score - s2 < _FUZZY_MARGIN:
            return None
    return best_val


# ---------------------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------------------
@dataclass
class Tok:
    raw: str
    kind: str                 # num | digit | scale | mod | curr | paise | unit | other
    value: Fraction | None = None
    mod: str | None = None


_PUNCT_EDGE = ".,!?;:\"'()[]{}।"


_NUM1_9 = (r"\u090f\u0915|\u0926\u094b|\u0924\u0940\u0928|\u091a\u093e\u0930|\u092a\u093e\u0902\u091a|\u092a\u093e\u0901\u091a|\u091b\u0939|\u091b\u0903|\u091b\u0947|\u0938\u093e\u0924|\u0906\u0920|\u0928\u094c|"
           r"ek|do|teen|char|chaar|paanch|panch|chhe|chheh|saat|aath|nau")
_HUNDRED_AS_RS = re.compile(
    r"(?:(?P<m>\u0921\u0947\u0922\u093c|\u0921\u0947\u0922|\u0922\u093e\u0908|\u0938\u0935\u093e|\u092a\u094c\u0928\u0947|\u0938\u093e\u0922\u093c\u0947\s+\S+|dedh|dhai|dhaai|sava|paune|saade\s+\S+)|\b(?P<d>[1-9]|" + _NUM1_9 + r"))"
    r"\s*\u20b9\s*100(?!\d)", re.I)
_VERB_THEN_2 = re.compile(
    r"(?P<v>\u0932\u093f\u0916|\u0915\u0930|\u0926\u0947|\u091c\u094b\u0921\u093c|\u091c\u094b\u0921|likh|kar|de|jod)\s+2\b(?!\s*(?:\u20b9|\u0930\u0941\u092a\u092f|\u0930\u0941\u092a\u090f|rs\b|rupa|hazaar|\u0939\u091c\u093c\u093e\u0930|\u0939\u091c\u093e\u0930|\u0938\u094c|sau|lakh|\u0932\u093e\u0916|k\b))", re.I)


_FRAC_ROUND = re.compile(
    r"(?P<w>\u092a\u094c\u0928\u0947|\u0938\u0935\u093e|\u0938\u093e(?:\u095d|\u0922\u093c)\u0947|paune|sava|saade|saadhe)"
    r"\s*\u20b9?\s*(?P<n>[1-9][\d,]*)(?![\d,])", re.I)
_SAADE_CLOCK = re.compile(r"(?<![\d:])0?(?P<x>[2-9]):30\s*\u20b9\s*100(?!\d)")


def _frac_round(m: "re.Match[str]") -> str:
    """v2.5 folds the unit into the number: "paune do sau" -> "paune Rs200" (=175), "paune do lakh" -> "paune Rs2,00,000" (=1,75,000)."""
    digits = m.group("n").replace(",", "")
    if not re.fullmatch(r"[1-9]0{2,}", digits) or int(digits[0]) < 2:
        return m.group(0)
    x, unit = int(digits[0]), 10 ** (len(digits) - 1)
    w = m.group("w").lower()
    if w in ("\u092a\u094c\u0928\u0947", "paune"):
        val = x * unit - unit // 4
    elif w in ("\u0938\u0935\u093e", "sava"):
        val = x * unit + unit // 4
    else:
        val = x * unit + unit // 2
    return "\u20b9" + str(val)


def _stt_quirks(text: str) -> str:
    """Gnani v2.5 writes spoken 'sau' (100) as a rupee amount ('dedh sau' -> 'dedh Rs100') and 'do' (please) as '2' ('likh do' -> 'likh 2').
    Undo both, narrowly, so the second listen agrees with the first when the speech was the same."""
    text = text.translate(_DEVA_DIGITS)
    text = _SAADE_CLOCK.sub(lambda m: "\u20b9" + str(int(m.group('x')) * 100 + 50), text)
    text = _FRAC_ROUND.sub(_frac_round, text)
    text = _HUNDRED_AS_RS.sub(lambda m: f"{m.group('m') or m.group('d')} \u0938\u094c", text)
    return _VERB_THEN_2.sub(lambda m: f"{m.group('v')} \u0926\u094b", text)


def _pre(text: str) -> str:
    text = _stt_quirks(text)
    text = text.translate(_DEVA_DIGITS)
    text = text.replace("₹", " ₹ ")
    text = re.sub(r"\brs\.?(?=\s|\d|$)", " rs ", text, flags=re.I)
    text = re.sub(r"(?<=\d),(?=\d)", "", text)            # 1,50,000 -> 150000
    text = re.sub(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", " ", text)
    text = re.sub(r"(?<=\d)(?=[\u0900-\u097f])|(?<=[\u0900-\u097f])(?=\d)", " ", text)  # 350रुपये -> 350 रुपये
    text = re.sub(r"(\d)\s*[kK]\b", r"\1 k", text)
    text = text.replace("+", " + ")
    return text


def tokenize(text: str) -> list[Tok]:
    toks: list[Tok] = []
    for raw in _pre(text).split():
        w = raw.strip(_PUNCT_EDGE)
        if not w:
            continue
        lw = w.lower()
        if lw in ("do", "दो") and toks and toks[-1].raw.lower() in _DO_VERBS:
            toks.append(Tok(w, "other"))
        elif re.fullmatch(r"\d+(\.\d+)?", lw):
            toks.append(Tok(w, "digit", Fraction(lw)))
        elif lw == "k":
            toks.append(Tok(w, "scale", Fraction(1000)))
        elif lw in _CURRENCY or w in _CURRENCY:
            toks.append(Tok(w, "curr"))
        elif lw in _PAISE or w in _PAISE:
            toks.append(Tok(w, "paise"))
        elif lw in _UNITS or w in _UNITS:
            toks.append(Tok(w, "unit"))
        elif _is_deva(w):
            dk = _deva_key(w)
            if dk in _SCALE_DEVA:
                toks.append(Tok(w, "scale", Fraction(_SCALE_DEVA[dk])))
            elif dk in _MOD_DEVA:
                toks.append(Tok(w, "mod", mod=_MOD_DEVA[dk]))
            elif dk in DEVA_VALUE:
                toks.append(Tok(w, "num", Fraction(DEVA_VALUE[dk])))
            else:
                toks.append(Tok(w, "other"))
        else:
            rk = _roman_key(w)
            if not rk:
                toks.append(Tok(w, "other"))
            elif rk in _SCALE_ROMAN:
                toks.append(Tok(w, "scale", Fraction(_SCALE_ROMAN[rk])))
            elif rk in _MOD_ROMAN:
                toks.append(Tok(w, "mod", mod=_MOD_ROMAN[rk]))
            elif rk in WORD_VALUE:
                toks.append(Tok(w, "num", Fraction(WORD_VALUE[rk])))
            else:
                fv = _fuzzy_word(rk)
                if fv is not None:
                    toks.append(Tok(w, "num", Fraction(fv)))
                else:
                    toks.append(Tok(w, "other"))
    return toks


# ---------------------------------------------------------------------------------------
# Run evaluation
# ---------------------------------------------------------------------------------------
@dataclass
class AmountSpan:
    paise: int | None
    text: str
    start: int
    end: int
    valid: bool
    has_currency: bool = False
    is_quantity: bool = False
    reason: str = ""
    strong: bool = True  # False = bare small number word with no scale/digit/currency ("do", "ek")


_MOD_DELTA = {"sava": Fraction(1, 4), "paune": Fraction(-1, 4), "saadhe": Fraction(1, 2)}
_MAX_PAISE = 10 * 10_000_000 * 100  # 10 crore rupees


def _eval_run(run: list[Tok]) -> tuple[Fraction | None, str]:
    """Evaluate a run of num/digit/scale/mod tokens to RUPEES (Fraction). Returns (value, reason)."""
    total = Fraction(0)
    cur = Fraction(0)
    pending_mod: str | None = None
    pending_base: Fraction | None = None
    last_was_small = False  # last token was a plain number contributing to `cur`
    for t in run:
        if t.kind in ("num", "digit"):
            assert t.value is not None
            if pending_mod in ("sava", "paune", "saadhe", "dedh", "dhai") and pending_base is None:
                # base number following a modifier ("saadhe teen sau", "sava do sau")
                if pending_mod in ("dedh", "dhai"):
                    return None, "number after dedh/dhai"
                pending_base = t.value
                continue
            if cur % 100 != 0 and last_was_small:
                return None, "two adjacent small numbers"
            cur += t.value
            last_was_small = True
        elif t.kind == "mod":
            if pending_mod is not None:
                return None, "two modifiers"
            pending_mod = t.mod
            pending_base = None
        elif t.kind == "scale":
            assert t.value is not None
            scale = t.value
            if pending_mod:
                if pending_mod == "dedh":
                    mult = Fraction(3, 2)
                elif pending_mod == "dhai":
                    mult = Fraction(5, 2)
                elif pending_mod == "saadhe":
                    if pending_base is None:
                        return None, "saadhe without base"
                    mult = pending_base + _MOD_DELTA["saadhe"]
                else:  # sava / paune
                    base = pending_base if pending_base is not None else Fraction(1)
                    mult = base + _MOD_DELTA[pending_mod]
                pending_mod, pending_base = None, None
                part = mult * scale
                if scale == 100:
                    cur += part
                else:
                    total += part
                    cur = Fraction(0)
            else:
                mult = cur if cur != 0 else Fraction(1)
                if scale == 100:
                    # 'sau' applies to the small number just before it, not to earlier hundreds
                    cur = mult * 100
                else:
                    total += mult * scale
                    cur = Fraction(0)
            last_was_small = False
    if pending_mod is not None:
        return None, "modifier without scale"
    return total + cur, ""


def parse_amounts(text: str) -> list[AmountSpan]:
    """Find every money-like run in `text`. Caller decides what to do with several."""
    toks = tokenize(text)
    spans: list[AmountSpan] = []
    i, n = 0, len(toks)
    while i < n:
        if toks[i].kind not in ("num", "digit", "scale", "mod"):
            i += 1
            continue
        j = i
        while j < n and toks[j].kind in ("num", "digit", "scale", "mod"):
            j += 1
        run = toks[i:j]
        has_cur = False
        is_paise = False
        # a leading currency symbol/word ("₹ 150", "rs 150")
        if i > 0 and toks[i - 1].kind == "curr":
            has_cur = True
        end = j
        nxt = toks[j] if j < n else None
        is_qty = False
        if nxt is not None:
            if nxt.kind == "curr":
                has_cur = True
                end = j + 1
            elif nxt.kind == "paise":
                is_paise = True
                has_cur = True
                end = j + 1
            elif nxt.kind == "unit":
                is_qty = True
        val, reason = _eval_run(run)
        text_span = " ".join(t.raw for t in toks[i:end])
        if val is None:
            spans.append(AmountSpan(None, text_span, i, end, False, has_cur, is_qty, reason))
        else:
            paise_f = val if is_paise else val * 100
            if paise_f.denominator != 1 or paise_f < 0 or paise_f > _MAX_PAISE:
                spans.append(AmountSpan(None, text_span, i, end, False, has_cur, is_qty,
                                        "non-integer or out-of-range"))
            else:
                # a lone bare word like 'ek' / 'do' with no scale/currency is weak evidence
                strong = has_cur or any(t.kind in ("digit", "scale", "mod") for t in run) or len(run) > 1
                spans.append(AmountSpan(int(paise_f), text_span, i, end, True, has_cur, is_qty,
                                        strong=strong))
        i = end
    return spans


def parse_amount(text: str) -> int | None:
    """Convenience: the single valid, non-quantity amount in paise, else None."""
    good = [s for s in parse_amounts(text) if s.valid and not s.is_quantity]
    if any(s.strong for s in good):
        good = [s for s in good if s.strong]
    return good[0].paise if len(good) == 1 else None


# ---------------------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------------------
def format_inr(paise: int) -> str:
    """₹2,50,000 style (Indian grouping); shows paise only when non-zero."""
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    rupees, ps = divmod(paise, 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    out = f"{sign}₹{s}"
    if ps:
        out += f".{ps:02d}"
    return out


def _below_100(n: int, script: str) -> str:
    return DEVA_CANON[n] if script == "deva" else ROMAN_CANON[n]


def rupees_to_words(rupees: int, script: str = "deva") -> str:
    """Spoken form for TTS. Gnani TTS mis-reads digit strings, so we ALWAYS speak words."""
    if rupees < 0:
        raise ValueError("negative amount")
    if rupees == 0:
        return _below_100(0, script)
    sau = "सौ" if script == "deva" else "sau"
    names = (
        [(10_000_000, "करोड़"), (100_000, "लाख"), (1000, "हज़ार")]
        if script == "deva"
        else [(10_000_000, "crore"), (100_000, "lakh"), (1000, "hazaar")]
    )
    parts: list[str] = []
    rem = rupees
    for size, name in names:
        q, rem = divmod(rem, size)
        if q:
            parts.append(rupees_to_words(q, script) + " " + name)
    h, r = divmod(rem, 100)
    if h:
        parts.append(_below_100(h, script) + " " + sau)
    if r:
        parts.append(_below_100(r, script))
    return " ".join(parts)


def paise_to_words(paise: int, script: str = "deva") -> str:
    rupees, ps = divmod(paise, 100)
    rupee_word = "रुपये" if script == "deva" else "rupaye"
    paise_word = "पैसे" if script == "deva" else "paise"
    out = f"{rupees_to_words(rupees, script)} {rupee_word}"
    if ps:
        out += f" {rupees_to_words(ps, script)} {paise_word}"
    return out


# ---- English (Indian grouping) number words, for en-IN voice reminders ---------------------------------
_EN_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
            "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _en_below_100(n: int) -> str:
    if n < 20:
        return _EN_ONES[n]
    t, o = divmod(n, 10)
    return _EN_TENS[t] + (f" {_EN_ONES[o]}" if o else "")


def rupees_to_words_en(n: int) -> str:
    if n < 0:
        raise ValueError("negative amount")
    if n == 0:
        return "zero"
    parts: list[str] = []
    for size, name in ((10_000_000, "crore"), (100_000, "lakh"), (1000, "thousand")):
        q, n = divmod(n, size)
        if q:
            parts.append(f"{rupees_to_words_en(q)} {name}")
    h, r = divmod(n, 100)
    if h:
        parts.append(f"{_EN_ONES[h]} hundred")
    if r:
        parts.append(_en_below_100(r))
    return " ".join(parts)


def paise_to_words_en(paise: int) -> str:
    rupees, ps = divmod(paise, 100)
    out = f"{rupees_to_words_en(rupees)} rupees"
    if ps:
        out += f" {_en_below_100(ps)} paise"
    return out
