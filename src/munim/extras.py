"""Command layer for the v4 features (inventory/ONDC, mandates, insurance, pool, lending, policy, devices, reconcile).

Transport-agnostic: Telegram, WhatsApp and the REST API call `run()` / `callback()` / `daily()`. All external systems are adapters
in `Services` and default to SANDBOX implementations — nothing here talks to ONDC, an insurer, a lender or an e-rupee rail.
Role checks happen in the transport (COMMAND_ACTION); callbacks re-check that the chat is the shop owner (API/WhatsApp principals
act as their linked role).
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

from munim.agent import negotiator, reconcile
from munim.insurance import micro as INS
from munim.inventory import distributor_ocr as OCR
from munim.inventory.stock import Inventory, fmt_qty, norm_name
from munim.inventory.voice import parse_price, parse_qty
from munim.lending import aa
from munim.lending import ocen_gateway as LEND
from munim.ondc import gateway as ONDC
from munim.passport import cashflow as PF
from munim.payments import mandate as MND
from munim.scribe.indic_numbers import format_inr, parse_amount
from munim.scribe.names import resolve
from munim.service import Reply

EXTRA_COMMANDS = ("stock", "price", "count", "ondc", "pincode", "mandate", "insure", "pool", "passport", "loan", "policy", "recon",
                  "edge", "smsdev", "invoice", "aa")
EXTRA_ACTIONS = {"stock": "query", "count": "entry", "price": "admin", "ondc": "admin", "pincode": "admin", "mandate": "money_admin",
                 "insure": "admin", "pool": "admin", "passport": "query", "loan": "admin", "policy": "admin", "recon": "money_admin",
                 "edge": "admin", "smsdev": "admin", "invoice": "admin", "aa": "admin"}
_PENDING: dict[tuple[str, int], tuple[object, float]] = {}


@dataclass
class Services:
    sink: ONDC.Sink = field(default_factory=ONDC.SandboxSink)
    insurer: INS.Insurer = field(default_factory=INS.SandboxInsurer)
    rail: MND.Rail = field(default_factory=MND.SandboxRail)
    lender: LEND.Lender = field(default_factory=LEND.SandboxLender)
    llm: object | None = None
    pool: object | None = None
    edge_devices: object | None = None
    sms_devices: object | None = None
    wa_link_code: object | None = None


SERVICES = Services()


def _pool(reg):
    if SERVICES.pool is None:
        from munim.consortium.pool import Pool
        SERVICES.pool = Pool(reg)
    return SERVICES.pool


def _owner(reg, chat: int) -> bool:
    m = reg.membership(chat)
    return (m is not None and m.role == "owner") or chat >= 8_000_000_000_000


def _cust(t, text: str):
    r = resolve(text, t.ledger.customers())
    return r.customer if r.status == "match" else None


def _pool_admins() -> set[int]:
    return {int(x) for x in os.environ.get("MUNIM_POOL_ADMINS", "").split(",") if x.strip().isdigit()}


# ------------------------------------------------------------------------------------------------ commands
async def run(reg, t, m, chat: int, name: str, args: str) -> Reply:
    fn = _CMDS.get(name)
    if fn is None:
        return Reply("Yeh command nahi pehchana.")
    try:
        r = fn(reg, t, chat, args.strip())
        return await r if hasattr(r, "__await__") else r
    except (ValueError, MND.MandateError, PermissionError) as exc:
        return Reply(f"⚠️ {exc}")


def c_stock(reg, t, chat, a):
    inv = Inventory(t.aux, t._clock)
    skus = inv.skus()
    if not skus:
        return Reply("Abhi koi item nahi. Bolein: “stocked 50 packets of Maggi”.")
    low = {s.id for s in inv.low_stock()}
    if a == "low":
        skus = [s for s in skus if s.id in low]
    lines = [f"{'🔔' if s.id in low else '•'} {s.name}: {fmt_qty(s.stock, s.dim)}"
             + (f" @ {format_inr(s.price_paise)}" if s.price_paise else " (daam nahi)") + f" [{inv.confidence(s)}]" for s in skus[:40]]
    return Reply("📦 Stock\n" + "\n".join(lines))


def c_price(reg, t, chat, a):
    mrp = re.search(r"\bmrp\s+(\d+(?:\.\d{1,2})?)", a, re.I)
    mk = re.search(r"\bby\s+(.+)$", a, re.I)
    core = re.sub(r"\bmrp\s+\S+|\bby\s+.+$", "", a, flags=re.I).strip()
    pp = parse_price(core)
    if pp is None:
        return Reply("Aise: /price maggi 14 rupaye packet mrp 14 by Nestle   (ya: /price atta 55 per kilo mrp 58 by Aashirvaad)")
    item, paise, per = pp
    inv = Inventory(t.aux, t._clock)
    m = inv.find(item)
    if m.status != "match":
        return Reply("Pehle item stock mein jodein: “stocked 10 packets of …”.")
    s = m.sku
    inv.upsert(s.name, s.dim, price_paise=paise, price_per=per or s.price_per,
               mrp_paise=int(round(float(mrp.group(1)) * 100)) if mrp else None, manufacturer=mk.group(1).strip()[:50] if mk else None)
    return Reply(f"✅ {s.name}: daam {format_inr(paise)}" + (f", MRP {format_inr(int(round(float(mrp.group(1)) * 100)))}" if mrp else "") + ".")


def c_count(reg, t, chat, a):
    q = parse_qty(a)
    if q is None:
        return Reply("Aise: /count maggi 40 packet")
    qty, dim, unit, s, e = q
    inv = Inventory(t.aux, t._clock)
    m = inv.find((a[:s] + " " + a[e:]).strip())
    if m.status != "match" or m.sku.dim != dim:
        return Reply("Item ya unit match nahi hua.")
    inv.count(m.sku.id, qty)
    return Reply(f"🔢 {m.sku.name}: ginti {fmt_qty(qty, dim)} darj — ONDC par pakka maana jayega.")


def c_pincode(reg, t, chat, a):
    reg.set_pincode(t.shop_id, a)
    return Reply(f"✅ Pincode {a} darj.")


def c_ondc(reg, t, chat, a):
    pin = reg.pincode(t.shop_id)
    if a == "off":
        ONDC.disable(t)
        return Reply("⏹ ONDC par listing band. Pichla data network par rehne ke liye aapke Seller NP se hatwana hoga.")
    if a == "sync":
        return Reply(f"ONDC sync: {ONDC.sync(t, pin, SERVICES.sink)}")
    cat = ONDC.preview(t, pin)
    from munim.ondc.beckn import WHY_HI
    items = cat.payload["message"]["catalog"]["bpp/providers"][0]["items"]
    lines = [f"✔ {i['descriptor']['name']}: ₹{i['price']['value']} × {i['quantity']['available']['count']}" for i in items[:15]]
    lines += [f"✖ {s.name}: {', '.join(WHY_HI[w] for w in why)}" for s, why in cat.hidden[:15]]
    if a != "publish":
        return Reply("🛒 ONDC preview (abhi kuch publish nahi hua)\n" + ("\n".join(lines) or "Koi item nahi.") + "\n\n/ondc publish se jaari karein.")
    if not pin:
        return Reply("Pehle /pincode 800001 daalein.")
    return Reply("Yeh PUBLIC listing hogi:\n" + ("\n".join(lines) or "Koi item nahi.") + f"\n(Pincode {pin}). Ek baar haan kehne ke baad sirf badlaav bheje jayenge. "
                 "Dhyaan: asli network ke liye Seller NP, GST/FSSAI/bank KYC chahiye — abhi sandbox.", buttons=[("Publish ✅", "x:ondc:go"), ("Nahi ❌", "x:no")])


def c_mandate(reg, t, chat, a):
    eng = MND.Engine(t, SERVICES.rail)
    if a in ("", "list"):
        rows = t.aux.db.execute("SELECT * FROM mandates ORDER BY id DESC LIMIT 15").fetchall()
        cov = eng.coverage()
        return Reply("🧾 Mandates\n" + ("\n".join(f"• {t.ledger.customer_row(r['customer_id'])['name']}: {r['status']} {r['cap_pct']}% swept {format_inr(r['swept_paise'])}/{format_inr(r['total_cap_paise'])}" for r in rows) or "Koi nahi.")
                     + f"\nCapacity cover: {cov['covered_pct']}% — {cov['note']}")
    if a.startswith("revoke"):
        c = _cust(t, a[6:])
        return Reply(f"🛑 {eng.revoke_customer(c.id, 'owner')} mandate band." if c else "Customer nahi mila.")
    pct = re.search(r"(\d{1,2})\s*%", a)
    c = _cust(t, a)
    if c is None:
        return Reply("Aise: /mandate Sharma 25%   (customer pehle apne invite link se jud chuka ho)")
    dbt = bool(re.search(r"\bdbt\b", a, re.I))
    m = eng.propose(c.id, cap_pct=int(pct.group(1)) if pct else MND.DEFAULT_CAP_PCT, sources=["UPI_INFLOW", "WAGE"] + (["DBT"] if dbt else []))
    return Reply(f"📨 {c.name} ke chat mein prastaav dikhega (woh “mandate” likhein). Woh khud “mandate haan” kahenge tabhi chalu hoga; kabhi bhi band kar sakte hain. "
                 f"Limit: {m.cap_pct}% har credit ka, kul {format_inr(m.total_cap_paise)}. Yeh guarantee nahi hai.")


def c_insure(reg, t, chat, a):
    book = INS.Book(t, SERVICES.insurer)
    inv = Inventory(t.aux, t._clock)
    p = PF.build(t.ledger, t._clock())
    pol = t.aux.db.execute("SELECT * FROM policies ORDER BY id DESC LIMIT 1").fetchone()
    if a.startswith("per"):
        book.set_per_event(int(a.split()[1]))
        return Reply("✅ Har digital payment par jama hone wali rakam badli.")
    if a.startswith("pay") and pol:
        amt = parse_amount(a[3:]) or 0
        st = book.remit(pol["id"], amt)
        return Reply(f"✅ Insurer ko {format_inr(amt)} diya darj. Sthiti: {st}.\n" + book.status_text(pol["id"]))
    if a.startswith("claim") and pol:
        amt = parse_amount(a[5:]) or 0
        cid = book.claim_draft(pol["id"], a[5:], amt)
        return Reply(f"Claim draft #{cid} bana. Bhejne se pehle jaanch lein.", buttons=[("Bhejein ✅", f"x:claim:{cid}"), ("Nahi ❌", "x:no")])
    if pol and a == "":
        book.accrue(pol["id"])
        return Reply(book.status_text(pol["id"]))
    si = INS.suggest_sum_insured(INS.stock_value(inv))
    q = INS.indicate("STOCK_FIRE", p.tier, si)                                # raises (shown as ⚠️) when no tier yet
    plan = INS.funding_plan(q.annual_premium_paise, max(1, sum(1 for e in t.ledger.entries() if e.kind == "record_payment" and e.meta.get("src") in INS.QUALIFYING_SRC) // 3), book.per_event())
    txt = (f"🛡 Sandbox indication (insurer ka asli quote nahi): maal bima {format_inr(si)}, saalana premium ~{format_inr(q.annual_premium_paise)} (tier {p.tier}).\n"
           f"Har digital payment par {book.per_event()} paise jama karne se saal bhar mein ~{format_inr(plan['accrual_per_year_paise'])} banega — "
           + ("premium poora ho jayega." if plan["covers_premium"] else f"premium se {format_inr(plan['shortfall_paise'])} kam padega (chahiye ~{plan['per_event_needed_paise']} paise/payment).")
           + "\nCover tabhi chalu hoga jab insurer confirm kare; jama pot sirf bachat hai. Customer se kuch nahi kata jata.")
    return Reply(txt, buttons=[("Manzoor ✅", "x:ins:accept"), ("Nahi ❌", "x:no")])


def c_pool(reg, t, chat, a):
    pool = _pool(reg)
    pin = reg.pincode(t.shop_id)
    if not pin:
        return Reply("Pehle /pincode 800001 daalein.")
    parts = a.split()
    sh = t.shop_id
    if parts[:1] == ["join"] and len(parts) == 3:
        pool.join(int(parts[1]), sh, int(parts[2]))
        return Reply("✅ Shamil. Yeh sirf iraada hai; final daam dekhkar /pool confirm ID se pakka karein.")
    if parts[:1] == ["confirm"] and len(parts) == 2:
        po = pool.confirm(int(parts[1]), sh)
        return Reply(f"✅ Aapka order: {po['qty']} {po['dim']} {po['sku']} × {format_inr(po['price_paise'])} = {format_inr(po['total_paise'])} ({po['distributor']}). {po['note']}")
    if parts[:1] in (["rfq"], ["quote"], ["final"]):
        if chat not in _pool_admins():
            return Reply("Yeh sirf coordinator kar sakta hai.")
        if parts[0] == "rfq":
            rid = pool.open_rfq(pin, " ".join(parts[1:-3]), parts[-3], parts[-2], int(parts[-1]) * 100)
            return Reply(f"RFQ #{rid} khula (pincode {pin}). Format: /pool rfq <item> <pc|g|ml> <YYYY-MM-DD> <rate Rs>")
        if parts[0] == "quote":
            pool.add_quote(int(parts[1]), parts[2], int(float(parts[3]) * 100), int(parts[4]) if len(parts) > 4 else 0)
            ev = pool.evaluate(int(parts[1]))
            return Reply(f"Quotes: {ev.get('quotes')}\nTarget {ev.get('target_paise')} walk-away {ev.get('walk_away_paise')} (paise/unit)\n" + (pool.draft_counter(int(parts[1]), parts[2]) or ""))
        pool.finalize(int(parts[1]), parts[2], int(float(parts[3]) * 100))
        return Reply("Final daam darj; dukaanein /pool confirm se pakka karengi.")
    n = pool.contribute(sh)
    inv = Inventory(t.aux, t._clock)
    lines = []
    for s in inv.skus():
        f = pool.forecast(pin, s.key)
        if f["ok"]:
            sg = pool.suggest_for_shop(sh, s.key, s.stock, inv.weekly_velocity(s.id), f)
            lines.append(f"• {s.name}: agle hafte maang ~{f['predicted']} (aam {f['baseline']}){' ⚡surge' if f['surge'] else ''}; aapke liye ~{sg}")
    rf = pool.db.execute("SELECT * FROM rfq WHERE pin=? AND status IN ('open','final') ORDER BY id DESC LIMIT 5", (pin,)).fetchall()
    lines += [f"🧺 RFQ #{r['id']} {r['sku_key']} [{r['status']}] — /pool join {r['id']} <qty>" for r in rf]
    return Reply(f"🤝 Pincode {pin} pool (aapki {n} entries bheji). Sirf {5}+ dukaan hone par hi sankhya dikhti hai.\n" + ("\n".join(lines) or "Abhi itna data nahi."))


def c_passport(reg, t, chat, a):
    return Reply(PF.text(PF.build(t.ledger, t._clock())))


def c_loan(reg, t, chat, a):
    gw = LEND.Gateway(t, SERVICES.lender, reg._master)
    if a.startswith("take"):
        offs = t.aux.get("loan_offers") or []
        i = int(a.split()[1]) - 1
        o = LEND.Offer(**offs[i])
        return Reply(f"Aap {o.lender} ka {format_inr(o.principal_paise)} ka offer le rahe hain; EMI {format_inr(o.emi_paise)}. Pakka?", buttons=[("Haan, aage badhein ✅", f"x:loan:take:{i}"), ("Nahi ❌", "x:no")])
    s = gw.suggest(t._clock())
    return Reply((s or "Abhi koi kami nahi dikhti.") + "\n\nOffer dekhne ke liye apna passport (sirf jod-ghat, naam nahi) lender ko dikhana padega — aapki haan chahiye.",
                 buttons=[("Passport dikhao ✅", "x:loan:go"), ("Nahi ❌", "x:no")])


def c_policy(reg, t, chat, a):
    p = negotiator.Policy.load(t.aux)
    parts = a.split()
    if parts[:1] == ["on"]:
        p.enabled = True
    elif parts[:1] == ["off"]:
        p.enabled = False
    elif parts[:1] == ["ext"] and len(parts) == 2:
        p.max_extension_days = max(1, min(30, int(parts[1])))
    elif parts[:1] == ["inst"] and len(parts) == 2:
        p.max_installments = max(1, min(6, int(parts[1])))
    elif parts[:1] == ["max"] and len(parts) >= 2:
        p.max_auto_balance_paise = parse_amount(" ".join(parts[1:])) or p.max_auto_balance_paise
    p.save(t.aux)
    return Reply(f"🤖 Wasooli agent: {'CHALU' if p.enabled else 'band'} • vaada {p.max_extension_days} din tak apne aap • {p.max_installments} kisht tak • "
                 f"{format_inr(p.max_auto_balance_paise)} tak ka baaki. Isse bahar ya vivaad ho to aapse poochhta hai. Kabhi payment khud nahi likhta.")


def c_recon(reg, t, chat, a):
    d = reconcile.daily_digest(t.ledger, t._clock())
    return Reply(d.text, buttons=d.buttons) if d else Reply("Koi UPI/bank credit pending nahi.")


def c_edge(reg, t, chat, a):
    dev = SERVICES.edge_devices
    if dev is None:
        return Reply("Edge mic abhi server par chalu nahi (docs/EDGE.md).")
    if a.startswith("provision"):
        did, secret = dev.provision(t.shop_id, a[9:].strip() or "counter")
        return Reply(f"Device {did}\nSecret (sirf ek baar dikhega): {secret}\nDukaan par board lagayein: “Yahan awaaz-khata mic hai; sirf 'Munim ji' ke baad ki baat sunta hai.”")
    if a in ("mute", "unmute"):
        dev.set_muted(t.shop_id, a == "mute")
        return Reply("🔇 Mic band." if a == "mute" else "🎙 Mic chalu.")
    return Reply(f"Edge stats: {dev.stats(t.shop_id)}")


def c_smsdev(reg, t, chat, a):
    dev = SERVICES.sms_devices
    if dev is None:
        return Reply("SMS forwarder server par chalu nahi.")
    did, secret = dev.provision(t.shop_id, a or "phone")
    return Reply(f"SMS device {did}\nSecret (ek baar): {secret}\nSirf CREDIT SMS jate hain; OTP/debit nahi rakhe jate. Raqam tabhi likhi jayegi jab aap dabayenge.")


def c_invoice(reg, t, chat, a):
    return Reply("Distributor ka invoice (PDF ya saaf photo) bhejein — caption mein “invoice” likhein. Photo ke liye MUNIM_SCAN=1 chahiye.")


def c_aa(reg, t, chat, a):
    return Reply("AA ka (decrypt kiya hua) FI JSON file yahan bhejein, caption “aa”. Yeh sirf sandbox/demo hai — asli AA ke liye FIU registration chahiye.")


_CMDS = {"stock": c_stock, "price": c_price, "count": c_count, "ondc": c_ondc, "pincode": c_pincode, "mandate": c_mandate, "insure": c_insure,
         "pool": c_pool, "passport": c_passport, "loan": c_loan, "policy": c_policy, "recon": c_recon, "edge": c_edge, "smsdev": c_smsdev,
         "invoice": c_invoice, "aa": c_aa}


# ------------------------------------------------------------------------------------------------ files
async def invoice_file(reg, t, chat: int, data: bytes, mime: str, scan_allowed: bool) -> Reply:
    llm = SERVICES.llm
    if llm is None or not getattr(llm, "enabled", False):
        return Reply("Invoice padhne ke liye LLM key chahiye.")
    if mime != "application/pdf" and not scan_allowed:
        return Reply("Photo bahar ke model ko jati hai — MUNIM_SCAN=1 se chalu karein (ya text-wala PDF bhejein).")
    try:
        iv = await OCR.read_invoice(llm, data, mime, Inventory(t.aux, t._clock))
    except Exception as exc:
        return Reply(f"Invoice nahi padh paaya ({type(exc).__name__}). Saaf photo dobara bhejein.")
    _PENDING[(t.shop_id, chat)] = (iv, t._clock())
    probs = iv.problems + [f"{l.desc}: {','.join(l.problems)}" for l in iv.lines if l.problems]
    due = iv.due.isoformat() if iv.due else "15 din (andaza)"
    txt = (f"🧾 {iv.supplier} #{iv.invoice_no}: {format_inr(iv.total_paise or 0)}, {len(iv.lines)} line, due {due}. "
           f"Stock mein jayenge: {sum(1 for l in iv.lines if l.sku_id and not l.problems)} line.")
    if probs:
        txt += "\n⚠️ Gadbad: " + "; ".join(probs[:6]) + "\nJodh-ghat milta nahi — main ise theek nahi karta."
    return Reply(txt, buttons=[("Sahi, likho ✅", "x:inv:yes"), ("Nahi ❌", "x:no")] if iv.total_paise else [("Nahi ❌", "x:no")])


def aa_file(reg, t, chat: int, data: bytes) -> Reply:
    try:
        txns = aa.parse_fi(data.decode("utf-8"))
    except Exception:
        return Reply("FI JSON samajh nahi aaya.")
    out = aa.reconcile(t.ledger, txns, t._clock())
    return Reply(f"🏦 Bank: {out['credits']} credit • milaan hua {out['matched']} • aapki haan ke liye {out['queued']} • na mila {out['unmatched']}. "
                 f"Digital likhi par bank mein na dikhi: {out['ledger_digital_without_bank_credit']}. /recon dekhein.")


# ------------------------------------------------------------------------------------------------ callbacks
def callback(reg, t, chat: int, data: str) -> Reply:
    _, kind, *rest = data.split(":")
    if kind == "no":
        return Reply("Theek hai, kuch nahi kiya.")
    if not _owner(reg, chat):
        return Reply("Yeh sirf maalik kar sakte hain.")
    pin = reg.pincode(t.shop_id)
    try:
        if kind == "ondc":
            ONDC.enable(t)
            return Reply(f"✅ ONDC listing chalu: {ONDC.sync(t, pin, SERVICES.sink)}")
        if kind == "recon":
            return _recon_cb(t, rest[0])
        if kind == "ins":
            p = PF.build(t.ledger, t._clock())
            q = INS.indicate("STOCK_FIRE", p.tier, INS.suggest_sum_insured(INS.stock_value(Inventory(t.aux, t._clock))))
            pid = INS.Book(t, SERVICES.insurer).accept_quote(q)
            return Reply(f"✅ Prastaav #{pid} darj. Cover abhi shuru NAHI hua — insurer ko premium dekar /insure pay <raqam> likhein.")
        if kind == "claim":
            return Reply(f"Claim bheja: {INS.Book(t, SERVICES.insurer).claim_submit(int(rest[0]))}")
        if kind == "loan":
            gw = LEND.Gateway(t, SERVICES.lender, reg._master)
            if rest[0] == "go":
                offs, st = gw.request_offers(t._clock(), 20_000_000, 6, owner_consented=True)
                t.aux.put("loan_offers", [o.__dict__ for o in offs])
                return Reply(("\n\n".join(f"{i + 1}) " + LEND.offer_text(o) for i, o in enumerate(offs)) + "\n/loan take N") if offs else f"Abhi koi offer nahi ({st}).")
            o = LEND.Offer(**(t.aux.get("loan_offers") or [])[int(rest[1])])
            return Reply(f"✅ Aapka iraada lender tak gaya: {gw.accept(o, owner_confirmed=True)}. Paisa lender hi dega/legaa; Munim kuch nahi chhuta.")
        if kind == "inv":
            iv = _PENDING.pop((t.shop_id, chat), (None, 0))[0]
            if iv is None:
                return Reply("Samay khatam. Dobara bhejein.")
            r = OCR.commit(t, iv)
            return Reply("Pehle hi likha ja chuka hai." if r["duplicate"] else f"✅ {r['payables']}\nStock mein {r['stocked_lines']} line; due {r['due']}; na mile: {', '.join(r['unmatched'][:5]) or '—'}")
    except (ValueError, KeyError, IndexError, PermissionError) as exc:
        return Reply(f"⚠️ {exc}")
    return Reply("Samajh nahi aaya.")


def _recon_cb(t, what: str) -> Reply:
    L = t.ledger
    items = reconcile.pending(L)
    if what == "none":
        for i in items:
            reconcile.reject(L, i["id"])
        return Reply("Theek hai, koi payment nahi likhi.")
    if what == "review":
        names = {c.id: c.name for c in L.customers()}
        return Reply("\n".join(f"{i + 1}) {names.get(x['customer_id'])}: {format_inr(x['payload']['amount_paise'])} — {x['reason'][:60]}" for i, x in enumerate(items)) or "Kuch pending nahi.",
                     buttons=[("Sab sahi ✅", "x:recon:all"), ("Chhodo ❌", "x:recon:none")])
    n = 0
    for i in items:
        if reconcile.apply(L, i["id"]):
            n += 1
    return Reply(f"✅ {n} payment likh di gayi (undo possible).", buttons=[("Undo ↩️", "undo")])


# ------------------------------------------------------------------------------------------------ daily job
async def daily(reg, now: float | None = None) -> list[tuple[int, Reply]]:
    """Per shop: ONDC delta sync, premium accrual, pool contribution, reconcile digest, weekly cash-crunch nudge. Failures are isolated per shop."""
    out: list[tuple[int, Reply]] = []
    now = now or time.time()
    for sid in reg.shop_ids():
        try:
            t = reg.tenant(sid)
            owners = reg.chats(sid, "owner")
            if not owners:
                continue
            ONDC.sync(t, reg.pincode(sid), SERVICES.sink)
            pol = t.aux.db.execute("SELECT id FROM policies WHERE status IN ('accepted_unpaid','active')").fetchall()
            for p in pol:
                INS.Book(t, SERVICES.insurer).accrue(p["id"])
            if reg.pincode(sid):
                _pool(reg).contribute(sid)
            d = reconcile.daily_digest(t.ledger, now)
            if d:
                out.append((owners[0], Reply(d.text, buttons=d.buttons)))
            last = t.aux.get("crunch_nudge_ts", 0)
            if now - last > 7 * 86400:
                s = LEND.Gateway(t, SERVICES.lender, reg._master).suggest(now)
                if s:
                    t.aux.put("crunch_nudge_ts", now)
                    out.append((owners[0], Reply(s, buttons=[("Dekhein ✅", "x:loan:go"), ("Nahi ❌", "x:no")])))
        except Exception:
            continue
    return out
