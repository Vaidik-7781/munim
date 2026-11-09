"""Offline smoke tests for the Telegram transport (no network: handlers get fake updates)."""
from types import SimpleNamespace

import pytest

from munim.bot.telegram_bot import MunimBot, _markup, build_app
from munim.config import Settings
from munim.gnani.client import GnaniClient
from munim.tenancy.registry import Registry
from munim.tenancy.roles import CUSTOMER, STAFF


class FakeMsg:
    def __init__(self, text=""):
        self.text, self.sent, self.voices = text, [], []

    async def reply_text(self, t, **kw):
        self.sent.append(t)

    async def reply_voice(self, v):
        self.voices.append(v)

    async def reply_photo(self, p, caption=""):
        self.sent.append("PHOTO:" + caption)

    async def reply_document(self, d, filename=""):
        self.sent.append("DOC:" + filename)


class FakeBot:
    username = "munim_test_bot"

    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append((chat, text))

    async def send_voice(self, chat, v):
        self.sent.append((chat, "voice"))


def upd(msg, chat=1):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat), effective_message=msg,
                           effective_user=SimpleNamespace(id=chat))


def ctx(*args, bot=None):
    return SimpleNamespace(args=list(args), user_data={}, bot=bot or FakeBot())


@pytest.fixture
def bot(tmp_path):
    cfg = Settings(telegram_token="123:abc", gnani_key="k")
    reg = Registry(str(tmp_path), "s", cfg)
    t = reg.create_shop("Test Store", 1)
    t.ledger.add_customer("Ramesh Sharma")
    t.ledger.add_customer("Sunita Verma")
    b = MunimBot(cfg, reg, GnaniClient("k", "http://invalid.local"))

    async def no_tts(*a, **k):
        return None
    b._speaker(t).speak = no_tts
    b.t = t
    return b


def test_markup_two_per_row():
    m = _markup([("a", "1"), ("b", "2"), ("c", "3")])
    assert [len(r) for r in m.inline_keyboard] == [2, 1]
    assert _markup([]) is None


def test_build_app_registers_handlers(tmp_path):
    cfg = Settings(telegram_token="123:abc", gnani_key="k")
    app = build_app(cfg, Registry(str(tmp_path), "s", cfg), GnaniClient("k", "http://invalid.local"))
    cmds = {c for hs in app.handlers.values() for h in hs for c in getattr(h, "commands", [])}
    assert {"plan", "briefing", "risk", "limit", "from", "lang", "voice", "voices", "forecast", "anomaly",
            "newshop", "invite", "staff", "stats", "ask", "statement"} <= cmds


async def test_text_entry_and_confirm_flow(bot):
    m = FakeMsg("Ramesh Sharma ko 150 rupaye udhaar likh do")
    await bot.text(upd(m), ctx())
    assert m.sent


async def test_stranger_gets_onboarding_not_data(bot):
    m = FakeMsg("hello")
    await bot.text(upd(m, chat=99), ctx())
    assert "/newshop" in m.sent[0]
    m = FakeMsg()
    await bot.hisaab(upd(m, chat=99), ctx())
    assert "/newshop" in m.sent[0]


async def test_newshop_signup_and_cap(bot):
    m = FakeMsg()
    await bot.newshop(upd(m, chat=50), ctx("Naya", "Kirana"))
    assert "ban gayi" in m.sent[0] and bot.reg.tenant_for(50)[0].name == "Naya Kirana"
    m = FakeMsg()
    await bot.newshop(upd(m, chat=50), ctx("Dusra"))
    assert "pehle se" in m.sent[0]
    bot.cfg = Settings(open_signup=False)
    m = FakeMsg()
    await bot.newshop(upd(m, chat=51), ctx("Band Store"))
    assert "nahi jud rahe" in m.sent[0]


async def test_commands_run(bot):
    for fn, args in [(bot.risk, ()), (bot.risk, ("Ramesh",)), (bot.forecast_cmd, ()), (bot.anomaly, ()),
                     (bot.hisaab, ()), (bot.briefing, ()), (bot.plan, ()), (bot.stats, ())]:
        m = FakeMsg()
        await fn(upd(m), ctx(*args))
        assert m.sent, fn.__name__
    m = FakeMsg()
    await bot.voices(upd(m), ctx())
    assert m.sent


async def test_limit_lang_from_voice(bot):
    m = FakeMsg()
    await bot.limit(upd(m), ctx("Ramesh", "5000"))
    assert "seema" in m.sent[0]
    m = FakeMsg()
    await bot.lang(upd(m), ctx("Ramesh", "en"))
    assert "English" in m.sent[0]
    m = FakeMsg()
    c = ctx("Sunita")
    await bot.from_cmd(upd(m), c)
    assert "from_customer" in c.user_data
    m = FakeMsg("ruko")
    await bot.text(upd(m), c)
    assert m.sent and "from_customer" not in c.user_data
    m = FakeMsg()
    await bot.voice_cmd(upd(m), ctx("Nobody"))
    assert "nahi" in m.sent[0]
    await bot.voice_cmd(upd(m), ctx("avinash"))
    assert bot._speaker(bot.t).voice == "Avinash"


async def test_ask_statement_upi_and_batch_via_bot(bot):
    L = bot.t.ledger
    m = FakeMsg()
    await bot.ask(upd(m), ctx("Ramesh", "ki", "history", "dikhao"))
    assert m.sent
    L.add_udhaar(1, 50000)
    m = FakeMsg()
    await bot.ask(upd(m), ctx("Ramesh", "ka", "kitna", "baaki"))
    assert "₹500" in m.sent[0]
    m = FakeMsg()
    await bot.statement(upd(m), ctx("Ramesh"))
    assert "Statement nahi" in m.sent[0]
    L.grant_consent(1)
    m = FakeMsg()
    await bot.statement(upd(m), ctx("Ramesh"))
    assert "Customer ko bhejne" in m.sent[0]
    m = FakeMsg("Received ₹200 from Ramesh Sharma. UPI Ref No 412345678901")
    await bot.text(upd(m), ctx())
    assert m.sent and "UPI" in m.sent[0]
    m = FakeMsg("Ramesh ne 100 diye, Sunita ne 50 diye")
    await bot.text(upd(m), ctx())
    assert "entry taiyaar" in m.sent[0]


async def test_staff_flow_roles_and_tagging(bot):
    owner_ctx = ctx("x")
    m = FakeMsg()
    await bot.staff(upd(m), owner_ctx)
    tok = m.sent[0].split("start=s_")[1].split()[0]
    m = FakeMsg()
    await bot.start(upd(m, chat=7), ctx(f"s_{tok}"))
    assert "helper" in m.sent[0].lower() and bot.reg.membership(7).role == STAFF
    # staff can record
    m = FakeMsg("Ramesh ko 100 udhaar likh do")
    await bot.text(upd(m, chat=7), ctx())
    assert m.sent
    assert any(e.meta.get("by") for e in bot.t.ledger.entries() if e.kind == "add_udhaar") or bot.t.service.pending.get(7)
    # staff cannot do owner-only things
    for fn, args in [(bot.limit, ("Ramesh", "5")), (bot.consent, ("Ramesh",)), (bot.plan, ()), (bot.invite, ("Ramesh",)),
                     (bot.staff, ()), (bot.stats, ())]:
        m = FakeMsg()
        await fn(upd(m, chat=7), ctx(*args))
        assert "maalik" in m.sent[0], fn.__name__
    # staff cannot undo via button
    q = SimpleNamespace(data="undo", answer=lambda: _noop(), message=FakeMsg())
    u = SimpleNamespace(callback_query=q, effective_chat=SimpleNamespace(id=7), effective_message=q.message)
    await bot.callback(u, ctx())
    assert "maalik" in q.message.sent[0]


async def _noop():
    return None


async def test_customer_invite_self_consent_and_portal(bot):
    L = bot.t.ledger
    L.add_udhaar(1, 45000)
    fb = FakeBot()
    m = FakeMsg()
    await bot.invite(upd(m), ctx("Ramesh", bot=fb))
    assert "t.me/munim_test_bot?start=c_" in m.sent[0]
    tok = m.sent[0].split("start=c_")[1].split()[0]
    m = FakeMsg()
    await bot.start(upd(m, chat=60), ctx(f"c_{tok}", bot=fb))
    assert bot.reg.membership(60).role == CUSTOMER
    assert L.customer_row(1)["consent"] == "granted"
    assert any(chat == 1 and "khud di" in txt for chat, txt in fb.sent)          # owner told
    # reused link refused
    m = FakeMsg()
    await bot.start(upd(m, chat=61), ctx(f"c_{tok}", bot=fb))
    assert "galat" in m.sent[0] and bot.reg.membership(61) is None
    # portal: balance, stop (owner notified)
    m = FakeMsg("mera kitna baaki hai")
    await bot.text(upd(m, chat=60), ctx(bot=fb))
    assert "₹450" in m.sent[0]
    m = FakeMsg("ruko")
    await bot.text(upd(m, chat=60), ctx(bot=fb))
    assert L.customer_row(1)["consent"] == "revoked"
    assert any("khud sandesh band" in txt or "band karne" in txt for chat, txt in fb.sent if chat == 1)
    # customer cannot run owner commands
    m = FakeMsg()
    await bot.hisaab(upd(m, chat=60), ctx())
    assert "aapke liye nahi" in m.sent[0]


async def test_daily_job_covers_every_shop(bot):
    bot.reg.create_shop("Second Shop", 2)
    fb = FakeBot()
    await bot.daily(SimpleNamespace(bot=fb))
    chats = {c for c, _ in fb.sent}
    assert {1, 2} <= chats


async def test_score_command_local_and_global(bot, tmp_path):
    import sys
    sys.path.insert(0, ".")
    from data.seed import seed
    m = FakeMsg()
    await bot.score(upd(m), ctx("Ramesh"))
    assert "kam se kam" in m.sent[0]                                # not enough history yet: says so, no fake number
    L = seed(str(tmp_path / "seed.db"), "x", 90, 2610)
    bot.t.ledger = L                                                # swap in a ledger with history
    rich = [c for c in L.customers() if L.balance(c.id) > 0][0]
    m = FakeMsg()
    await bot.score(upd(m), ctx(rich.name))
    assert "%" in m.sent[0] and "guarantee nahi" in m.sent[0]


async def test_remind_command_governor_and_generation(bot):
    import json as _json
    import httpx
    from munim.llm.client import LLMClient
    from munim.govern.genreminder import GenReminder
    m = FakeMsg()
    await bot.remind(upd(m), ctx("Ramesh"))
    assert "Reminder nahi bhej sakta" in m.sent[0]          # no balance / no consent: Governor refuses, no LLM involved
    assert bot.t.service.pending.get(1) is None


async def test_dispute_paylink_dena_lena_export_commands(bot):
    L = bot.t.ledger
    L.add_udhaar(1, 50000, src="voice")
    m = FakeMsg()
    await bot.dispute(upd(m), ctx("Ramesh"))
    assert any("khata saboot" in x for x in m.sent) and any(x.startswith("DOC:") for x in m.sent)
    m = FakeMsg()
    await bot.paylink(upd(m), ctx("Ramesh"))
    assert "setvpa" in m.sent[0]                                   # no UPI id yet
    m = FakeMsg()
    await bot.setvpa(upd(m), ctx("bad id"))
    assert "Aise" in m.sent[0]
    m = FakeMsg()
    await bot.setvpa(upd(m), ctx("shop@okaxis"))
    assert "darj" in m.sent[0]
    m = FakeMsg()
    await bot.paylink(upd(m), ctx("Ramesh", "200"))
    assert m.sent[0].startswith("PHOTO:") and "₹200" in m.sent[0] and "am=200.00" in m.sent[1]
    L.open_dispute(1, None, "x")
    m = FakeMsg()
    await bot.paylink(upd(m), ctx("Ramesh"))
    assert "vivaad" in m.sent[0]                                    # no pay link while the account is disputed
    m = FakeMsg()
    await bot.dena(upd(m), ctx("Agarwal", "Traders", "5000"))
    assert "naya supplier" in m.sent[0]
    m = FakeMsg()
    await bot.lena(upd(m), ctx())
    assert "Lena" in m.sent[0] and "Dena" in m.sent[0]
    m = FakeMsg()
    await bot.dena(upd(m), ctx("undo"))
    assert "wapas" in m.sent[0]
    m = FakeMsg()
    await bot.export_cmd(upd(m), ctx())
    assert any(x.startswith("DOC:munim_backup") for x in m.sent) and any("Backup taiyaar" in x for x in m.sent)


async def test_import_and_scan_gates(bot):
    m = FakeMsg()
    await bot.scan_cmd(upd(m), ctx())
    assert "band hai" in m.sent[0]                                  # opt-in only
    c = ctx()
    m = FakeMsg()
    await bot.import_cmd(upd(m), c)
    assert c.user_data["import_mode"]
    # staff cannot import or scan
    bot.reg.add_member(bot.t.shop_id, 8, "staff", None, "munshi-9")
    for fn in (bot.import_cmd, bot.scan_cmd, bot.export_cmd, bot.setvpa):
        m = FakeMsg()
        await fn(upd(m, chat=8), ctx())
        assert "maalik" in m.sent[0], fn.__name__
