"""Telegram transport (multi-shop). Thin on purpose: all decisions live in MunimService / the agent layer.

Every chat maps to one shop and one role via the Registry: owner, staff (munshi) or customer.
Handlers are wrapped by `gate(action)`, which denies by default and injects the shop (tenant) and member.
"""
from __future__ import annotations

import functools
import io
import logging
import time
from datetime import time as dtime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from munim import portal
from munim.agent.planner import LLMPlanner, RulePlanner
from munim.agent.qa import answer as qa_answer
from munim.config import Settings
from munim.gnani.client import GnaniAuthError, GnaniClient, GnaniCreditsExhausted, GnaniError
from munim.gnani.stt import pass_a_prisma_v3
from munim.gnani.tts import ALL_VOICES, Speaker
from munim.govern.genreminder import GenReminder
from munim.govern.governor import draft_reminder
from munim.insights import engine as E
from munim.insights.forecast import forecast
from munim.insights.reports import morning_briefing, render
from munim.books import payables
from munim.ingest.csv_import import parse_csv
from munim.ingest.khata_scan import scan_page
from munim.insights import dispute_pack
from munim.ops import export as ops_export
from munim.insights.staff_audit import audit_staff
from munim.pay.upi_link import qr_png, upi_uri, valid_vpa
from munim.ml.credit_model import NAMES_HI, CreditModel, build_samples, score_customer, weekly_cutoffs
from munim.listen.listener import AudioDecodeError, AudioTooLong, listen, listen_long, prepare
from munim.llm.client import LLMClient
from munim.public.break_munim import SANDBOX_NAMES, trace
from munim.public.caps import PublicCaps
from munim.scribe.indic_numbers import format_inr, parse_amount
from munim.scribe.llm_spans import LLMSpans
from munim.scribe.names import Customer, resolve
from munim.service import HELP_HI, Reply
from munim.tenancy.registry import Member, Registry, Tenant
from munim.tenancy.roles import COMMAND_ACTION, CUSTOMER, ENTRY, OWNER, STAFF, UNDO, COMMUNICATE, can
from munim.timeutil import IST

log = logging.getLogger("munim.bot")

WELCOME = (
    "Namaste! Main Munim hoon — aapka awaaz se chalne wala udhaar-khata.\n"
    "Voice note bhejein: \"Sharma ji ko dedh sau udhaar likh do\".\n"
    "Har entry do baar jaanchi jati hai; shaq hone par main poochhta hoon, andaza nahi lagata.\n\n"
    + HELP_HI
    + "\n\nAur commands: /ask /statement /risk /forecast /anomaly /limit /from /lang /voice /voices /invite /staff /stats\n"
    "Naye: /stock /price /count /ondc /pincode /mandate /insure /pool /passport /loan /policy /recon /invoice\n"
    "Lamba voice note (90 sec tak) ya UPI SMS paste karein — Munim khud samajh lega."
)
STAFF_WELCOME = ("Namaste! Aap is dukaan ke munshi/helper ke roop mein jud gaye. Voice ya text mein entry likhwa sakte hain, "
                 "hisaab dekh sakte hain. Undo, limit aur customer ko sandesh sirf maalik kar sakte hain.")
NOT_A_MEMBER = ("Yeh Munim hai — kirana dukaan ka awaaz-khata.\n• Dukaan shuru karne ke liye: /newshop Dukaan ka naam\n"
                "• Customer hain? Apni dukaan se invite link maangein.\n• Demo ke liye: /break")


def _markup(buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in r] for r in rows])


def gate(action: str | None):
    """Deny-by-default role check. Injects (tenant, member) into the wrapped handler."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            chat = update.effective_chat.id
            tm = self.reg.tenant_for(chat)
            if tm is None:
                await update.effective_message.reply_text(NOT_A_MEMBER)
                return
            t, m = tm
            if not can(m.role, action):
                await update.effective_message.reply_text(
                    "Yeh sirf dukaan ke maalik kar sakte hain." if m.role == STAFF else "Yeh aapke liye nahi hai.")
                return
            if m.role == STAFF:
                t.service.actors[chat] = m.label or "staff"
            return await fn(self, update, ctx, t, m)
        return wrapper
    return deco


class MunimBot:
    def __init__(self, cfg: Settings, registry: Registry, client: GnaniClient):
        self.cfg, self.reg, self.client = cfg, registry, client
        self._speakers: dict[str, Speaker] = {}
        self.caps = PublicCaps(kill_file="KILL_PUBLIC")
        self.llm = LLMClient(cfg.groq_key, model=cfg.llm_model)
        self.spans = LLMSpans(self.llm) if (cfg.llm_spans and self.llm.enabled) else None
        self.gen = GenReminder(self.llm)
        from munim import extras
        extras.SERVICES.llm = self.llm

    # ---- helpers ---------------------------------------------------------------------
    def _speaker(self, t: Tenant) -> Speaker:
        sp = self._speakers.get(t.shop_id)
        if sp is None:
            sp = self._speakers[t.shop_id] = Speaker(self.client)
        return sp

    def _planner(self):
        return LLMPlanner(self.llm) if self.llm.enabled else RulePlanner()

    def _find(self, t: Tenant, text: str) -> Customer | None:
        r = resolve(text, t.ledger.customers())
        return r.customer if r.status == "match" else None

    async def _say(self, msg, t: Tenant, text: str, language: str | None = None, speed: float = 1.0) -> None:
        if not self.reg.quota.allow(t.shop_id, 1):
            return                                           # out of Gnani quota today: degrade to text, never fail
        audio = await self._speaker(t).speak(text, speed=speed, language=language)
        if audio:
            self.reg.quota.charge(t.shop_id, 1)
            await msg.reply_voice(io.BytesIO(audio))

    async def _send_reply(self, msg, reply: Reply, chat: int, t: Tenant, speak: bool = True) -> None:
        if reply.action == "plan":
            await msg.reply_text(reply.text)
            reply = await t.service.plan(chat, self._planner())
        await msg.reply_text(reply.text, reply_markup=_markup(reply.buttons))
        if speak and reply.voice_text:
            await self._say(msg, t, reply.voice_text)
        for text, lang, speed in reply.voices:             # customer-facing voice notes to forward
            await self._say(msg, t, text, language=lang, speed=speed)

    async def _notify_owners(self, bot, t: Tenant, replies: list[Reply]) -> None:
        for chat in self.reg.chats(t.shop_id, OWNER):
            for r in replies:
                try:
                    await bot.send_message(chat, r.text, reply_markup=_markup(r.buttons))
                except Exception as exc:                       # an unreachable owner must never break a customer's request
                    log.warning("owner notify failed: %s", type(exc).__name__)

    # ---- onboarding ------------------------------------------------------------------
    async def start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat, msg = update.effective_chat.id, update.effective_message
        arg = (ctx.args or [""])[0]
        if arg.startswith(("c_", "s_")):
            await self._redeem(update, ctx, arg)
            return
        tm = self.reg.tenant_for(chat)
        if tm is None:
            await msg.reply_text(NOT_A_MEMBER)
        elif tm[1].role == OWNER:
            await msg.reply_text(WELCOME)
        elif tm[1].role == STAFF:
            await msg.reply_text(STAFF_WELCOME)
        else:
            lang = tm[0].ledger.customer_row(tm[1].customer_id)["lang"]
            await msg.reply_text(portal.welcome(tm[0].name, tm[0].ledger.customer_row(tm[1].customer_id)["name"], lang).text)

    async def _redeem(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, arg: str) -> None:
        chat, msg = update.effective_chat.id, update.effective_message
        kind, token = arg[0], arg[2:]
        label = f"staff-{int(time.time()) % 10000}" if kind == "s" else None
        m = self.reg.redeem_invite(token, chat, label)
        if m is None or (kind == "c") != (m.role == CUSTOMER):
            await msg.reply_text("Yeh invite link galat, purana ya pehle use ho chuka hai. Dukaan se naya link maangein.")
            return
        t = self.reg.tenant(m.shop_id)
        if m.role == CUSTOMER:
            t.ledger.grant_consent(m.customer_id, how="customer_self_linked")
            row = t.ledger.customer_row(m.customer_id)
            w = portal.welcome(t.name, row["name"], row["lang"] or "hi")
            await msg.reply_text(w.text)
            await self._say(msg, t, w.voice or "", language=w.tts_language)
            await self._notify_owners(ctx.bot, t, [Reply(f"🔗 {row['name']} Munim se jud gaye — unki sehmati unhone khud di (darj).")])
        else:
            await msg.reply_text(STAFF_WELCOME)
            await self._notify_owners(ctx.bot, t, [Reply(f"👤 Naya helper jud gaya: {m.label}.")])

    async def newshop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat, msg = update.effective_chat.id, update.effective_message
        if self.reg.membership(chat) is not None:
            await msg.reply_text("Aap pehle se ek dukaan se jude hain.")
            return
        if not self.cfg.open_signup or self.reg.shops_created_since(time.time() - 86400) >= self.cfg.max_new_shops_per_day:
            await msg.reply_text("Abhi naye dukaan nahi jud rahe. Thodi der baad koshish karein.")
            return
        name = " ".join(ctx.args or []).strip()
        if len(name) < 2:
            await msg.reply_text("Aise likhein: /newshop Gupta Kirana Store")
            return
        t = self.reg.create_shop(name, chat)
        await msg.reply_text(f"✅ “{t.name}” ban gayi. Ab customer jodein: /add Naam\n\n" + WELCOME)

    # ---- commands --------------------------------------------------------------------
    async def help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(HELP_HI)

    @gate("entry")
    async def add(self, update, ctx, t: Tenant, m: Member) -> None:
        name = " ".join(ctx.args or []).strip()
        if len(name) < 2:
            await update.effective_message.reply_text("Aise likhein: /add Ramesh Sharma")
            return
        c = t.ledger.add_customer(name)
        await update.effective_message.reply_text(
            f"✅ {c.name} khate mein joda gaya. Reminder ke liye: /invite {c.name} (customer khud sehmati dega) "
            f"ya /consent {c.name}")

    @gate("money_admin")
    async def consent(self, update, ctx, t, m) -> None:
        c = self._find(t, " ".join(ctx.args or []))
        if c is None:
            await update.effective_message.reply_text("Naam saaf nahi mila. /consent Poora Naam")
            return
        t.ledger.grant_consent(c.id)
        await update.effective_message.reply_text(
            f"✅ {c.name}: sehmati darj (aapne bataya ki unhone haan kaha). 'ruko' bolne par band ho jayega.")

    @gate("query")
    async def hisaab(self, update, ctx, t, m) -> None:
        rows = E.outstanding(t.ledger)[:15]
        total = sum(b for _, b in rows)
        body = "\n".join(f"{c.name}: {format_inr(b)}" for c, b in rows) or "Koi baaki nahi."
        ok, _, msg = t.ledger.verify()
        await update.effective_message.reply_text(f"{body}\n\nKul (top 15): {format_inr(total)}\n"
                                                  f"Khata jaanch: {'✅ ' if ok else '❌ '}{msg}")

    @gate("communicate")
    async def plan(self, update, ctx, t, m) -> None:
        chat = update.effective_chat.id
        await self._send_reply(update.effective_message, await t.service.plan(chat, self._planner()), chat, t)

    @gate("query")
    async def briefing(self, update, ctx, t, m) -> None:
        await self._send_briefing(update.effective_message, t)

    async def _send_briefing(self, msg, t: Tenant) -> None:
        rep = morning_briefing(t.ledger, t.service._clock(), t.name)
        await msg.reply_text(rep.text)
        await self._say(msg, t, rep.voice)

    @gate("query")
    async def ask(self, update, ctx, t, m) -> None:
        q = " ".join(ctx.args or []).strip()
        if len(q) < 3:
            await update.effective_message.reply_text("Aise puchhein: /ask Sharma ne last payment kab di?")
            return
        r = await qa_answer(t.ledger, t.service._clock(), q, self.llm if self.llm.enabled else None)
        await update.effective_message.reply_text(r.text)
        if r.voice:
            await self._say(update.effective_message, t, r.voice)

    @gate("communicate")
    async def remind(self, update, ctx, t, m) -> None:
        chat = update.effective_chat.id
        c = self._find(t, " ".join(ctx.args or []))
        if c is None:
            await update.effective_message.reply_text("Aise: /remind Sharma")
            return
        d, draft = draft_reminder(t.ledger, self.cfg, c.id, t.name, now=t.service._clock())
        if draft is None:                                   # Governor said no: explain, never bypass
            await update.effective_message.reply_text(f"Reminder nahi bhej sakta: {d.explain_hi()}.")
            return
        lang = t.ledger.customer_row(c.id)["lang"] or "hi"
        g = await self.gen.generate(draft, name=c.name.split()[0], shop=t.name, customer_lang=lang)
        rep = t.service.start_reminder_with(chat, c.id, g.draft.text, g.draft.voice_text, g.draft.tts_language, g.note)
        await self._send_reply(update.effective_message, rep, chat, t, speak=False)

    @gate("communicate")
    async def statement(self, update, ctx, t, m) -> None:
        chat = update.effective_chat.id
        args = list(ctx.args or [])
        asked = bool(args) and args[-1].lower() == "asked"
        c = self._find(t, " ".join(args[:-1] if asked else args))
        if c is None:
            await update.effective_message.reply_text("Aise: /statement Sharma   (customer ne khud maanga ho to: /statement Sharma asked)")
            return
        await self._send_reply(update.effective_message, t.service.statement(chat, c.id, asked), chat, t, speak=False)

    @gate("query")
    async def risk(self, update, ctx, t, m) -> None:
        now = t.service._clock()
        c = self._find(t, " ".join(ctx.args or [])) if ctx.args else None
        if c is None:
            rep = render("risk", t.ledger, now)
            await update.effective_message.reply_text(rep.text)
            await self._say(update.effective_message, t, rep.voice)
            return
        rk = E.risk(t.ledger, c.id, now)
        ag = E.aging(t.ledger, c.id, now)
        lines = [f"{c.name}: {rk.tier.upper()} (score {rk.score})"] + [f"• {x}" for _, x in rk.reasons]
        lines.append("Umr: " + ", ".join(f"{k}d {format_inr(v)}" for k, v in ag.items() if v))
        lines.append(f"Suggested limit: {format_inr(E.suggested_limit(t.ledger, c.id, now))}")
        await update.effective_message.reply_text("\n".join(lines))

    @gate("query")
    async def score(self, update, ctx, t, m) -> None:
        c = self._find(t, " ".join(ctx.args or []))
        if c is None:
            await update.effective_message.reply_text("Aise: /score Sharma")
            return
        model, src = self.reg.load_global_model(), "sabhi dukaanon ke saath seekha model (sirf weights saajha hote hain)"
        if model is None:
            X, y, _ = build_samples(t.ledger, weekly_cutoffs(t.ledger))
            if len(y) < 40:
                await update.effective_message.reply_text("Abhi itna purana data nahi ki model seekh sake (kam se kam ~6 hafte ka khata chahiye).")
                return
            model, src = CreditModel().fit(X, y), "sirf aapke khate se seekha model"
        r = score_customer(t.ledger, c.id, t.service._clock(), model)
        if r is None:
            await update.effective_message.reply_text(f"{c.name} ka abhi kuch baaki nahi.")
            return
        p, why = r
        lines = [f"{c.name}: agle 14 din mein kuch payment aane ki sambhavna ≈ {round(p * 100)}%  ({src}).",
                 "Mukhya kaaran: " + "; ".join(f"{NAMES_HI[k]} ({'badhata' if v > 0 else 'ghatata'})" for k, v in why[:3]),
                 "Yeh prayogik (experimental) andaza hai, guarantee nahi — abhi isse zyada bharosemand /forecast aur /risk hain. Udhaar dene ya na dene ka faisla aapka hai."]
        await update.effective_message.reply_text("\n".join(lines))

    @gate("money_admin")
    async def limit(self, update, ctx, t, m) -> None:
        text = " ".join(ctx.args or [])
        c, amt = self._find(t, text), parse_amount(text)
        if c is None or amt is None:
            await update.effective_message.reply_text("Aise: /limit Sharma 5000   (hatane ke liye: /limit Sharma 0)")
            return
        t.ledger.set_limit(c.id, amt or None)
        await update.effective_message.reply_text(f"✅ {c.name}: seema {format_inr(amt) if amt else 'hata di'}.")

    @gate("query")
    async def forecast_cmd(self, update, ctx, t, m) -> None:
        f = forecast(t.ledger, t.service._clock(), 14)
        await update.effective_message.reply_text(
            f"Agle 14 din mein vasooli ka andaza: {format_inr(f['expected_paise'])} "
            f"(sambhav: {format_inr(f['low_paise'])} se {format_inr(f['high_paise'])}).\n"
            "Yeh har customer ke apne payment-pattern se nikala gaya statistical andaza hai, guarantee nahi.")

    @gate("query")
    async def anomaly(self, update, ctx, t, m) -> None:
        an = E.anomalies(t.ledger, t.service._clock(), 30)[:10]
        names = {c.id: c.name for c in t.ledger.customers()}
        body = "\n".join(f"• #{a.seq} {names.get(a.customer_id, '?')}: {a.code} ({a.detail})" for a in an)
        await update.effective_message.reply_text(body or "Pichle 30 din mein kuch ajeeb nahi mila.")

    @gate("communicate")
    async def from_cmd(self, update, ctx, t, m) -> None:
        c = self._find(t, " ".join(ctx.args or []))
        if c is None:
            await update.effective_message.reply_text("Aise: /from Sharma — phir customer ka voice note/message forward karein.")
            return
        ctx.user_data["from_customer"] = c.id
        await update.effective_message.reply_text(f"Ab agla voice note/message {c.name} ka maana jayega.")

    @gate("admin")
    async def lang(self, update, ctx, t, m) -> None:
        args = ctx.args or []
        code = args[-1].lower() if args else ""
        c = self._find(t, " ".join(args[:-1]))
        if c is None or code not in ("hi", "en"):
            await update.effective_message.reply_text("Aise: /lang Sharma en   (hi ya en)")
            return
        t.ledger.set_flag(c.id, lang=code)
        await update.effective_message.reply_text(f"✅ {c.name} ke reminder ab {'English' if code == 'en' else 'Hindi'} mein.")

    async def voices(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text("Timbre v3.0 awaazein: " + ", ".join(ALL_VOICES) + "\nBadalne ke liye /voice Naam")

    @gate("admin")
    async def voice_cmd(self, update, ctx, t, m) -> None:
        name = (ctx.args or [""])[0].title()
        try:
            self._speaker(t).set_voice(name)
        except ValueError:
            await update.effective_message.reply_text("Yeh awaaz nahi hai. /voices dekhein.")
            return
        await update.effective_message.reply_text(f"✅ Awaaz: {name}. Namuna:")
        await self._say(update.effective_message, t, "Namaste, main Munim hoon. Aapka khata sambhalne ke liye taiyaar.")

    @gate("communicate")
    async def invite(self, update, ctx, t, m) -> None:
        c = self._find(t, " ".join(ctx.args or []))
        if c is None:
            await update.effective_message.reply_text("Aise: /invite Sharma — customer ko ek link milega; jaise hi woh dabayenge, sehmati unki khud ki darj hogi.")
            return
        tok = self.reg.make_invite(t.shop_id, CUSTOMER, c.id)
        await update.effective_message.reply_text(
            f"{c.name} ko yeh link bhejein (7 din, ek baar chalta hai):\nhttps://t.me/{ctx.bot.username}?start=c_{tok}")

    @gate("query")
    async def dispute(self, update, ctx, t, m) -> None:
        c = self._find(t, " ".join(ctx.args or []))
        if c is None:
            await update.effective_message.reply_text("Aise: /dispute Sharma — us customer ke khate ka poora saboot.")
            return
        text, _weak = dispute_pack.build(t.ledger, c.id, t.name, t.service._clock())
        await update.effective_message.reply_text(text[:3900])
        await update.effective_message.reply_document(io.BytesIO(dispute_pack.to_pdf(text)), filename=f"saboot_{c.id}.pdf")

    @gate("admin")
    async def setvpa(self, update, ctx, t, m) -> None:
        vpa = (ctx.args or [""])[0]
        if not valid_vpa(vpa):
            await update.effective_message.reply_text("Aise: /setvpa dukaan@upi  (aapka UPI id)")
            return
        self.reg.set_vpa(t.shop_id, vpa)
        await update.effective_message.reply_text(f"✅ UPI id darj: {vpa}. Ab /paylink Naam se QR bana sakte hain.")

    @gate("communicate")
    async def paylink(self, update, ctx, t, m) -> None:
        text = " ".join(ctx.args or [])
        c = self._find(t, text)
        if c is None or not t.vpa:
            await update.effective_message.reply_text("Aise: /paylink Sharma [raqam]  (pehle /setvpa se apna UPI id daalein)")
            return
        if t.ledger.customer_row(c.id)["erased"] or t.ledger.is_disputed(c.id):
            await update.effective_message.reply_text("Is khate par vivaad khula hai (ya data hata diya gaya) — pehle suljhayein, tab link banayein.")
            return
        bal = t.ledger.balance(c.id)
        want = parse_amount(text.replace(c.name, ""))
        amt = min(want, bal) if want else bal
        if amt <= 0:
            await update.effective_message.reply_text(f"{c.name} ka abhi kuch baaki nahi.")
            return
        uri = upi_uri(t.vpa, t.name, amt, "Khata")
        await update.effective_message.reply_photo(io.BytesIO(qr_png(uri)), caption=f"{c.name}: {format_inr(amt)} ka UPI QR. Payment aane par SMS yahan paste karein — Munim jama kar dega.")
        await update.effective_message.reply_text(uri)

    @gate("entry")
    async def dena(self, update, ctx, t, m) -> None:
        args = " ".join(ctx.args or [])
        if args.strip().lower() == "undo":
            e = t.payables.undo_last()
            await update.effective_message.reply_text("↩️ Pichli supplier entry wapas li." if e else "Wapas lene ko kuch nahi.")
            return
        p = payables.parse_dena(args)
        if p is None:
            await update.effective_message.reply_text("Aise: /dena Agarwal Traders 5000  (maal udhaar liya)\n/dena Agarwal Traders diya 2000  (payment kiya)\n/dena undo")
            return
        name, kind, paise = p
        await update.effective_message.reply_text(payables.record(t.payables, name, kind, paise, by=m.label))

    @gate("query")
    async def lena(self, update, ctx, t, m) -> None:
        await update.effective_message.reply_text(payables.position(t.ledger, t.payables))

    @gate("admin")
    async def import_cmd(self, update, ctx, t, m) -> None:
        ctx.user_data["import_mode"] = True
        await update.effective_message.reply_text("CSV/Excel-export file bhejein (columns: name, amount). Phone number aadi nahi liye jayenge. Kuch likhne se pehle poori list dikhaungi.")

    def _extra(self, name: str):
        from munim import extras

        @gate(extras.EXTRA_ACTIONS[name])
        async def handler(self, update, ctx, t, m) -> None:
            reply = await extras.run(self.reg, t, m, update.effective_chat.id, name, " ".join(ctx.args or []))
            await self._send_reply(update.effective_message, reply, update.effective_chat.id, t, speak=False)
        return functools.partial(handler, self)

    async def _extra_file(self, update, kind: str, data: bytes, mime: str = "image/jpeg") -> bool:
        from munim import extras
        chat, msg = update.effective_chat.id, update.effective_message
        tm = self.reg.tenant_for(chat)
        if tm is None or not can(tm[1].role, "admin"):
            return False
        t = tm[0]
        reply = (await extras.invoice_file(self.reg, t, chat, data, mime, self.cfg.allow_scan) if kind == "invoice"
                 else extras.aa_file(self.reg, t, chat, data))
        await self._send_reply(msg, reply, chat, t, speak=False)
        return True

    async def document(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat, msg = update.effective_chat.id, update.effective_message
        cap = (msg.caption or "").strip().lower()
        if msg.document and cap in ("invoice", "aa") and (msg.document.file_size or 0) <= 4_000_000:
            f = await msg.document.get_file()
            data = bytes(await f.download_as_bytearray())
            if await self._extra_file(update, "invoice" if cap == "invoice" else "aa", data, msg.document.mime_type or "application/pdf"):
                return
        tm = self.reg.tenant_for(chat)
        if tm is None or not can(tm[1].role, "admin") or not ctx.user_data.pop("import_mode", False):
            return
        t = tm[0]
        doc = msg.document
        if doc.file_size and doc.file_size > 300_000:
            await msg.reply_text("File bahut bada hai (max 300 KB).")
            return
        f = await doc.get_file()
        data = bytes(await f.download_as_bytearray())
        await self._send_reply(msg, t.service.stage_ingest(chat, parse_csv(t.ledger, data), "csv"), chat, t, speak=False)

    @gate("admin")
    async def scan_cmd(self, update, ctx, t, m) -> None:
        if not self.cfg.allow_scan or not self.llm.enabled:
            await update.effective_message.reply_text("Photo-scan band hai (isme photo AI seva ko jaati hai). Chalu karne ke liye MUNIM_SCAN=1 aur GROQ_API_KEY set karein, ya CSV /import use karein.")
            return
        ctx.user_data["scan_mode"] = True
        await update.effective_message.reply_text("Khata ke ek page ki saaf photo bhejein. ⚠️ Photo AI seva ko jaati hai; haath ke likhe number galat padhe ja sakte hain, isliye poori list dekhkar hi manzoor karein.")

    async def photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat, msg = update.effective_chat.id, update.effective_message
        if (msg.caption or "").strip().lower() == "invoice":
            f = await msg.photo[-1].get_file()
            if await self._extra_file(update, "invoice", bytes(await f.download_as_bytearray())):
                return
        tm = self.reg.tenant_for(chat)
        if tm is None or not can(tm[1].role, "admin") or not ctx.user_data.pop("scan_mode", False):
            return
        t = tm[0]
        f = await msg.photo[-1].get_file()
        data = bytes(await f.download_as_bytearray())
        plan = await scan_page(t.ledger, self.llm, data)
        await self._send_reply(msg, t.service.stage_ingest(chat, plan, "scan"), chat, t, speak=False)

    @gate("admin")
    async def export_cmd(self, update, ctx, t, m) -> None:
        data = ops_export.export_zip(t.ledger, t.name, t.service._clock())
        ok, msg = ops_export.verify_export(data)
        await update.effective_message.reply_document(io.BytesIO(data), filename=f"munim_backup_{t.shop_id}.zip")
        await update.effective_message.reply_text(
            f"Backup taiyaar ({'✅' if ok else '❌'} {msg}). Isme customers ke naam hain — surakshit rakhein. "
            "Koi bhi isey bina secret ke verify kar sakta hai.")

    @gate("admin")
    async def staff(self, update, ctx, t, m) -> None:
        args = ctx.args or []
        if args and args[0].lower() == "remove" and len(args) > 1 and args[1].lstrip("-").isdigit():
            self.reg.remove_member(int(args[1]))
            await update.effective_message.reply_text("Helper hata diya.")
            return
        tok = self.reg.make_invite(t.shop_id, STAFF)
        labels = self.reg.staff_labels(t.shop_id)
        listing = "\n".join(f"• {lbl} (chat {cid})" for cid, lbl in labels.items()) or "abhi koi nahi"
        await update.effective_message.reply_text(
            f"Helper ka invite link (7 din, ek baar):\nhttps://t.me/{ctx.bot.username}?start=s_{tok}\n\nAbhi ke helper:\n{listing}\n"
            "Hatane ke liye: /staff remove <chat id>")

    @gate("admin")
    async def stats(self, update, ctx, t, m) -> None:
        q = self.reg.quota.snapshot()
        stats, flags = audit_staff(t.ledger)
        lines = [f"Gnani calls aaj: {q['shops'].get(t.shop_id, 0)} / {q['per_shop_cap']} (sabhi dukaan: {q['global_used']}/{q['global_cap']})",
                 f"Customers: {len(t.ledger.customers())} • Entries: {len(t.ledger.entries())}"]
        for who, s in stats.items():
            lines.append(f"• {who}: {s['entries']} entry, {s['reversed']} wapas li, {s['after_hours']} raat ki")
        lines += [f"⚠️ {f.label}: {f.detail} — sirf ek sawaal, ilzaam nahi" for f in flags]
        await update.effective_message.reply_text("\n".join(lines))

    # ---- owner / staff messages ------------------------------------------------------
    async def text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat, msg = update.effective_chat.id, update.effective_message
        tm = self.reg.tenant_for(chat)
        if tm is None:
            await msg.reply_text(NOT_A_MEMBER)
            return
        t, m = tm
        if m.role == CUSTOMER:
            await self._portal(update, ctx, t, m, msg.text or "", None)
            return
        if m.role == STAFF:
            t.service.actors[chat] = m.label or "staff"
        cid = ctx.user_data.pop("from_customer", None) if can(m.role, COMMUNICATE) else None
        reply = (t.service.on_customer_message(chat, cid, msg.text or "") if cid else t.service.on_typed(chat, msg.text or ""))
        await self._send_reply(msg, reply, chat, t)

    async def _portal(self, update, ctx, t: Tenant, m: Member, text: str, detected_lang: str | None) -> None:
        msg = update.effective_message
        owners = self.reg.chats(t.shop_id, OWNER)
        res = portal.handle(t, m.customer_id, text, owner_chat=owners[0] if owners else None, detected_lang=detected_lang)
        await msg.reply_text(res.text)
        if res.voice:
            await self._say(msg, t, res.voice, language=res.tts_language)
        await self._notify_owners(ctx.bot, t, res.owner_msgs)

    async def voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat, msg = update.effective_chat.id, update.effective_message
        tm = self.reg.tenant_for(chat)
        public = ctx.user_data.get("break_mode", False) and tm is None
        if tm is None and not public:
            await msg.reply_text(NOT_A_MEMBER)
            return
        if public:
            ctx.user_data["break_mode"] = False
            t, m = None, None
        else:
            t, m = tm
            if not self.reg.quota.allow(t.shop_id, 3):
                await msg.reply_text("Aaj ka awaaz-seva quota poora ho gaya. Likh kar bhejein — khata chalta rahega.")
                return
        f = await (msg.voice or msg.audio).get_file()
        audio = bytes(await f.download_as_bytearray())
        if public:
            await self._voice_public(msg, audio)
            return
        if m.role == CUSTOMER:
            await self._voice_customer(update, ctx, t, m, audio)
            return
        if m.role == STAFF:
            t.service.actors[chat] = m.label or "staff"
        customers = t.ledger.customers()
        prev = ctx.user_data.get("prev")
        prev_text = prev[0] if prev and time.time() - prev[1] < 120 else None
        try:
            try:
                heard = await listen(self.client, audio, customers, previous_text=prev_text)
            except AudioTooLong:
                await msg.reply_text("Lamba voice note hai — tukdon mein sun raha hoon…")
                lh = await listen_long(self.client, audio, customers)
                self.reg.quota.charge(t.shop_id, lh.calls)
                ctx.user_data["prev"] = (lh.a_text[-300:], time.time())
                await self._send_reply(msg, t.service.on_heard(
                    chat, lh.a_text, lh.b_text, snr=lh.snr_db, dur=lh.seconds, lang=lh.language), chat, t)
                return
        except AudioTooLong:
            await msg.reply_text("Voice note 90 second se chhota rakhein, phir bhejein.")
            return
        except AudioDecodeError:
            await msg.reply_text("Awaaz padh nahi paaya. Dobara bhejein.")
            return
        except GnaniCreditsExhausted:
            await msg.reply_text("Awaaz seva ke credit khatam. Abhi likh kar bhejein — khata chalta rahega.")
            return
        except GnaniAuthError:
            log.error("Gnani auth failed — check GNANI_API_KEY_ID")
            await msg.reply_text("Awaaz seva se judne mein dikkat. Abhi likh kar bhejein.")
            return
        except GnaniError as exc:
            log.warning("STT failed: %s", type(exc).__name__)
            await msg.reply_text("Awaaz seva abhi busy hai. Thodi der baad ya likh kar bhejein.")
            return
        self.reg.quota.charge(t.shop_id, heard.calls)
        a, b, c = heard.a, heard.b, heard.c
        ctx.user_data["prev"] = (a.transcript[-300:], time.time())
        cid = ctx.user_data.pop("from_customer", None) if can(m.role, COMMUNICATE) else None
        if cid:
            await self._send_reply(msg, t.service.on_customer_message(
                chat, cid, a.transcript, snr=heard.prepared.snr_db, dur=heard.prepared.seconds), chat, t)
            return
        llm_ex = await self.spans.propose(a.transcript, customers) if self.spans else None
        reply = t.service.on_heard(
            chat, a.transcript, b.transcript if b else None, snr=heard.prepared.snr_db,
            dur=heard.prepared.seconds, lang=a.language, req_ids=(a.request_id, b.request_id if b else None),
            llm=llm_ex, c_text=c.transcript if c else None, d_text=heard.d.transcript if heard.d else None)
        await self._send_reply(msg, reply, chat, t)

    async def _voice_customer(self, update, ctx, t: Tenant, m: Member, audio: bytes) -> None:
        """Customer voice self-service: ONE Prisma pass (read-only/forwarded actions need no second ear)."""
        msg = update.effective_message
        try:
            prep = await prepare(audio)
            a = await pass_a_prisma_v3(self.client, prep.wav, keyterms=[t.ledger.customer_row(m.customer_id)["name"]])
        except (AudioTooLong, AudioDecodeError):
            await msg.reply_text("Voice note 30 second se chhota aur saaf rakhein.")
            return
        except GnaniError:
            await msg.reply_text("Awaaz seva abhi busy hai. Likh kar bhejein.")
            return
        self.reg.quota.charge(t.shop_id, 1)
        await self._portal(update, ctx, t, m, a.transcript, a.language)

    async def _voice_public(self, msg, audio: bytes) -> None:
        customers = [Customer(i + 1, n) for i, n in enumerate(SANDBOX_NAMES)]
        try:
            heard = await listen(self.client, audio, customers)
        except AudioTooLong:
            await msg.reply_text("Public demo mein voice note 30 second se chhota rakhein.")
            return
        except (AudioDecodeError, GnaniError):
            await msg.reply_text("Awaaz seva abhi uplabdh nahi. Thodi der baad koshish karein.")
            return
        a, b, c = heard.a, heard.b, heard.c
        await msg.reply_text(trace(a.transcript, b.transcript if b else None, heard.prepared.snr_db,
                                   heard.prepared.seconds, self.cfg)
                             + (f"\nPass C (3rd ear, only on disagreement): “{c.transcript}”" if c else "")
                             + (f"\nLanguage re-listen: {heard.retried_lang}" if heard.retried_lang else ""))

    async def callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        chat = update.effective_chat.id
        tm = self.reg.tenant_for(chat)
        if tm is None:
            return
        t, m = tm
        data = q.data or ""
        need = UNDO if data in ("undo", "undo_batch") else (COMMUNICATE if data.startswith("appr:") or data == "approve_reminder" else ENTRY)
        if not can(m.role, need):
            await q.message.reply_text("Yeh sirf dukaan ke maalik kar sakte hain.")
            return
        if m.role == STAFF:
            t.service.actors[chat] = m.label or "staff"
        await self._send_reply(q.message, t.service.on_callback(chat, data), chat, t)

    # ---- proactive daily job: briefing + agent plan for EVERY shop (still needs approval to send anything) ----
    async def daily(self, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        class _M:                       # tiny adapter so the same helpers work for push messages
            def __init__(s, bot, chat):
                s.bot, s.chat = bot, chat

            async def reply_text(s, text, **kw):
                await s.bot.send_message(s.chat, text, **kw)

            async def reply_voice(s, voice):
                await s.bot.send_voice(s.chat, voice)

        for shop_id, chat in self.reg.owner_chats():
            try:
                t = self.reg.tenant(shop_id)
                mm = _M(ctx.bot, chat)
                await self._send_briefing(mm, t)
                await self._send_reply(mm, await t.service.plan(chat, self._planner()), chat, t)
            except Exception as exc:                          # one broken shop must not stop the others
                log.warning("daily job failed for %s: %s", shop_id, type(exc).__name__)
        try:
            from munim import extras
            for chat, reply in await extras.daily(self.reg):
                t = self.reg.tenant_for(chat)[0]
                await self._send_reply(_M(ctx.bot, chat), reply, chat, t, speak=False)
        except Exception as exc:
            log.warning("v4 daily failed: %s", type(exc).__name__)

    # ---- public 'Break Munim' --------------------------------------------------------
    async def break_cmd(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        uid = update.effective_user.id
        ok, why = self.caps.check(uid)
        if not ok:
            msg = {"user_cap": "Aaj ke 3 try ho chuke. Kal phir aayein!",
                   "global_cap": "Aaj ka public quota poora ho gaya. Kal aayein!",
                   "killed": "Public demo abhi band hai."}[why]
            await update.effective_message.reply_text(msg)
            return
        ctx.user_data["break_mode"] = True
        await update.effective_message.reply_text(
            f"Sandbox mein koi bhi voice note bhejein (shor, mix bhasha, dedh/dhai/sava — jo marzi). "
            f"Main dikhaunga ki kya suna aur kyun likha ya roka. Bacha hua quota: {self.caps.remaining(uid)}.\n"
            "Apna phone number / Aadhaar mat bolein. (Demo sirf unke liye hai jo kisi dukaan se jude nahi.)")
        self.caps.consume(uid)


def build_app(cfg: Settings, registry: Registry, client: GnaniClient) -> Application:
    bot = MunimBot(cfg, registry, client)
    import os
    b = (Application.builder().token(cfg.telegram_token)
         .connect_timeout(20).read_timeout(30).write_timeout(30).pool_timeout(20)
         .get_updates_connect_timeout(20).get_updates_read_timeout(40).get_updates_write_timeout(30).get_updates_pool_timeout(20))
    proxy = os.environ.get("MUNIM_TELEGRAM_PROXY", "").strip()
    if proxy:
        b = b.proxy(proxy).get_updates_proxy(proxy)
    app = b.build()
    for name, fn in [("start", bot.start), ("help", bot.help), ("newshop", bot.newshop), ("add", bot.add),
                     ("consent", bot.consent), ("hisaab", bot.hisaab), ("ask", bot.ask), ("statement", bot.statement), ("remind", bot.remind),
                     ("plan", bot.plan), ("briefing", bot.briefing), ("risk", bot.risk), ("score", bot.score), ("dispute", bot.dispute), ("setvpa", bot.setvpa), ("paylink", bot.paylink),
                     ("dena", bot.dena), ("lena", bot.lena), ("import", bot.import_cmd), ("scan", bot.scan_cmd), ("export", bot.export_cmd), ("limit", bot.limit),
                     ("forecast", bot.forecast_cmd), ("anomaly", bot.anomaly), ("from", bot.from_cmd),
                     ("lang", bot.lang), ("voices", bot.voices), ("voice", bot.voice_cmd), ("invite", bot.invite),
                     ("staff", bot.staff), ("stats", bot.stats), ("break", bot.break_cmd)]:
        app.add_handler(CommandHandler(name, fn))
    from munim import extras
    for name in extras.EXTRA_COMMANDS:
        app.add_handler(CommandHandler(name, bot._extra(name)))
    app.add_handler(CallbackQueryHandler(bot.callback))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, bot.voice))
    app.add_handler(MessageHandler(filters.Document.ALL, bot.document))
    app.add_handler(MessageHandler(filters.PHOTO, bot.photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.text))
    if app.job_queue is not None:
        app.job_queue.run_daily(bot.daily, time=dtime(cfg.briefing_hour, cfg.briefing_minute, tzinfo=IST), name="daily")
    app.bot_data["munim"] = bot
    return app
