import pytest

from munim.audit.auditor import ACT, ASK, BLOCK, SOFT, AuditContext, audit
from munim.config import Settings
from munim.scribe.extract import extract
from munim.scribe.names import Customer, resolve

CFG = Settings()
RS = 100

CUSTS = [
    Customer(1, "Ramesh Sharma"),
    Customer(2, "Sunita Verma"),
    Customer(3, "Mohan Das"),
    Customer(4, "Anil Gupta", aliases=["गुप्ता"]),
]
TWO_SHARMAS = CUSTS + [Customer(5, "Dinesh Sharma")]


# ------------------------------------------------------------------ names
@pytest.mark.parametrize("text,cid", [
    ("Sharma ji ko 150 udhaar", 1), ("Verma ji ke naam dedh sau", 2), ("verma bhaiya", 2),
    ("Sarma ji", 1), ("Gupta ji ne paanch sau diye", 4), ("गुप्ता जी", 4), ("Anil bhai ko", 4),
    ("Mohan Das ko likh do", 3), ("Sunita didi", 2), ("Ramesh Sharma", 1), ("Guptaa ji", 4),
])
def test_resolve_match(text, cid):
    r = resolve(text, CUSTS)
    assert r.status == "match" and r.customer.id == cid, (text, r)


def test_two_sharmas_ask_not_guess():
    r = resolve("Sharma ji ko 150 udhaar", TWO_SHARMAS)
    assert r.status == "ask"
    assert {c.id for c, _ in r.candidates} == {1, 5}


def test_full_name_disambiguates():
    r = resolve("Dinesh Sharma ko 150", TWO_SHARMAS)
    assert r.status == "match" and r.customer.id == 5


def test_none_for_stranger():
    assert resolve("Pandey ji ko 150 udhaar", CUSTS).status == "none"


def test_no_false_positive_on_common_words():
    assert resolve("aaj ka hisaab batao kal", CUSTS).status == "none"


# ------------------------------------------------------------------ extractor
@pytest.mark.parametrize("text,intent", [
    ("Sharma ji ko dedh sau udhaar likh do", "add_udhaar"),
    ("Sharma ji ke naam 150 jod do", "add_udhaar"),
    ("Verma ji ne paanch sau diye", "record_payment"),
    ("Verma ji ne udhaar ke paise diye 500", "record_payment"),
    ("Sharma ji ka kitna baaki hai", "balance_query"),
    ("Sharma ji ko reminder bhejo", "send_reminder"),
    ("Sharma ji ko reminder mat bhejo", "opt_out_customer"),
    ("Sharma ji ruko", "pause_customer"),
    ("Sharma ji dobara shuru karo", "resume_customer"),
    ("undo", "undo_last"),
    ("galti se likh diya, hata do", "undo_last"),
    ("help", "help"),
    ("namaste", "unknown"),
])
def test_intent(text, intent):
    assert extract(text, CUSTS).intent == intent


def test_das_surname_not_read_as_ten_rupees():
    e = extract("Das ji ko dedh sau udhaar", CUSTS)
    assert e.customer.id == 3 and e.amount_paise == 150 * RS


def test_bare_name_with_das_only_amount_none():
    e = extract("Das ji ko udhaar likh do", CUSTS)
    assert e.amount_paise is None


def test_multi_amount_sum_and_flag():
    e = extract("Sharma ji dedh sau aur dhai hazaar udhaar", CUSTS)
    assert e.amount_paise == 2650 * RS and len(e.spans) == 2


def test_quantity_ignored():
    e = extract("Sharma ji ko 2 kilo cheeni 150 udhaar", CUSTS)
    assert e.amount_paise == 150 * RS


# ------------------------------------------------------------------ auditor
def mk(a_text, b_text=None, **kw):
    a = extract(a_text, CUSTS)
    b = extract(b_text, CUSTS) if b_text is not None else None
    return AuditContext(a=a, b=b, snr_db=kw.pop("snr", 25.0), duration_s=kw.pop("dur", 3.0), **kw)


def codes(ctx):
    return audit(ctx, CFG).codes


def test_clean_is_soft():
    r = audit(mk("Sharma ji ko 150 udhaar likh do", "Sharma ji ko 150 udhaar likh do"), CFG)
    assert r.decision == SOFT and r.amount_paise == 150 * RS and r.customer.id == 1


def test_b_missing_blocks():
    r = audit(mk("Sharma ji ko 150 udhaar", None), CFG)
    assert r.decision == BLOCK and "b_missing" in r.codes


def test_amount_disagreement_blocks_and_keeps_alt():
    r = audit(mk("Sharma ji ko dedh sau udhaar", "Sharma ji ko dedh hazaar udhaar"), CFG)
    assert r.decision == BLOCK
    assert {"amount_disagreement", "scale_risk"} <= set(r.codes)
    assert r.amount_paise == 150 * RS and r.alt_amount_paise == 1500 * RS


def test_disagreement_non_scale():
    r = audit(mk("Sharma ji ko 150 udhaar", "Sharma ji ko 250 udhaar"), CFG)
    assert "amount_disagreement" in r.codes and "scale_risk" not in r.codes


def test_low_snr_and_short():
    assert "low_snr" in codes(mk("Sharma ji ko 150 udhaar", "Sharma ji ko 150 udhaar", snr=6.0))
    assert "short_clip" in codes(mk("Sharma ji ko 150 udhaar", "Sharma ji ko 150 udhaar", dur=0.6))


def test_outlier_no_history():
    assert "outlier" in codes(mk("Sharma ji ko das hazaar ek udhaar", "Sharma ji ko das hazaar ek udhaar"))
    assert "outlier" not in codes(mk("Sharma ji ko paanch hazaar udhaar", "Sharma ji ko paanch hazaar udhaar"))


def test_outlier_with_history():
    hist = [20000, 30000, 25000, 40000]      # median Rs 275 -> limit max(1375, 5000) = 5000
    c = mk("Sharma ji ko chhe hazaar udhaar", "Sharma ji ko chhe hazaar udhaar", history_paise=hist)
    assert "outlier" in codes(c)
    c = mk("Sharma ji ko paanch sau udhaar", "Sharma ji ko paanch sau udhaar", history_paise=hist)
    assert "outlier" not in codes(c)


def test_duplicate_within_window():
    base = "Sharma ji ko 150 udhaar"
    assert "duplicate" in codes(mk(base, base, recent=[(1, "add_udhaar", 150 * RS, 60.0)]))
    assert "duplicate" not in codes(mk(base, base, recent=[(1, "add_udhaar", 150 * RS, 300.0)]))


def test_multi_amount_blocks():
    t = "Sharma ji dedh sau aur dhai hazaar udhaar"
    r = audit(mk(t, t), CFG)
    assert r.decision == BLOCK and "multi_amount" in r.codes


def test_ambiguous_name_blocks_with_candidates():
    a = extract("Sharma ji ko 150 udhaar", TWO_SHARMAS)
    r = audit(AuditContext(a=a, b=a, snr_db=30, duration_s=3), CFG)
    assert r.decision == BLOCK and "ambiguous_name" in r.codes and len(r.candidates) == 2


def test_missing_customer_and_amount():
    r = audit(mk("Pandey ji ko udhaar likh do", "Pandey ji ko udhaar likh do"), CFG)
    assert {"missing_customer", "missing_amount"} <= set(r.codes)


def test_language_switch():
    c = mk("Sharma ji ko 150 udhaar", "Sharma ji ko 150 udhaar")
    c.lang_a = "ta-IN"
    assert "language_switch" in codes(c)


def test_balance_query_no_block():
    r = audit(mk("Sharma ji ka kitna baaki hai"), CFG)
    assert r.decision == ACT and r.customer.id == 1


def test_undo_and_unknown():
    assert audit(mk("undo"), CFG).decision == ACT
    assert audit(mk("namaste"), CFG).decision == ASK


def test_intent_disagreement():
    r = audit(mk("Sharma ji ko 150 udhaar", "Sharma ji ne 150 diye"), CFG)
    assert "intent_disagreement" in r.codes
