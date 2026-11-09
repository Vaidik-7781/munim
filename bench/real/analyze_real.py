"""Authentic-speech analysis. Truth = numbers in the HUMAN reference transcript (parsed by our parser).

Per system and SNR band: exact-number-match rate, number recall, 95% bootstrap CIs.
MUNIM_dual: if both passes agree on the numbers -> 'writes'; wrong+agree = SILENT ERROR; disagree = 'asks'.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from bench.analyze import boot_ci  # noqa: E402
from munim.realdata import number_values, numeric_match  # noqa: E402

BANDS = [("<10dB", -1e9, 10), ("10-20dB", 10, 20), (">=20dB", 20, 1e9)]


def band(snr: float) -> str:
    return next(n for n, lo, hi in BANDS if lo <= snr < hi)


def analyze_real(rows: list[dict]) -> dict:
    g: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        b = band(r.get("snr_db", 99.0))
        for key, name in (("hyp_v3", "S1_gnani_v3"), ("hyp_v25", "S2_gnani_v25")):
            if r.get(key) is None:
                continue
            exact, rec = numeric_match(r["ref"], r[key])
            for bb in (b, "ALL"):
                g[(name, bb)].append({"exact": exact, "recall": rec, "wrote": True, "silent": not exact, "asked": False})
        if r.get("hyp_v3") is not None and r.get("hyp_v25") is not None:
            agree = Counter(number_values(r["hyp_v3"])) == Counter(number_values(r["hyp_v25"]))
            exact, rec = numeric_match(r["ref"], r["hyp_v3"])
            for bb in (b, "ALL"):
                g[("MUNIM_dual", bb)].append({"exact": agree and exact, "recall": rec if agree else 0.0,
                                              "wrote": agree, "silent": agree and not exact, "asked": not agree})
    out = {}
    for (s, b), outs in sorted(g.items()):
        f = lambda k: boot_ci(np.array([o[k] for o in outs], dtype=float))
        out[f"{s}|{b}"] = {"n": len(outs), "exact": f("exact"), "recall": f("recall"), "silent_error": f("silent"),
                           "ask_rate": f("asked")}
    return out


def fmt(t: dict) -> str:
    c = lambda x: f"{x[0] * 100:5.1f}% [{x[1] * 100:4.1f},{x[2] * 100:5.1f}]"
    lines = [f"{'system|band':<24}{'n':>4}  {'exact-number':>20}  {'SILENT-ERR':>20}  {'ask':>20}"]
    for k, v in t.items():
        lines.append(f"{k:<24}{v['n']:>4}  {c(v['exact']):>20}  {c(v['silent_error']):>20}  {c(v['ask_rate']):>20}")
    return "\n".join(lines)


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "bench/real/results.jsonl")
    print(fmt(analyze_real([json.loads(x) for x in open(p, encoding="utf-8") if x.strip()])))
