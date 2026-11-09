"""Governor: the rules a reminder must pass BEFORE the shopkeeper is even shown a draft.

Every rule is deterministic and every refusal carries a reason, so the bot can explain
"kyun nahi bheja" in plain words. The AI never decides to message a human on its own:
the output is a DRAFT that needs a one-tap approval from the shopkeeper.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import yaml

from munim.config import Settings
from munim.ledger.chain import Ledger
from munim.scribe.indic_numbers import format_inr, paise_to_words, paise_to_words_en

from .tone_lint import lint

from munim.timeutil import IST  # noqa: E402  (re-exported)
_SCRIPTS = os.path.join(os.path.dirname(__file__), "scripts", "gentle.yaml")

REASON_TEXT_HI = {
    "no_consent": "is customer ne reminder ke liye sehmati nahi di",
    "opted_out": "is customer ne reminder band karne ko kaha hai",
    "paused": "is customer ke reminder abhi ruke hue hain",
    "disputed": "is khate par vivaad (dispute) khula hai",
    "no_balance": "koi baaki raqam nahi hai",
    "outside_hours": "reminder sirf subah 8 se shaam 7 baje ke beech bheje jate hain",
    "too_soon": "pichle reminder ko 72 ghante nahi hue",
    "monthly_cap": "pichle 30 din mein 3 reminder ja chuke hain",
    "erased": "customer ka data hata diya gaya hai",
    "promise_pending": "customer ne vaada kiya hai aur vaada-tareekh abhi nahi aayi",
}


@dataclass
class GovernorDecision:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    next_allowed_at: float | None = None

    def explain_hi(self) -> str:
        return "; ".join(REASON_TEXT_HI.get(r, r) for r in self.reasons)


def check(ledger: Ledger, cfg: Settings, customer_id: int, now: float | None = None) -> GovernorDecision:
    now = now if now is not None else ledger._clock()
    row = ledger.customer_row(customer_id)
    reasons: list[str] = []
    next_at: float | None = None

    if row["erased"]:
        reasons.append("erased")
    if row["consent"] != "granted":
        reasons.append("no_consent")
    if row["opted_out"]:
        reasons.append("opted_out")
    if row["paused"]:
        reasons.append("paused")
    if ledger.is_disputed(customer_id):
        reasons.append("disputed")
    if ledger.balance(customer_id) <= 0:
        reasons.append("no_balance")
    from munim.insights.engine import active_promise   # local import: insights depends on ledger only

    pr = active_promise(ledger, customer_id, now)
    if pr is not None:
        from munim.insights.engine import _due_end_ts

        if now <= _due_end_ts(pr.meta["due"]):
            reasons.append("promise_pending")
            next_at = max(next_at or 0, _due_end_ts(pr.meta["due"]) + 1)

    local = datetime.fromtimestamp(now, IST)
    if not (cfg.reminder_start_hour <= local.hour < cfg.reminder_end_hour):
        reasons.append("outside_hours")
        nxt = local.replace(hour=cfg.reminder_start_hour, minute=0, second=0, microsecond=0)
        if local.hour >= cfg.reminder_end_hour:
            nxt += timedelta(days=1)
        next_at = nxt.timestamp()

    times = ledger.reminder_times(customer_id)
    if times:
        gap = cfg.reminder_min_gap_hours * 3600
        if now - times[-1] < gap:
            reasons.append("too_soon")
            next_at = max(next_at or 0, times[-1] + gap)
    window = [t for t in times if now - t < 30 * 86400]
    if len(window) >= cfg.reminder_max_per_30d:
        reasons.append("monthly_cap")
        next_at = max(next_at or 0, window[0] + 30 * 86400)

    return GovernorDecision(not reasons, reasons, next_at)


@dataclass
class ReminderDraft:
    customer_id: int
    text: str
    voice_text: str
    amount_paise: int
    lang: str = "hi"                 # reminder language; tts_language is the Gnani `language` for the voice note

    @property
    def tts_language(self) -> str:
        return "en-IN" if self.lang == "en" else ("hi-en" if self.lang == "hi" else self.lang)


def draft_reminder(ledger: Ledger, cfg: Settings, customer_id: int, shop: str,
                   tone: str = "gentle", now: float | None = None) -> tuple[GovernorDecision, ReminderDraft | None]:
    """Returns (decision, draft). Draft exists only if every rule passes AND the linter passes."""
    d = check(ledger, cfg, customer_id, now)
    if not d.allowed:
        return d, None
    with open(_SCRIPTS, encoding="utf-8") as fh:
        scripts = yaml.safe_load(fh)
    script = scripts.get(tone) or scripts["gentle"]
    bal = ledger.balance(customer_id)
    row = ledger.customer_row(customer_id)
    name = row["name"].split()[0]
    lang = "en" if (row["lang"] or "hi") == "en" else "hi"
    if lang == "en":
        en = scripts["en"]
        text = " ".join(str(en["text"]).split()).format(shop=shop, name=name, amount=format_inr(bal))
        voice = " ".join(str(en["voice"]).split()).format(shop=shop, name=name, amount_words=paise_to_words_en(bal))
    else:
        text = " ".join(str(script["hi"]).split()).format(
            shop=shop, name=name, amount=format_inr(bal), amount_words=paise_to_words(bal, "roman"))
        voice_src = script.get("hi_voice", script["hi"])
        voice = " ".join(str(voice_src).split()).format(
            shop=shop, name=name, amount=format_inr(bal), amount_words=paise_to_words(bal, "deva"))
    lr = lint(text)
    if not lr.ok:
        return GovernorDecision(False, ["tone_lint:" + ",".join(lr.violations)]), None
    return d, ReminderDraft(customer_id, text, voice, bal, lang)
