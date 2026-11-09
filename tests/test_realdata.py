import json
import random

import pytest

from bench.real.analyze_real import analyze_real, band
from munim.realdata import is_number_bearing, numeric_match, number_values, pick_text, select_number_utterances

# Rows shaped like google/fleurs hi_in (raw_transcription/transcription/num_samples) and Kathbath (text/duration)
FLEURS_LIKE = [
    {"id": 1, "raw_transcription": "इस वर्ष 2019 में 15 लोग आए", "transcription": "इस वर्ष 2019 में 15 लोग आए", "num_samples": 16000 * 6},
    {"id": 2, "raw_transcription": "मौसम आज बहुत अच्छा है", "transcription": "मौसम आज बहुत अच्छा है", "num_samples": 16000 * 4},
    {"id": 3, "raw_transcription": "कीमत डेढ़ सौ रुपये है", "num_samples": 16000 * 5},
    {"id": 4, "raw_transcription": "1 2 3 4 5 6 7 8", "num_samples": 16000 * 5},
    {"id": 5, "raw_transcription": "उसने दो लाख रुपये दिए", "num_samples": 16000 * 31},
]


def test_pick_text_prefers_raw():
    assert pick_text({"raw_transcription": "a", "transcription": "b"}) == "a"
    assert pick_text({"text": "x"}) == "x" and pick_text({"sentence": "s"}) == "s" and pick_text({}) == ""


def test_selection_filters_numberless_overlong_and_numberstorms():
    rows = [{**r, "seconds": r["num_samples"] / 16000} for r in FLEURS_LIKE]
    got = select_number_utterances(rows, 10, seed=1)
    ids = {r["id"] for r in got}
    assert ids == {1, 3}              # 2 has no numbers, 4 is a number storm (>4), 5 exceeds 28 s


def test_selection_is_seeded_and_capped():
    rows = [{"id": i, "text": f"{i + 10} रुपये", "seconds": 3} for i in range(50)]
    a = [r["id"] for r in select_number_utterances(rows, 5, seed=3)]
    b = [r["id"] for r in select_number_utterances(rows, 5, seed=3)]
    assert a == b and len(a) == 5


def test_numeric_match_is_format_agnostic():
    assert numeric_match("डेढ़ सौ रुपये", "150 रुपये") == (True, 1.0)
    assert numeric_match("पचहत्तर हज़ार चार सौ चालीस", "75,440") == (True, 1.0)
    ex, rec = numeric_match("2019 में 15 लोग", "2019 में 50 लोग")
    assert not ex and rec == 0.5
    assert numeric_match("कोई संख्या नहीं", "कुछ नहीं") == (True, 1.0)


def test_real_analysis_flags_silent_errors_and_asks():
    ref = "कीमत डेढ़ सौ रुपये है"
    rows = [
        {"utt_id": "a", "ref": ref, "hyp_v3": "कीमत 150 रुपये है", "hyp_v25": "कीमत 150 रुपये है", "snr_db": 25},   # agree, right
        {"utt_id": "b", "ref": ref, "hyp_v3": "कीमत 1500 रुपये है", "hyp_v25": "कीमत 150 रुपये है", "snr_db": 8},   # disagree -> ask
        {"utt_id": "c", "ref": ref, "hyp_v3": "कीमत 250 रुपये है", "hyp_v25": "कीमत 250 रुपये है", "snr_db": 15},  # agree, wrong -> silent
    ]
    t = analyze_real(rows)
    m = t["MUNIM_dual|ALL"]
    assert m["n"] == 3
    assert abs(m["ask_rate"][0] - 1 / 3) < 1e-9 and abs(m["silent_error"][0] - 1 / 3) < 1e-9
    assert abs(t["S1_gnani_v3|ALL"]["silent_error"][0] - 2 / 3) < 1e-9
    assert band(8) == "<10dB" and band(15) == "10-20dB" and band(30) == ">=20dB"


# ---------------- Kaggle-shaped realistic ledger ----------------
def test_realistic_ledger_from_bigbasket_shaped_csv(tmp_path):
    from data.real.build_realistic_ledger import ARCHETYPES, basket, build, load_names, load_prices

    p = tmp_path / "bb.csv"
    rows = ["index,product,category,sale_price,market_price"] + [
        f"{i},Item {i},Staples,{price},{price + 5}" for i, price in enumerate([20, 45, 60, 99, 120, 35, 250, 18, 75, 140, 55, 30, 12, 9000, 2])]
    p.write_text("\n".join(rows), encoding="utf-8")
    prices = load_prices(str(p))
    assert 9000 not in prices and 2 not in prices and len(prices) == 13   # staple range only
    names = load_names(None, random.Random(1), 12)
    assert len(set(names)) == 12
    assert all(basket(prices, random.Random(i)) % 5 == 0 for i in range(50))
    led = build(prices, names, str(tmp_path / "r.db"), days=90)
    assert led.verify()[0] and len(led.entries()) > 100
    assert all(led.balance(c.id) >= 0 for c in led.customers())          # never overpaid in simulation
    srcs = {e.meta.get("src") for e in led.entries() if e.kind in ("add_udhaar", "record_payment")}
    assert srcs == {"bigbasket_basket", "settlement"}
    assert abs(sum(a.weight for a in ARCHETYPES) - 1.0) < 1e-9


def test_price_column_missing_fails_loudly(tmp_path):
    from data.real.build_realistic_ledger import load_prices

    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,2\n")
    with pytest.raises(SystemExit):
        load_prices(str(p))
