"""Customer side of a mandate: see the terms in plain words, accept, or revoke — by text/voice in their own chat."""
from __future__ import annotations

import re

from munim.scribe.indic_numbers import format_inr

MWORD = re.compile(r"\b(mandate|autopay|auto\s*pay|sweep|auto\s*kat)\b", re.I)
YES = re.compile(r"\b(haan|han|yes|ok|okay|manzoor|accept|theek)\b", re.I)
NO = re.compile(r"\b(nahi|no|band|cancel|revoke|hatao|mat)\b", re.I)


def rail():
    from munim.payments.mandate import SandboxRail
    return SandboxRail()


def terms_text(m, en: bool) -> str:
    dbt = "DBT" in m.sources
    if en:
        return (f"The shop proposes an automatic settlement: when money arrives from {', '.join(m.sources)}, at most {m.cap_pct}% of that credit "
                f"(never more than {format_inr(m.per_sweep_paise)} at a time, {format_inr(m.total_cap_paise)} in total) goes toward your udhaar. "
                f"You keep the rest. You can cancel any time by saying “mandate stop”. {'This includes government benefit credits (DBT), only because you must opt in. ' if dbt else ''}"
                "Reply “mandate yes” to accept.")
    return (f"Dukaan ek auto-settlement ka prastaav rakh rahi hai: {', '.join(m.sources)} se paise aane par us credit ka zyada se zyada {m.cap_pct}% "
            f"(ek baar mein {format_inr(m.per_sweep_paise)} se zyada nahi, kul {format_inr(m.total_cap_paise)}) aapke udhaar mein jayega. Baaki paise aapke paas rahenge. "
            f"Kabhi bhi “mandate band” bolkar ruk sakte hain. {'Isme sarkaari laabh (DBT) bhi hai — sirf aapki haan par. ' if dbt else ''}Manzoor ho to “mandate haan” likhein.")


def customer_message(tenant, cid: int, text: str, lang: str):
    from munim.payments.mandate import Engine, MandateError
    from munim.portal import PortalResult
    if not MWORD.search(text):
        return None
    en = lang == "en"
    tts = "en-IN" if en else "hi-en"
    eng = Engine(tenant, rail())
    name = tenant.ledger.customer_row(cid)["name"]
    open_m = eng.for_customer(cid, "proposed", "active")
    if open_m is None:
        m = "Abhi koi mandate prastaav nahi hai." if not en else "There is no mandate proposal right now."
        return PortalResult(m, None, tts)
    if NO.search(text) and not YES.search(text):
        eng.revoke(open_m.id, "customer")
        m = "Mandate band kar diya. Ab koi paisa apne aap nahi katega." if not en else "Mandate cancelled. Nothing will be taken automatically."
        from munim.service import Reply
        return PortalResult(m, m, tts, [Reply(f"🛑 {name} ne mandate band kiya.")])
    if YES.search(text) and open_m.status == "proposed":
        try:
            eng.accept(open_m.id, by_customer=True)
        except MandateError:
            return PortalResult("Prastaav ab valid nahi hai.", None, tts)
        from munim.service import Reply
        m = "Mandate chalu ho gaya. Kabhi bhi “mandate band” likhkar rok sakte hain." if not en else "Mandate is active. Say “mandate stop” any time."
        return PortalResult(m, m, tts, [Reply(f"✅ {name} ne mandate manzoor kiya.")])
    return PortalResult(terms_text(open_m, en), None, tts)
