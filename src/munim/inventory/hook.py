"""Conversation hook: turns "2 kilo atta sold" / "stocked 50 packets of Maggi" into stock events (service-level, any transport)."""
from __future__ import annotations

from munim.inventory.stock import Inventory, fmt_qty
from munim.inventory.voice import StockCmd, parse


def _inv(svc) -> Inventory | None:
    f = getattr(svc, "aux_factory", None)
    return Inventory(f(), svc._clock) if f else None


def handle(svc, chat: int, text: str):
    from munim.service import Pending, Reply
    inv = _inv(svc)
    if inv is None:
        return None
    cmd = parse(text)
    if cmd is None:
        return None
    by = svc.actors.get(chat)
    m = inv.find(cmd.item)
    if m.status == "ask":
        names = ", ".join(s.name for s in m.candidates or [])
        return Reply(f"Kaun sa item? {names}. Poora naam bolkar dobara bolein.")
    if m.status == "none":
        if cmd.kind != "restock" or not cmd.item:
            return Reply(f"“{cmd.item or '?'}” stock list mein nahi hai. Pehle bolein: “{cmd.item or 'item'} {fmt_qty(cmd.qty, cmd.dim)} aaya”.")
        svc.pending[chat] = Pending("new_sku", "new_sku", None, None, meta={"item": cmd.item, "qty": cmd.qty, "dim": cmd.dim, "by": by},
                                    created=svc._clock())
        return Reply(f"Naya item jodein: {cmd.item.title()} — {fmt_qty(cmd.qty, cmd.dim)} stock? Sahi hai?",
                     buttons=[("Haan ✅", "yes"), ("Nahi ❌", "no")])
    s = m.sku
    assert s is not None
    if s.dim != cmd.dim:
        return Reply(f"{s.name} {s.dim} mein gina jata hai, aapne {cmd.raw_unit} bola. Dobara bolein.")
    if cmd.kind == "sale":
        ev = inv.sale(s.id, cmd.qty, src=f"voice{':' + by if by else ''}")
        s = inv.get(s.id)
        warn = ""
        if s.stock < 0:
            warn = "\n⚠️ GintI se zyada bika — stock galat ho sakta hai, /count se theek karein."
        elif s in inv.low_stock():
            warn = f"\n🔔 {s.name} kam bacha hai — mangwane ka samay."
        return Reply(f"✅ {s.name}: {fmt_qty(cmd.qty, s.dim)} bika. Bacha: {fmt_qty(max(s.stock, 0), s.dim)}.{warn}",
                     buttons=[("Undo ↩️", f"stockundo:{ev}")])
    if cmd.kind == "restock":
        ev = inv.restock(s.id, cmd.qty, src=f"voice{':' + by if by else ''}")
        s = inv.get(s.id)
        return Reply(f"📦 {s.name}: {fmt_qty(cmd.qty, s.dim)} aaya. Ab stock: {fmt_qty(s.stock, s.dim)}.",
                     buttons=[("Undo ↩️", f"stockundo:{ev}")])
    inv.count(s.id, cmd.qty)
    return Reply(f"🔢 {s.name}: ginti {fmt_qty(cmd.qty, s.dim)} darj. (Ab ONDC par yeh pakka maana jayega.)")


def confirm_new(svc, chat: int, p):
    from munim.service import Reply
    inv = _inv(svc)
    svc.pending.pop(chat, None)
    m = p.meta
    s = inv.upsert(m["item"], m["dim"])
    inv.restock(s.id, m["qty"], src="voice", note="new sku")
    s = inv.get(s.id)
    return Reply(f"📦 Naya item: {s.name} — {fmt_qty(s.stock, s.dim)}. Daam batayein: “{s.name.lower()} 14 rupaye packet” "
                 "(ONDC par daam ke bina nahi dikhega).")


def undo(svc, data: str):
    from munim.service import Reply
    inv = _inv(svc)
    ok = inv.undo(int(data.split(":", 1)[1])) if inv else False
    return Reply("↩️ Stock entry ulat di gayi." if ok else "Yeh entry pehle hi ulat chuki hai.")
