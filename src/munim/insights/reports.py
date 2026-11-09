"""Spoken/typed business reports ("sabse zyada udhaar kiska?", "aaj ka hisaab", "kaun bahut din se baaki?").
Numbers come from engine.py; voice_text spells every amount as words for TTS."""
from __future__ import annotations

import re
from dataclasses import dataclass

from munim.ledger.chain import Ledger
from munim.scribe.indic_numbers import format_inr, paise_to_words

from . import engine as E

REPORT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("today", re.compile(r"\b(aaj\s+ka\s+(hisaab|hisab|summary)|aaj\s+kitna|din\s+ka\s+hisaab)\b|आज का हिसाब", re.I)),
    ("week", re.compile(r"\b(is\s+hafte|pichle\s+hafte|weekly|hafte\s+ka)\b|इस हफ्ते", re.I)),
    ("overdue", re.compile(r"\b(bahut\s+din\s+se|purana\s+(udhaar|baaki)|overdue|kisne\s+nahi\s+(diya|chukaya)|kaun\s+(\w+\s+){0,2}nahi\s+(de|chuk))", re.I)),
    ("risk", re.compile(r"\b(risk|khatra|kaun\s+bhagega|kis\s+par\s+bharosa|risky)\b", re.I)),
    ("top", re.compile(r"\b(sabse\s+(zyada|jyada|bada)|top\s*\d*\s*(udhaar|debtor)|kiska\s+(sabse\s+)?(zyada|jyada))\b|सबसे ज़्यादा|सबसे ज्यादा", re.I)),
    ("total", re.compile(r"\b(kul\s+(kitna|udhaar|baaki)|total\s+(udhaar|baaki)|sab\s+ka\s+(udhaar|baaki)|market\s+(mein|me)\s+kitna)\b|कुल", re.I)),
    ("plan", re.compile(r"\b(aaj\s+kis\s*se\s+(maangu|mangu|vasooli)|vasooli\s+(plan|list)|kise\s+yaad\s+dilana)\b", re.I)),
]
REPORT_ANY = re.compile("|".join(p.pattern for _, p in REPORT_PATTERNS), re.I)


def report_kind(text: str) -> str | None:
    for k, pat in REPORT_PATTERNS:
        if pat.search(text):
            return k
    return None


@dataclass
class Report:
    text: str
    voice: str


def _w(p: int) -> str:
    return paise_to_words(p)


def render(kind: str, ledger: Ledger, now: float) -> Report:
    if kind == "total":
        tot, n = E.total_outstanding(ledger), len(E.outstanding(ledger))
        return Report(f"Kul baaki: {format_inr(tot)} ({n} customer se).", f"Kul baaki {_w(tot)} hai, {n} customer se.")
    if kind == "top":
        rows = E.top_debtors(ledger, 5)
        if not rows:
            return Report("Abhi kisi ka baaki nahi hai. 🎉", "Abhi kisi ka baaki nahi hai.")
        lines = [f"{i + 1}. {c.name}: {format_inr(b)}" for i, (c, b) in enumerate(rows)]
        c0, b0 = rows[0]
        return Report("Sabse zyada baaki:\n" + "\n".join(lines), f"Sabse zyada baaki {c0.name} ka hai, {_w(b0)}.")
    if kind == "today":
        d = E.day_summary(ledger, now)
        return Report(f"Aaj: {d['entries']} entry. Udhaar diya {format_inr(d['given'])}, vasooli {format_inr(d['collected'])}.",
                      f"Aaj {d['entries']} entry hui. Udhaar {_w(d['given'])}, vasooli {_w(d['collected'])}.")
    if kind == "week":
        w = E.weekly_flow(ledger, now, 2)
        cur = w[-1]
        net = cur["net"]
        trend = "udhaar badha" if net > 0 else "udhaar ghata"
        return Report(f"Is hafte: diya {format_inr(cur['given'])}, vasooli {format_inr(cur['collected'])} — {trend} ({format_inr(abs(net))}).",
                      f"Is hafte udhaar {_w(cur['given'])}, vasooli {_w(cur['collected'])}. {trend.capitalize()}.")
    if kind == "overdue":
        rows = E.overdue(ledger, now, 30)[:5]
        if not rows:
            return Report("30 din se purana koi baaki nahi.", "Tees din se purana koi baaki nahi hai.")
        lines = [f"• {c.name}: {format_inr(b)}, {d} din purana" for c, b, d in rows]
        return Report("30+ din purane baaki:\n" + "\n".join(lines), f"{rows[0][0].name} ka baaki {rows[0][2]} din purana hai.")
    if kind == "risk":
        scored = sorted(((E.risk(ledger, c.id, now), c) for c, _ in E.outstanding(ledger)), key=lambda x: -x[0].score)[:5]
        scored = [(r, c) for r, c in scored if r.tier != "low"]
        if not scored:
            return Report("Abhi koi customer risky nahi dikh raha.", "Abhi koi customer risky nahi dikh raha.")
        lines = [f"• {c.name} [{r.tier.upper()}] — {'; '.join(t for _, t in r.reasons[:2])}" for r, c in scored]
        return Report("Dhyaan dene layak:\n" + "\n".join(lines), f"{scored[0][1].name} par sabse zyada dhyaan dein.")
    return Report("Yeh report abhi samajh nahi aayi.", "Yeh report abhi samajh nahi aayi.")


def morning_briefing(ledger: Ledger, now: float, shop: str = "Dukaan") -> Report:
    tot = E.total_outstanding(ledger)
    top = E.top_debtors(ledger, 1)
    od = E.overdue(ledger, now, 30)
    an = E.anomalies(ledger, now, 2)
    ds = E.day_summary(ledger, now - 86400)
    txt = [f"Suprabhaat! {shop} ka subah ka hisaab:",
           f"• Kul baaki: {format_inr(tot)}",
           f"• Kal: udhaar {format_inr(ds['given'])}, vasooli {format_inr(ds['collected'])}"]
    voice = [f"Suprabhaat. Kul baaki {_w(tot)} hai.", f"Kal udhaar {_w(ds['given'])} aur vasooli {_w(ds['collected'])} rahi."]
    if top:
        txt.append(f"• Sabse zyada: {top[0][0].name} ({format_inr(top[0][1])})")
        voice.append(f"Sabse zyada baaki {top[0][0].name} ka hai.")
    if od:
        txt.append(f"• 30+ din purane: {len(od)} customer")
        voice.append(f"{len(od)} customer ka baaki tees din se purana hai.")
    if an:
        txt.append(f"• ⚠️ {len(an)} entry jaanchne layak (/anomaly)")
        voice.append(f"{len(an)} entry jaanchne layak hain.")
    txt.append("Vasooli ki list ke liye /plan dabayein.")
    return Report("\n".join(txt), " ".join(voice))
