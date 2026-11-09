"""Helpers for benchmarking on AUTHENTIC public speech/text (no synthetic voices).

Schema-agnostic on purpose: dataset column names differ (FLEURS: raw_transcription/transcription,
Kathbath: text, Common Voice: sentence). Numbers are compared through OUR parser on both the
reference and the hypothesis, so digits-vs-words formatting differences do not count as errors.
"""
from __future__ import annotations

import random
from collections import Counter
from typing import Any, Iterable

from munim.scribe.indic_numbers import parse_amounts

TEXT_FIELDS = ("raw_transcription", "transcription", "text", "sentence", "normalized", "verbatim")


def pick_text(row: dict[str, Any]) -> str:
    for f in TEXT_FIELDS:
        v = row.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def number_values(text: str) -> list[int]:
    """All valid numeric spans (incl. quantities/years) as integer paise — a language-agnostic multiset key."""
    return [s.paise for s in parse_amounts(text) if s.valid and s.paise is not None]


def is_number_bearing(text: str, min_values: int = 1, max_values: int = 4) -> bool:
    vals = number_values(text)
    return min_values <= len(vals) <= max_values


def select_number_utterances(rows: Iterable[dict[str, Any]], n: int, *, max_seconds: float = 28.0,
                             seed: int = 2610, seconds_field: str = "seconds") -> list[dict[str, Any]]:
    """Pick up to n number-bearing rows, seeded, shortest-clip friendly (Gnani STT limit is 30 s)."""
    pool = []
    for r in rows:
        t = pick_text(r)
        if not t or not is_number_bearing(t):
            continue
        secs = r.get(seconds_field)
        if secs is not None and secs > max_seconds:
            continue
        pool.append({**r, "_ref": t})
    random.Random(seed).shuffle(pool)
    return pool[:n]


def numeric_match(ref: str, hyp: str) -> tuple[bool, float]:
    """(exact multiset match, recall of reference numbers)."""
    a, b = Counter(number_values(ref)), Counter(number_values(hyp))
    if not a:
        return (not b), 1.0
    inter = sum((a & b).values())
    return a == b, inter / sum(a.values())
