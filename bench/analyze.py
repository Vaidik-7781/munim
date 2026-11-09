"""Noise Shield analysis. Same amount parser for EVERY system => no system is advantaged by post-processing.

Input: JSONL rows {system, condition, utt_id, expect_paise, transcript, [transcript_b, snr_db, dur_s]}
Metrics per (system, condition) with 95% bootstrap CIs:
  amount_accuracy  — parsed amount == truth
  silent_error     — system would WRITE a wrong amount with no question asked   (the harm metric)
  ask_rate         — system declined to auto-write (only Munim-auditor system can ask)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from munim.audit.auditor import SOFT, AuditContext, audit  # noqa: E402
from munim.config import Settings  # noqa: E402
from munim.scribe.extract import extract  # noqa: E402
from munim.scribe.indic_numbers import parse_amount  # noqa: E402
from munim.scribe.names import Customer  # noqa: E402

BENCH_CUSTOMERS = [Customer(i + 1, n) for i, n in enumerate(
    ["Sharma", "Verma", "Gupta", "Yadav", "Mishra", "Patel", "Singh", "Das", "Sahu"])]


def _tr(s: str) -> str:
    return s.replace("₹", " rs ")


def single_system_outcome(row: dict) -> dict:
    got = parse_amount(row["transcript"])  # parse_amount already treats the rupee sign as currency
    return {"correct": got == row["expect_paise"], "wrote": got is not None, "asked": False}


def munim_outcome(row: dict, cfg: Settings) -> dict:
    # benchmark utterances are phrased as ledger commands; force the udhaar intent word set
    a = extract(row["transcript"] + " udhaar", BENCH_CUSTOMERS)
    b = extract(row["transcript_b"] + " udhaar", BENCH_CUSTOMERS) if row.get("transcript_b") is not None else None
    res = audit(AuditContext(a=a, b=b, snr_db=row.get("snr_db"), duration_s=row.get("dur_s", 3.0)), cfg)
    # only amount questions are measured here (names are fixed per utterance)
    amt_flags = {"amount_disagreement", "scale_risk", "missing_amount", "multi_amount", "low_snr",
                 "b_missing", "outlier", "short_clip"}
    asked = res.decision != SOFT and any(c in amt_flags for c in res.codes)
    return {"correct": res.amount_paise == row["expect_paise"], "wrote": res.decision == SOFT, "asked": asked}


def boot_ci(x: np.ndarray, n: int = 2000, seed: int = 7) -> tuple[float, float, float]:
    if len(x) == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return float(x.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def analyze(rows: list[dict], cfg: Settings | None = None) -> dict:
    cfg = cfg or Settings()
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        out = munim_outcome(r, cfg) if r["system"].startswith("MUNIM") else single_system_outcome(r)
        groups[(r["system"], r["condition"])].append(out)
    table: dict[str, dict] = {}
    for (sys_, cond), outs in sorted(groups.items()):
        acc = np.array([o["correct"] for o in outs], dtype=float)
        silent = np.array([o["wrote"] and not o["correct"] for o in outs], dtype=float)
        ask = np.array([o["asked"] for o in outs], dtype=float)
        table[f"{sys_}|{cond}"] = {
            "n": len(outs),
            "amount_accuracy": boot_ci(acc),
            "silent_error": boot_ci(silent),
            "ask_rate": boot_ci(ask),
        }
    return table


def fmt(table: dict) -> str:
    lines = [f"{'system|cond':<22}{'n':>4}  {'acc':>20}  {'SILENT-ERR':>20}  {'ask':>20}"]
    for k, v in table.items():
        f = lambda t: f"{t[0] * 100:5.1f}% [{t[1] * 100:4.1f},{t[2] * 100:5.1f}]"
        lines.append(f"{k:<22}{v['n']:>4}  {f(v['amount_accuracy']):>20}  {f(v['silent_error']):>20}  {f(v['ask_rate']):>20}")
    return "\n".join(lines)


def ask_causes(rows: list[dict], cfg: Settings | None = None) -> dict:
    """Why MUNIM_dual did not auto-write: counts per (condition, cause). Causes are exclusive, first match wins."""
    cfg = cfg or Settings()
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if r["system"] != "MUNIM_dual":
            continue
        a = extract(r["transcript"] + " udhaar", BENCH_CUSTOMERS)
        tb = r.get("transcript_b")
        b = extract(tb + " udhaar", BENCH_CUSTOMERS) if tb is not None else None
        res = audit(AuditContext(a=a, b=b, snr_db=r.get("snr_db"), duration_s=r.get("dur_s", 3.0)), cfg)
        if res.decision == SOFT:
            out[r["condition"]]["auto-written"] += 1
            continue
        c = set(res.codes)
        if c & {"amount_disagreement", "scale_risk"}:
            cause = "ears disagree on amount"
        elif c & {"missing_amount", "multi_amount", "b_missing"}:
            cause = "amount unclear"
        elif c & {"low_snr", "short_clip"}:
            cause = "bad audio"
        elif "outlier" in c:
            cause = "large first-time amount (policy)"
        elif "missing_customer" in c:
            cause = "customer name unclear"
        else:
            cause = "other: " + ",".join(sorted(c))
        out[r["condition"]][cause] += 1
    return {k: dict(v) for k, v in sorted(out.items())}


def fmt_causes(d: dict) -> str:
    lines = ["", "MUNIM_dual outcome by cause (count of utterances per condition)"]
    for cond, causes in d.items():
        lines.append(f"  {cond}: " + "; ".join(f"{k}={v}" for k, v in sorted(causes.items(), key=lambda kv: -kv[1])))
    return "\n".join(lines)


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "bench/results/results.jsonl"
    data = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    print(fmt(analyze(data)))
    print(fmt_causes(ask_causes(data)))
