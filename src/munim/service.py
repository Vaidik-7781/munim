"""MunimService: the conversation brain. Transport-agnostic (Telegram, CLI, tests all use it).

Flow:  transcripts -> Scribe -> Auditor -> { SOFT: write now (+undo) | BLOCK: ask | ACT: do | ASK: clarify }
Every money write goes through the hash-chained ledger; no audio or names are written to the chain.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable

from munim.audit.auditor import ACT, ASK, BLOCK, SOFT, AuditContext, AuditResult, audit
from munim.config import Settings
from datetime import datetime

from munim.agent.timing import SLOT_HI
from munim.govern.governor import check, draft_reminder
from munim.insights.reports import render as render_report
from munim.scribe.dates_hi import describe
from munim.timeutil import IST
from munim.ledger.chain import Ledger
from munim.scribe.extract import PRONOUN, Extraction, extract
from munim.ingest.rows import Plan, clean_name
from munim.agent.qa import answer_rules, is_new_query
from munim.insights.statement import REFUSE_HI, build as build_statement
from munim.scribe.batch import Segment, is_batch, segments
from munim.scribe.names import Resolution, resolve
from munim.scribe.upi import looks_like_upi, parse_upi
from munim.scribe.indic_numbers import format_inr, paise_to_words

YES = {"haan", "han", "ha", "haa", "ji haan", "ji han", "sahi", "theek", "thik", "yes", "y", "ok", "okay",
       "हाँ", "हां", "सही", "ठीक"}
NO = {"nahi", "nahin", "na", "galat", "no", "n", "cancel", "नहीं", "गलत"}

HELP_HI = (
    "Munim ko aise bolein:\n"
    "• \"Sharma ji ko dedh sau udhaar likh do\"\n"
    "• \"Verma ji ne paanch sau diye\"\n"
    "• \"Sharma ji ka kitna baaki hai?\"\n"
    "• \"Sharma ji ko reminder bhejo\"  •  \"undo\"\n"
    "• \"Sharma ji shukravar tak dedh sau denge\" (vaada)\n"
    "• \"Sabse zyada udhaar kiska?\"  •  \"Aaj ka hisaab\"  •  \"Kaun bahut din se baaki?\"\n"
    "• \"Sharma ne last payment kab di?\"  •  \"Sharma ki history\"\n"
    "• Ek note mein kai entry: \"Sharma ko sau udhaar, Verma ne paanch sau diye\"\n"
    "• UPI ka SMS seedha paste karein — payment khud pehchaan lunga\n"
    "Naya customer: /add Naam  •  Vasooli plan: /plan  •  Subah ka hisaab: /briefing  •  /ask  •  /statement Naam"
)


WHY = {
    "low_snr": "awaaz saaf nahi thi", "short_clip": "awaaz bahut chhoti thi",
    "outlier": "raqam aam se zyada hai", "duplicate": "abhi abhi same entry hui thi",
    "multi_amount": "kai raqam suni", "b_missing": "dobara jaanch nahi ho paayi",
    "language_switch": "bhasha alag lagi", "intent_disagreement": "do baar alag samajh aaya",
    "name_disagreement": "naam alag suna", "customer_inferred": "naam pichli baat se samjha", "name_rescued": "naam doosri tarah se suna (roman mein)",
    "llm_disagreement": "AI ki doosri raay alag thi", "over_limit": "tay seema se zyada baaki ho jayega",
    "amount_disagreement": "do baar alag raqam suni", "scale_risk": "raqam 10 guna alag ho sakti hai",
    "ambiguous_name": "naam ek se zyada customer se mila", "missing_customer": "customer ka naam nahi mila",
    "missing_amount": "raqam nahi mili", "batch_duplicate": "isi list mein dobara aayi",
    "intent_unclear": "udhaar ya jama saaf nahi", "upi": "UPI message se padha",
    "ambient_edge": "dukaan ke mic se suna — ek baar pakka karein",
}

RISK_TEXT = {
    "old_balance": "purana baaki", "never_paid": "kabhi payment nahi", "stale_payment": "der se payment nahi",
    "low_collection": "chukaya bahut kam", "weak_collection": "chukaya kam", "broken_promise": "vaada tooTa",
    "over_limit": "seema se zyada",
}


@dataclass
class Pending:
    kind: str                       # confirm | choose_amount | choose_customer | approve_reminder
    intent: str
    customer_id: int | None
    amount_paise: int | None
    alt_amount_paise: int | None = None
    candidates: list[int] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    created: float = 0.0
    reminder_text: str = ""


@dataclass
class Reply:
    text: str
    voice_text: str | None = None
    buttons: list[tuple[str, str]] = field(default_factory=list)
    entry_seq: int | None = None
    wrote: bool = False
    action: str | None = None                                # e.g. "plan": the transport should await svc.plan()
    voices: list[tuple[str, str, float]] = field(default_factory=list)   # extra TTS notes: (text, language, speed)


class MunimService:
    def __init__(self, ledger: Ledger, cfg: Settings, shop_name: str = "Dukaan",
                 clock: Callable[[], float] = time.time):
        self.ledger, self.cfg, self.shop, self._clock = ledger, cfg, shop_name, clock
        self.pending: dict[int, Pending] = {}
        self.actors: dict[int, str] = {}                 # chat -> staff label (owner chats absent)
        self.last_batch: dict[int, list[int]] = {}
        self.last_customer: dict[int, tuple[int, float]] = {}     # conversation memory: chat -> (customer_id, ts)
        self.aux_factory = None          # set by Registry: () -> munim.store.Aux (inventory, mandates, insurance …)
        self.extras_cb = None            # set by Registry: (chat, data) -> Reply for 'x:…' callbacks

    # ---------------------------------------------------------------- entry points
    def on_typed(self, chat: int, text: str) -> Reply:
        text = text.strip()
        p = self._live_pending(chat)
        if p is not None:
            r = self._answer_pending(chat, p, text)
            if r is not None:
                return r
        if looks_like_upi(text):
            return self.on_upi(chat, text)
        ob = self.on_onboard(chat, text)
        if ob is not None:
            return ob
        q = self.on_query(chat, text)
        if q is not None:
            return q
        sk = self.on_stock(chat, text)
        if sk is not None:
            return sk
        custs = self.ledger.customers()
        segs = segments(text, custs, self._today())
        if is_batch(segs):
            return self.on_batch(chat, segs, segs, snr=None, dur=None, lang=None, src="text")
        ex = extract(text, custs, self._today())
        return self._process(chat, ex, ex, snr=None, dur=None, lang=None, src="text", req_ids=())

    def on_heard(self, chat: int, a_text: str, b_text: str | None, *, snr: float | None,
                 dur: float | None, lang: str | None = None, req_ids: tuple[str | None, ...] = (),
                 llm: Extraction | None = None, c_text: str | None = None, d_text: str | None = None,
                 src: str = "voice") -> Reply:
        p = self._live_pending(chat)
        if p is not None:
            r = self._answer_pending(chat, p, a_text.strip())
            if r is not None:
                return r
        ob = self.on_onboard(chat, a_text)
        if ob is not None:
            return ob
        q = self.on_query(chat, a_text)
        if q is not None:
            return q
        sk = self.on_stock(chat, a_text)
        if sk is not None:
            return sk
        custs = self.ledger.customers()
        sa = segments(a_text, custs, self._today())
        if is_batch(sa):
            sb = segments(b_text, custs, self._today()) if b_text is not None else None
            return self.on_batch(chat, sa, sb, snr=snr, dur=dur, lang=lang, src=src)
        ea = extract(a_text, custs, self._today())
        eb = extract(b_text, custs, self._today()) if b_text is not None else None
        ec = extract(c_text, custs, self._today()) if c_text else None
        if d_text and ea.customer is None:          # name rescue (Gnani transliterate pass): never trusted silently -> Auditor asks
            ed = extract(d_text, custs, self._today())
            if ed.customer is not None:
                ea.resolution, ea.rescued_customer = ed.resolution, True
                if eb is not None and eb.customer is None:
                    eb.resolution, eb.rescued_customer = ed.resolution, True
        return self._process(chat, ea, eb, snr=snr, dur=dur, lang=lang, src=src, req_ids=req_ids, llm=llm, ec=ec)

    def on_callback(self, chat: int, data: str) -> Reply:
        p = self._live_pending(chat)
        if data == "undo":
            return self._undo()
        if data == "undo_batch":
            return self._undo_batch(chat)
        if data.startswith("appr:"):
            return self._on_approval(data)
        if data.startswith("stockundo:"):
            from munim.inventory.hook import undo as _stock_undo
            return _stock_undo(self, data)
        if data.startswith("x:") and self.extras_cb is not None:
            return self.extras_cb(chat, data)
        if p is None:
            return Reply("Samay khatam ho gaya. Kripya dobara bolein.")
        if data == "yes" and p.kind == "batch":
            return self._confirm_batch(chat, p)
        if data == "yes" and p.kind == "ingest":
            return self._confirm_ingest(chat, p)
        if data == "yes" and p.kind == "add_customers":
            return self._confirm_add_customers(chat, p)
        if data == "yes" and p.kind == "new_sku":
            from munim.inventory.hook import confirm_new
            return confirm_new(self, chat, p)
        if data == "yes" and p.kind == "confirm_promise":
            return self._confirm_promise(chat, p)
        if data == "yes":
            return self._confirm(chat, p, p.amount_paise)
        if data == "no":
            self.pending.pop(chat, None)
            return Reply("Theek hai, kuch nahi likha. Dobara bolein.")
        if data.startswith("amt:"):
            return self._confirm(chat, p, int(data[4:]))
        if data.startswith("cust:"):
            return self._choose_customer(chat, p, int(data[5:]))
        if data == "approve_reminder" and p.kind == "approve_reminder":
            return self._approve_reminder(chat, p)
        return Reply("Samajh nahi aaya.")

    def on_stock(self, chat: int, text: str) -> Reply | None:
        """'2 kilo atta sold' / 'stocked 50 packets of Maggi' -> inventory event (never touches money)."""
        if self.aux_factory is None:
            return None
        from munim.inventory.hook import handle
        return handle(self, chat, text)

    def _today(self):
        return datetime.fromtimestamp(self._clock(), IST).date()

    # ---------------------------------------------------------------- pending handling
    def _live_pending(self, chat: int) -> Pending | None:
        p = self.pending.get(chat)
        ttl = 300 if (p and p.kind in ("batch", "ingest", "add_customers")) else self.cfg.block_wait_seconds
        if p and self._clock() - p.created > ttl:
            self.pending.pop(chat, None)
            return None
        return p

    def _answer_pending(self, chat: int, p: Pending, text: str) -> Reply | None:
        low = " ".join(text.lower().replace(".", " ").replace("!", " ").split())
        if p.kind in ("confirm", "approve_reminder", "confirm_promise", "batch", "ingest", "add_customers", "new_sku"):
            if low in YES:
                return self.on_callback(chat, "approve_reminder" if p.kind == "approve_reminder" else "yes")
            if low in NO:
                return self.on_callback(chat, "no")
        if p.kind == "choose_amount" and low in NO:
            return self.on_callback(chat, "no")
        if p.kind == "choose_amount" and low in ("1", "pehla", "pehle"):
            return self._confirm(chat, p, p.amount_paise)
        if p.kind == "choose_amount" and low in ("2", "doosra", "dusra"):
            return self._confirm(chat, p, p.alt_amount_paise)
        if p.kind == "choose_customer":
            if low.isdigit() and 1 <= int(low) <= len(p.candidates):
                return self._choose_customer(chat, p, p.candidates[int(low) - 1])
            ex = extract(text, [c for c in self.ledger.customers() if c.id in p.candidates])
            if ex.customer is not None:
                return self._choose_customer(chat, p, ex.customer.id)
        return None   # not an answer: treat as a fresh command

    def _choose_customer(self, chat: int, p: Pending, cid: int) -> Reply:
        self.pending.pop(chat, None)
        p.customer_id = cid
        if p.intent in ("add_udhaar", "record_payment") and p.amount_paise is not None:
            p.kind = "confirm"
            return self._ask_confirm(chat, p)
        return self._execute(chat, p.intent, cid, p.amount_paise, p.meta)

    # ---------------------------------------------------------------- core
    def _name(self, cid: int) -> str:
        return self.ledger.customer_row(cid)["name"]

    def _apply_context(self, chat: int, ex: Extraction | None) -> None:
        """'unhi ko 50 aur jod do' -> last customer in this chat (≤10 min). Marked inferred => Auditor asks."""
        if ex is None or ex.resolution.status != "none" or not PRONOUN.search(ex.transcript):
            return
        mem = self.last_customer.get(chat)
        if mem and self._clock() - mem[1] <= 600:
            row = next((c for c in self.ledger.customers() if c.id == mem[0]), None)
            if row is not None:
                ex.resolution = Resolution("match", row, [(row, 100.0)], None, 100.0)
                ex.inferred_customer = True

    def _process(self, chat: int, ea: Extraction, eb: Extraction | None, *, snr, dur, lang, src, req_ids,
                 llm: Extraction | None = None, ec: Extraction | None = None) -> Reply:
        self._apply_context(chat, ea)
        self._apply_context(chat, eb)
        cust = ea.customer
        hist = self.ledger.history_amounts(cust.id) if cust else []
        bal = self.ledger.balance(cust.id) if cust else 0
        lim = self.ledger.customer_row(cust.id)["credit_limit"] if cust else None
        ctx = AuditContext(a=ea, b=eb, snr_db=snr, duration_s=dur, lang_a=lang, history_paise=hist,
                           recent=self.ledger.recent_writes(self.cfg.duplicate_window_seconds),
                           balance_paise=bal, credit_limit_paise=lim, llm=llm)
        res = audit(ctx, self.cfg)
        if src == "edge" and res.decision == SOFT:      # ambient mic: never a silent write; one owner tap (see docs/EDGE.md)
            from munim.audit.auditor import Flag
            res.flags.append(Flag("ambient_edge"))
            res.decision = BLOCK
        meta = {"src": src, "flags": res.codes, "req": [r for r in req_ids if r]}
        now = self._clock()
        if res.customer is not None:
            self.last_customer[chat] = (res.customer.id, now)
        if res.decision == ACT and res.intent == "report_query" and ea.report == "plan":
            return Reply("Vasooli list taiyaar karta hoon…", action="plan")

        if res.decision == ACT and res.intent == "report_query":
            r = render_report(ea.report or "total", self.ledger, now)
            return Reply(r.text, voice_text=r.voice)
        if res.decision == ACT and res.intent == "promise_to_pay":
            return self._ask_promise(chat, res, ea, meta, now)
        if res.decision == ACT:
            return self._execute(chat, res.intent, res.customer.id if res.customer else None, None, meta)
        if res.decision == ASK:
            return self._clarify(chat, res, meta, now)
        if res.decision == SOFT:
            return self._execute(chat, res.intent, res.customer.id, res.amount_paise, meta)
        # BLOCK
        return self._block(chat, res, meta, now, ec)

    def _clarify(self, chat: int, res: AuditResult, meta: dict, now: float) -> Reply:
        codes = res.codes
        if "missing_date" in codes:
            return Reply("Kab tak dene ka vaada hai? Jaise \"shukravar tak\" ya \"15 tarikh tak\" bolein.")
        if "unknown_intent" in codes:
            return Reply("Samajh nahi aaya. " + HELP_HI)
        if "ambiguous_name" in codes:
            return self._ask_which_customer(chat, res, meta, now)
        if "missing_customer" in codes:
            return Reply("Yeh naam khate mein nahi mila. Pehle jodein: /add Naam")
        return Reply("Samajh nahi aaya. " + HELP_HI)

    def _ask_promise(self, chat: int, res: AuditResult, ea: Extraction, meta: dict, now: float) -> Reply:
        cid, when = res.customer.id, ea.when
        amt = ea.amount_paise
        p = Pending("confirm_promise", "promise_to_pay", cid, amt, meta={**meta, "due": when.day.isoformat()},
                    created=now)
        self.pending[chat] = p
        amt_txt = f" {format_inr(amt)}" if amt else ""
        warn = f"\n(Dhyaan: \"{when.phrase}\" ka matlab {describe(when.day)} liya)" if when.ambiguous else ""
        return Reply(f"{self._name(cid)}: {describe(when.day)} tak{amt_txt} dene ka vaada. Sahi hai?{warn}",
                     voice_text=f"{describe(when.day)} tak dene ka vaada. Sahi hai?",
                     buttons=[("Haan ✅", "yes"), ("Nahi ❌", "no")])

    def _confirm_promise(self, chat: int, p: Pending) -> Reply:
        self.pending.pop(chat, None)
        e = self.ledger.add_promise(p.customer_id, p.meta["due"], p.amount_paise, src=p.meta.get("src"))
        from datetime import date as _d

        return Reply(f"🤝 {self._name(p.customer_id)}: vaada darj ({describe(_d.fromisoformat(p.meta['due']))}). "
                     "Tab tak reminder nahi jayega; tareekh nikalne par main bataunga.", entry_seq=e.seq, wrote=True)

    def _ask_which_customer(self, chat, res, meta, now) -> Reply:
        ids = [c.id for c in res.candidates]
        self.pending[chat] = Pending("choose_customer", res.intent, None, res.amount_paise, None, ids,
                                     res.codes, meta, now)
        lines = [f"{i + 1}) {c.name}" for i, c in enumerate(res.candidates)]
        first = res.candidates[0].name.split()[-1]
        return Reply(f"Kaun sa {first}?\n" + "\n".join(lines),
                     voice_text=f"Kaun sa {first}?",
                     buttons=[(c.name, f"cust:{c.id}") for c in res.candidates])

    def _block(self, chat: int, res: AuditResult, meta: dict, now: float, ec: Extraction | None = None) -> Reply:
        codes = set(res.codes)
        if "ambiguous_name" in codes:
            return self._ask_which_customer(chat, res, meta, now)
        if "missing_customer" in codes:
            return Reply("Yeh naam khate mein nahi mila. Pehle jodein: /add Naam")
        if "missing_amount" in codes:
            return Reply("Raqam samajh nahi aayi. Kitne rupaye? Dobara bolein, jaise \"dedh sau\".")
        cid = res.customer.id
        if "amount_disagreement" in codes and res.alt_amount_paise and res.amount_paise:
            self.pending[chat] = Pending("choose_amount", res.intent, cid, res.amount_paise,
                                         res.alt_amount_paise, [], res.codes, meta, now)
            a, b = format_inr(res.amount_paise), format_inr(res.alt_amount_paise)
            pick = None                      # third ear only RANKS the options; the shopkeeper still decides
            if ec is not None and ec.amount_paise in (res.amount_paise, res.alt_amount_paise):
                pick = ec.amount_paise
            note = f"\n(Teesri baar sunne par {format_inr(pick)} jaisa laga ⭐)" if pick else ""
            opts = [(a, res.amount_paise), (b, res.alt_amount_paise)]
            if pick == res.alt_amount_paise:
                opts.reverse()
            return Reply(f"{self._name(cid)}: do tarah se suna — 1) {a}  2) {b}. Kaun sa sahi hai?{note}",
                         voice_text=f"Do tarah se suna. {paise_to_words(res.amount_paise)} ya "
                                    f"{paise_to_words(res.alt_amount_paise)}. Kaun sa sahi hai?",
                         buttons=[(("⭐ " if v == pick else "") + t, f"amt:{v}") for t, v in opts] + [("Dono galat", "no")])
        p = Pending("confirm", res.intent, cid, res.amount_paise, None, [], res.codes, meta, now)
        return self._ask_confirm(chat, p)

    def _ask_confirm(self, chat: int, p: Pending) -> Reply:
        p.created = self._clock()
        self.pending[chat] = p
        verb = "udhaar likhna" if p.intent == "add_udhaar" else "jama (payment) likhna"
        reasons = [WHY[f] for f in p.flags if f in WHY]
        amt = format_inr(p.amount_paise or 0)
        txt = f"{self._name(p.customer_id)} — {amt} {verb}. Sahi hai?"
        if reasons:
            txt += f"\n(Poochh raha hoon kyunki: {', '.join(reasons)})"
        return Reply(txt,
                     voice_text=f"{paise_to_words(p.amount_paise or 0)} {verb}. Sahi hai?",
                     buttons=[("Haan ✅", "yes"), ("Nahi ❌", "no")])

    def _confirm(self, chat: int, p: Pending, amount: int | None) -> Reply:
        self.pending.pop(chat, None)
        if amount is None or p.customer_id is None:
            return Reply("Kuch gadbad hui, dobara bolein.")
        meta = dict(p.meta)
        meta["confirmed"] = True
        return self._execute(chat, p.intent, p.customer_id, amount, meta)

    # ---------------------------------------------------------------- actions
    def _execute(self, chat: int, intent: str, cid: int | None, amount: int | None, meta: dict) -> Reply:
        L = self.ledger
        if chat in self.actors:
            meta = {**meta, "by": self.actors[chat]}
        if intent == "help":
            return Reply(HELP_HI)
        if intent == "undo_last":
            return self._undo()
        if cid is None:
            return Reply("Customer samajh nahi aaya. Dobara bolein.")
        name = self._name(cid)
        if intent == "add_udhaar" and amount:
            e = L.add_udhaar(cid, amount, **meta)
            bal = L.balance(cid)
            return Reply(f"✅ {name}: {format_inr(amount)} udhaar likh diya. Ab baaki: {format_inr(bal)}.",
                         voice_text=f"Likh diya. {paise_to_words(amount)} udhaar. Ab kul baaki "
                                    f"{paise_to_words(max(bal, 0))}.",
                         buttons=[("Undo ↩️", "undo")], entry_seq=e.seq, wrote=True)
        if intent == "record_payment" and amount:
            before = L.balance(cid)
            e = L.record_payment(cid, amount, **meta)
            bal = L.balance(cid)
            extra = f" ({format_inr(amount - before)} zyada — advance maana gaya)" if before >= 0 and amount > before else ""
            return Reply(f"✅ {name}: {format_inr(amount)} jama. Ab baaki: {format_inr(max(bal, 0))}.{extra}",
                         voice_text=f"Jama kar diya. {paise_to_words(amount)}. Ab baaki "
                                    f"{paise_to_words(max(bal, 0))}.",
                         buttons=[("Undo ↩️", "undo")], entry_seq=e.seq, wrote=True)
        if intent == "balance_query":
            bal = L.balance(cid)
            return Reply(f"{name} ka baaki: {format_inr(bal)}",
                         voice_text=f"{name.split()[0]} ka baaki {paise_to_words(max(bal, 0))} hai.")
        if intent == "pause_customer":
            L.set_flag(cid, paused=1)
            return Reply(f"⏸️ {name} ke reminder ruk gaye.")
        if intent == "resume_customer":
            L.set_flag(cid, paused=0)
            return Reply(f"▶️ {name} ke reminder dobara chalu.")
        if intent == "opt_out_customer":
            L.revoke_consent(cid)
            return Reply(f"🔕 {name} ko ab koi reminder nahi jayega.")
        if intent == "dispute_entry":
            L.open_dispute(cid, None, "raised via bot")
            return Reply(f"⚠️ {name} ke khate par vivaad darj. Jab tak suljhe, reminder nahi jayenge.")
        if intent == "send_reminder":
            return self._start_reminder(chat, cid)
        return Reply("Samajh nahi aaya. " + HELP_HI)

    def _undo(self) -> Reply:
        rev = self.ledger.undo_last()
        if rev is None:
            return Reply("Undo karne ke liye kuch nahi hai.")
        return Reply(f"↩️ Pichli entry ({format_inr(rev.amount_paise)}) ulat di gayi. Purana record surakshit hai.")

    def _start_reminder(self, chat: int, cid: int) -> Reply:
        d, draft = draft_reminder(self.ledger, self.cfg, cid, self.shop, now=self._clock())
        name = self._name(cid)
        if draft is None:
            return Reply(f"Reminder nahi bhej sakta: {d.explain_hi()}.")
        self.pending[chat] = Pending("approve_reminder", "send_reminder", cid, draft.amount_paise,
                                     created=self._clock(), reminder_text=draft.text)
        return Reply(f"{name} ke liye reminder taiyaar hai:\n\n“{draft.text}”\n\nBhejne ki ijaazat dein?",
                     voice_text=draft.voice_text,
                     buttons=[("Haan, bhejein ✅", "approve_reminder"), ("Nahi ❌", "no")])

    def start_reminder_with(self, chat: int, cid: int, text: str, voice: str, tts_language: str, note: str = "") -> Reply:
        """Preview an already-generated (and validated) reminder variant for one-tap approval. Governor re-checks on approval."""
        self.pending[chat] = Pending("approve_reminder", "send_reminder", cid, None, created=self._clock(), reminder_text=text)
        return Reply(f"{self._name(cid)} ke liye reminder taiyaar hai:\n\n“{text}”\n\n{note}\nBhejne ki ijaazat dein?".replace("\n\n\n", "\n\n"),
                     voices=[(voice, tts_language, 0.95)] if voice else [],
                     buttons=[("Haan, bhejein ✅", "approve_reminder"), ("Nahi ❌", "no")])

    def _approve_reminder(self, chat: int, p: Pending) -> Reply:
        self.pending.pop(chat, None)
        d = draft_reminder(self.ledger, self.cfg, p.customer_id, self.shop, now=self._clock())[0]
        if not d.allowed:
            return Reply(f"Ab reminder nahi bhej sakta: {d.explain_hi()}.")
        self.ledger.log_reminder(p.customer_id)
        return Reply("✅ Reminder manzoor. Upar ka sandesh customer ko forward kar dein; log mein darj ho gaya.")


    # ---------------------------------------------------------------- agent: collections plan + approvals
    async def plan(self, chat: int, planner=None) -> Reply:
        from munim.agent.runner import run_agent

        run = await run_agent(self.ledger, self.cfg, self.shop, self._clock(), planner)
        items = self.ledger.pending_approvals()
        if not items:
            return Reply("Aaj kisi ko reminder bhejne layak nahi mila (rules ya vaade ki wajah se).",
                         voice_text="Aaj kisi ko reminder bhejne layak nahi mila.")
        lines, buttons = [], []
        for it in items:
            nm = self._name(it["customer_id"])
            why = ", ".join(RISK_TEXT.get(c, c) for c in (it["reason"] or "").split(";") if c)[:90]
            lines.append(f"• {nm}: {format_inr(it['payload']['amount_paise'])}" + (f" — {why}" if why else ""))
            buttons.append((f"✅ {nm.split()[0]}", f"appr:{it['id']}:y"))
        buttons += [("Sab ✅", "appr:all"), ("Chhodo ❌", "appr:none")]
        how = f"(planner: {run.planner}" + (f"; AI fail hua to rules par laut gaya" if run.fallback_reason else "") + f", {len(run.trace)} kadam)"
        return Reply("Aaj ki vasooli list — har ek ke liye aapki ijaazat chahiye:\n" + "\n".join(lines) + "\n" + how,
                     voice_text=f"Aaj {len(items)} customer ko yaad dilane layak hain. Kis ko bhejna hai, chun lijiye.",
                     buttons=buttons)

    def _on_approval(self, data: str) -> Reply:
        L = self.ledger
        _, what, *rest = data.split(":")
        if what == "none":
            for it in L.pending_approvals():
                L.decide_approval(it["id"], False)
            return Reply("Theek hai, koi reminder nahi bheja.")
        ids = [it["id"] for it in L.pending_approvals()] if what == "all" else [int(what)]
        out: list[str] = []
        voices: list[tuple[str, str, float]] = []
        for aid in ids:
            item = next((i for i in L.pending_approvals() if i["id"] == aid), None)
            if item is None:
                continue
            d = check(L, self.cfg, item["customer_id"], self._clock())     # re-check: rules may have changed since drafting
            nm = self._name(item["customer_id"])
            if not d.allowed:
                L.decide_approval(aid, False)
                out.append(f"✖ {nm}: nahi bheja — {d.explain_hi()}.")
                continue
            L.decide_approval(aid, True)
            L.log_reminder(item["customer_id"], item["kind"])
            out.append(f"✅ {nm}:\n“{item['payload']['text']}”" +
                       (f"\n(behtar samay: {SLOT_HI[item['payload']['slot']]})" if item['payload'].get('slot') in SLOT_HI else ""))
            if item["payload"].get("voice"):
                voices.append((item["payload"]["voice"], item["payload"].get("tts_language", "hi-en"), 0.95))
        return Reply("\n\n".join(out) + "\n\nUpar ke sandesh (aur voice note) customer ko forward kar dein; sab log mein darj hai." if out else "Kuch bacha nahi.",
                     voices=voices)



    # ---------------------------------------------------------------- read-only questions + statement
    def on_query(self, chat: int, text: str) -> Reply | None:
        """Questions the older intents don't cover (last payment, history, ageing, forecast). Read-only, so no two-ear gate."""
        if not is_new_query(text):
            return None
        qa, cid = answer_rules(self.ledger, self._clock(), text)
        if qa is None:
            return None
        if cid is not None:
            self.last_customer[chat] = (cid, self._clock())
        return Reply(qa.text, voice_text=qa.voice)

    def statement(self, chat: int, cid: int, asked: bool = False) -> Reply:
        st = build_statement(self.ledger, cid, self._clock(), self.shop, asked=asked)
        if not st.ok:
            return Reply(f"Statement nahi bana: {REFUSE_HI.get(st.reason, st.reason)}.")
        return Reply("📄 Customer ko bhejne ke liye (aap forward karein):\n\n" + st.text,
                     voices=[(st.voice_text, st.tts_language, 0.95)])

    # ---------------------------------------------------------------- onboarding + ingest (CSV / scan)
    _ONBOARD = re.compile(r"^\s*(?:naye?|new|add)\s+(?:customers?|grahak|khate?)\s*[:\-]?\s*(.+)$", re.I | re.S)

    def on_onboard(self, chat: int, text: str) -> Reply | None:
        """'naye customer: Ramesh Sharma, Sunita Verma aur Mohan Das' (typed or spoken) -> one confirm, then added."""
        m = self._ONBOARD.match(text)
        if not m:
            return None
        parts = re.split(r"\s*(?:,|;|\n|\baur\b|\band\b)\s*", m.group(1).strip())
        names, bad = [], []
        have = {c.name.lower() for c in self.ledger.customers()}
        for raw in parts:
            if not raw.strip():
                continue
            n = clean_name(raw.strip().title())
            if n is None:
                bad.append(raw.strip()[:25])
            elif n.lower() in have or n in names:
                bad.append(f"{n} (pehle se hai)")
            else:
                names.append(n)
        if not names:
            return Reply("Koi naya naam nahi mila." + (f" Chhoda: {', '.join(bad)}" if bad else ""))
        self.pending[chat] = Pending("add_customers", "add_customers", None, None, meta={"names": names[:50]}, created=self._clock())
        txt = f"{len(names)} naye customer joden?\n" + "\n".join(f"• {n}" for n in names[:50])
        if bad:
            txt += f"\n\nChhoda: {', '.join(bad)}"
        return Reply(txt, voice_text=f"{len(names)} naye customer jodun?", buttons=[("Haan, jodo ✅", "yes"), ("Nahi ❌", "no")])

    def _confirm_add_customers(self, chat: int, p: Pending) -> Reply:
        self.pending.pop(chat, None)
        for n in p.meta["names"]:
            self.ledger.add_customer(n)
        return Reply(f"✅ {len(p.meta['names'])} customer jod diye. Ab unka udhaar bolkar likhwa sakte hain.", wrote=False)

    def stage_ingest(self, chat: int, plan: Plan, source: str) -> Reply:
        """Existing records (CSV or scanned page) -> ONE review list; nothing is written until the owner confirms."""
        if not plan.rows:
            why = "; ".join(f"{a}: {b}" for a, b in plan.rejected[:5]) or "kuch nahi mila"
            return Reply(f"Koi row jodne layak nahi mili. {why}")
        rows = plan.rows
        lines = []
        for r in rows[:25]:
            sign = "udhaar" if r.kind == "add_udhaar" else "jama"
            lines.append(f"• {r.name} — {sign} {format_inr(r.amount_paise)}" + (f"  [{'; '.join(r.flags)}]" if r.flags else ""))
        more = f"\n… aur {len(rows) - 25} row" if len(rows) > 25 else ""
        given = sum(r.amount_paise for r in rows if r.kind == "add_udhaar")
        got = sum(r.amount_paise for r in rows if r.kind == "record_payment")
        txt = (f"📥 {len(rows)} row mili ({source}). Kul udhaar {format_inr(given)}, jama {format_inr(got)}.\n" + "\n".join(lines) + more)
        if plan.rejected:
            txt += "\n\nChhodi gayi:\n" + "\n".join(f"✖ {a}: {b}" for a, b in plan.rejected[:8])
        txt += "\n\nSab milakar dekh liya? Tab hi 'Jodo' dabayein — isse pehle kuch nahi likha jayega."
        self.pending[chat] = Pending("ingest", "ingest", None, None, meta={"rows": [vars(r) for r in rows], "source": source},
                                     created=self._clock())
        return Reply(txt, buttons=[(f"Sab jodo ✅ ({len(rows)})", "yes"), ("Nahi ❌", "no")])

    def _confirm_ingest(self, chat: int, p: Pending) -> Reply:
        self.pending.pop(chat, None)
        seqs, made = [], 0
        for r in p.meta["rows"]:
            cid = r["customer_id"]
            if cid is None:
                existing = next((c for c in self.ledger.customers() if c.name.lower() == r["name"].lower()), None)
                cid = existing.id if existing else self.ledger.add_customer(r["name"]).id
                made += 0 if existing else 1
            fn = self.ledger.add_udhaar if r["kind"] == "add_udhaar" else self.ledger.record_payment
            extra = {"by": self.actors[chat]} if chat in self.actors else {}
            seqs.append(fn(cid, r["amount_paise"], src=p.meta["source"], opening=True, confirmed=True,
                           **({"as_of": r["as_of"]} if r.get("as_of") else {}), **extra).seq)
        self.last_batch[chat] = seqs
        return Reply(f"✅ {len(seqs)} entry jodi ({made} naye customer).", buttons=[("Undo import ↩️", "undo_batch")], wrote=True)

    # ---------------------------------------------------------------- batch dictation
    def on_batch(self, chat: int, sa: list[Segment], sb: list[Segment] | None, *, snr, dur, lang, src: str) -> Reply:
        """Several entries in one note. Never written silently: clean lines -> ONE review list, flagged lines held back."""
        sa = sa[:12]
        ok: list[dict] = []
        held: list[tuple[str, list[str]]] = []
        seen: set[tuple[int, str, int]] = set()
        usable_b = [x for x in (sb or []) if x.usable]
        for seg in sa:
            ex = seg.ex
            label = f"“{seg.text[:40]}”"
            if ex.customer is None or not ex.spans:
                held.append((label, ["missing_customer" if ex.customer is None else "missing_amount"]))
                continue
            if ex.intent not in ("add_udhaar", "record_payment"):
                held.append((label, ["intent_unclear"]))
                continue
            eb = None
            if src == "text":
                eb = ex
            elif usable_b:
                same = [x for x in usable_b if x.ex.customer.id == ex.customer.id]
                if same:
                    eb = min(same, key=lambda x: (x.ex.intent != ex.intent,
                                                  abs((x.ex.amount_paise or 0) - (ex.amount_paise or 0)))).ex
            cid = ex.customer.id
            ctx = AuditContext(a=ex, b=eb, snr_db=snr, duration_s=dur, lang_a=lang,
                               history_paise=self.ledger.history_amounts(cid),
                               recent=self.ledger.recent_writes(self.cfg.duplicate_window_seconds),
                               balance_paise=self.ledger.balance(cid),
                               credit_limit_paise=self.ledger.customer_row(cid)["credit_limit"])
            res = audit(ctx, self.cfg)
            codes = list(res.codes)
            key = (cid, res.intent, res.amount_paise or 0)
            if key in seen:
                codes.append("batch_duplicate")
            seen.add(key)
            if res.decision != SOFT or codes:
                held.append((f"{self._name(cid)} — {format_inr(ex.amount_paise or 0)}", codes or ["intent_unclear"]))
                continue
            ok.append({"cid": cid, "intent": res.intent, "amount": res.amount_paise, "inherited": seg.inherited})
        if not ok:
            lines = "\n".join(f"⚠️ {l}: {', '.join(WHY.get(c, c) for c in cs)}" for l, cs in held)
            return Reply("Koi entry seedhi likhne layak nahi mili. Ek-ek karke bolein.\n" + lines)
        given = sum(i["amount"] for i in ok if i["intent"] == "add_udhaar")
        got = sum(i["amount"] for i in ok if i["intent"] == "record_payment")
        lines = [f"{'➕ udhaar' if i['intent'] == 'add_udhaar' else '💰 jama'} — {self._name(i['cid'])}: {format_inr(i['amount'])}"
                 + (" (baaki lines ke hisaab se)" if i["inherited"] else "") for i in ok]
        txt = f"📋 {len(ok)} entry taiyaar:\n" + "\n".join(lines)
        txt += f"\nKul udhaar {format_inr(given)}, kul jama {format_inr(got)}."
        if held:
            txt += "\n\nRoka hua (ek-ek karke bolein):\n" + "\n".join(
                f"⚠️ {l}: {', '.join(WHY.get(c, c) for c in cs)}" for l, cs in held)
        txt += "\n\nSab sahi hai? Ek tap mein likh doon?"
        self.pending[chat] = Pending("batch", "batch", None, None, meta={"items": ok, "src": src},
                                     created=self._clock())
        voice = (f"{len(ok)} entry taiyaar hain. Kul udhaar {paise_to_words(given)}, kul jama {paise_to_words(got)}."
                 + (f" {len(held)} entry roki hain." if held else "") + " Likh doon?")
        return Reply(txt, voice_text=voice, buttons=[(f"Sab likho ✅ ({len(ok)})", "yes"), ("Nahi ❌", "no")])

    def _confirm_batch(self, chat: int, p: Pending) -> Reply:
        self.pending.pop(chat, None)
        seqs: list[int] = []
        for i in p.meta["items"]:
            fn = self.ledger.add_udhaar if i["intent"] == "add_udhaar" else self.ledger.record_payment
            extra = {"by": self.actors[chat]} if chat in self.actors else {}
            e = fn(i["cid"], i["amount"], src="batch", via=p.meta.get("src", "?"), confirmed=True, **extra)
            seqs.append(e.seq)
        self.last_batch[chat] = seqs
        self.last_customer[chat] = (p.meta["items"][-1]["cid"], self._clock())
        return Reply(f"✅ {len(seqs)} entry likh di.",
                     voice_text=f"{len(seqs)} entry likh di.", buttons=[("Undo batch ↩️", "undo_batch")], wrote=True)

    def _undo_batch(self, chat: int) -> Reply:
        seqs = self.last_batch.pop(chat, [])
        n = sum(1 for s in seqs if self.ledger.reverse_seq(s, "batch_undo") is not None)
        return Reply(f"↩️ {n} entry wapas li (history mein reversal darj hai).") if n else Reply("Wapas lene ko kuch nahi mila.")

    # ---------------------------------------------------------------- UPI reconciliation
    def on_upi(self, chat: int, text: str) -> Reply:
        """A pasted UPI/bank credit message -> proposed record_payment. Credits only; de-duplicated by keyed reference."""
        cr = parse_upi(text)
        if cr is None:
            return Reply("Yeh payment-credit message saaf nahi laga (ek hi raqam aur 'credited/received' chahiye). "
                         "Naam aur raqam likh kar bata dein, jaise: Sharma ne 500 diye.")
        tok = self.ledger.pseudo("upi", cr.ref) if cr.ref else None
        if tok and self.ledger.meta_token_seen("upi", tok):
            return Reply("ℹ️ Yeh UPI payment pehle hi khate mein likhi ja chuki hai (same reference). Dobara nahi likhi.")
        meta = {"src": "upi", **({"upi": tok} if tok else {})}
        customers = self.ledger.customers()
        r = resolve(cr.payer, customers) if cr.payer else None
        if r is not None and r.status == "match":
            p = Pending("confirm", "record_payment", r.customer.id, cr.amount_paise, flags=["upi"], meta=meta,
                        created=self._clock())
            rep = self._ask_confirm(chat, p)
            rep.text = (f"UPI: {r.customer.name} ({cr.payer}) se {format_inr(cr.amount_paise)} aaye.\n"
                        f"Kya yeh unki payment likh doon?")
            return rep
        from munim.insights import engine as E
        if r is not None and r.status == "ask":
            cands = [c for c, _ in r.candidates[:4]]
        else:
            cands = []
        if not cands:
            cands = [c for c, _ in E.outstanding(self.ledger)[:4]]
        if not cands:
            return Reply("Koi customer nahi mila jisse yeh payment jodi ja sake. Pehle /add se customer jodein.")
        self.pending[chat] = Pending("choose_customer", "record_payment", None, cr.amount_paise,
                                     candidates=[c.id for c in cands], flags=["upi"], meta=meta, created=self._clock())
        who = f"“{cr.payer}” se" if cr.payer else ""
        return Reply(f"UPI: {format_inr(cr.amount_paise)} {who} aaye. Kis customer ki payment hai?",
                     buttons=[(c.name, f"cust:{c.id}") for c in cands])

    # ---------------------------------------------------------------- inbound CUSTOMER voice/text (forwarded by shopkeeper)
    def on_customer_message(self, chat: int, customer_id: int, text: str, *, snr: float | None = None,
                            dur: float | None = None) -> Reply:
        """The shopkeeper forwards what a customer said back (e.g. a reply voice note) after `/from <naam>`.
        Safe by design: opt-out and disputes take effect immediately (they only REDUCE what Munim does);
        anything involving money or promises waits for the shopkeeper's explicit confirmation."""
        cust = next((c for c in self.ledger.customers() if c.id == customer_id), None)
        if cust is None:
            return Reply("Customer nahi mila.")
        ex = extract(text, [cust], self._today())
        ex.resolution = Resolution("match", cust, [(cust, 100.0)], None, 100.0)
        name = self._name(customer_id)
        low = text.lower()
        intent = ex.intent
        if intent == "pause_customer" or re.search(r"\b(mat\s*bhejo|band\s*karo|nahi\s*chahiye|stop|unsubscribe)\b", low):
            self.ledger.revoke_consent(customer_id)
            return Reply(f"🔕 {name} ne sandesh band karne ko kaha — reminder band kar diye (turant).")
        if intent == "dispute_entry" or re.search(r"\b(galat|ghalat|wrong|nahi\s*liya|maine\s*nahi)\b", low):
            self.ledger.open_dispute(customer_id, None, "raised by customer message")
            return Reply(f"⚠️ {name} hisaab par aapatti utha rahe hain — vivaad darj; jaanchne tak reminder nahi jayenge.")
        if intent == "promise_to_pay":
            if ex.when is None:
                return Reply(f"{name} dene ka vaada kar rahe hain, par tareekh samajh nahi aayi. Kab tak?")
            res = AuditResult("ACT", [], intent, cust, ex.amount_paise)
            return self._ask_promise(chat, res, ex, {"src": "customer_voice"}, self._clock())
        if intent == "record_payment":
            amt = ex.amount_paise
            if not amt:
                return Reply(f"{name} kehte hain ki unhone de diye, par raqam samajh nahi aayi. Kitne?")
            p = Pending("confirm", "record_payment", customer_id, amt, flags=["customer_claim"],
                        meta={"src": "customer_voice", "snr": snr}, created=self._clock())
            r = self._ask_confirm(chat, p)
            r.text = (f"{name} ka dava hai ki unhone {format_inr(amt)} de diye. Kya aapko mile?\n"
                      "Sirf tab 'Haan' dabayein jab paise sach mein mile hon.")
            return r
        if intent == "balance_query":
            bal = self.ledger.balance(customer_id)
            return Reply(f"{name} puchh rahe hain kitna baaki hai: {format_inr(bal)}. Unhe yehi bata dein.")
        return Reply(f"{name} ne kaha: “{text[:200]}” — kuch karne layak nahi samjha. Aap batayein.")
