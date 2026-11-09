import numpy as np
import pytest
import yaml

from bench.analyze import analyze, boot_ci
from bench.make_noise import CONDITIONS, add_noise, apply_condition, mulaw_8k
from munim.listen.snr import estimate_snr_db
from munim.scribe.indic_numbers import parse_amount, rupees_to_words


def test_truth_set_is_self_consistent():
    rows = yaml.safe_load(open("bench/utterances.yaml", encoding="utf-8"))
    assert len(rows) == 30 and len({r["id"] for r in rows}) == 30
    for r in rows:
        assert parse_amount(r["amt"]) == r["expect"] * 100, r          # Devanagari 'say' text parses to truth
        assert r["kind"] in ("udhaar", "diye")


def test_add_noise_hits_target_snr():
    sr = 16000
    t = np.arange(sr * 3) / sr
    x = 0.3 * np.sin(2 * np.pi * 200 * t)
    for target in (10.0, 0.0):
        y = add_noise(x, target, sr)
        noise = y - x
        got = 10 * np.log10((x ** 2).mean() / (noise ** 2).mean())
        assert abs(got - target) < 1.0


def test_conditions_and_mulaw():
    sr = 16000
    x = (0.3 * np.sin(2 * np.pi * 300 * np.arange(sr * 2) / sr)).astype(np.float32)
    assert set(CONDITIONS) == {"C0", "C1", "C2", "C3"}
    y, sr2 = apply_condition(x, sr, "C3")
    assert sr2 == 8000 and abs(len(y) - len(x) // 2) < 5
    y0, s0 = apply_condition(x, sr, "C0")
    assert np.array_equal(y0, x)
    z, _ = mulaw_8k(x, sr)
    assert np.corrcoef(z[:1000], np.interp(np.arange(1000) / 8000, np.arange(len(x)) / sr, x)[:1000])[0, 1] > 0.98


def test_bootstrap_ci_brackets_mean():
    m, lo, hi = boot_ci(np.array([1, 1, 1, 0, 1, 1, 0, 1, 1, 1], dtype=float))
    assert lo <= m <= hi and 0.5 < m < 1.0


def rows(system, cond, pairs):
    return [dict(system=system, condition=cond, utt_id=f"u{i}", expect_paise=e * 100,
                 transcript=a, transcript_b=b, snr_db=25.0, dur_s=3.0) for i, (e, a, b) in enumerate(pairs)]


def test_munim_converts_silent_errors_into_questions():
    good = (150, "Sharma ji ko dedh sau", "Sharma ji ko dedh sau")
    # system hears 'dedh hazaar' in pass A (wrong) but pass B is right -> disagreement -> ASK, not a silent error
    miss = (150, "Sharma ji ko dedh hazaar", "Sharma ji ko dedh sau")
    data = rows("S1", "C2", [good] * 5 + [miss] * 5)
    data += rows("MUNIM", "C2", [good] * 5 + [miss] * 5)
    t = analyze(data)
    assert t["S1|C2"]["silent_error"][0] == 0.5
    assert t["MUNIM|C2"]["silent_error"][0] == 0.0
    assert t["MUNIM|C2"]["ask_rate"][0] == 0.5
    assert t["S1|C2"]["amount_accuracy"][0] == 0.5


def test_munim_cannot_hide_when_both_passes_wrong_same_way():
    # honest limitation: identical wrong hearing in both passes is NOT caught -> silent error stays
    same_wrong = (150, "Sharma ji ko dhai sau", "Sharma ji ko dhai sau")
    t = analyze(rows("MUNIM", "C1", [same_wrong] * 4))
    assert t["MUNIM|C1"]["silent_error"][0] == 1.0
