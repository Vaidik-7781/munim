"""Split a long dictation into <=28 s pieces, cutting at the quietest moment near each boundary.

Gnani STT takes at most 30 s per call. End-of-day dictation ("Sharma ko sau, Verma ne paanch sau diye, ...")
is longer, so we cut at silences (never mid-word if avoidable) and chain the pieces with `previous_text`.
"""
from __future__ import annotations

import io
import wave

import numpy as np


def split_wav(wav: bytes, max_s: float = 28.0, search_s: float = 5.0, hop_ms: int = 10) -> list[bytes]:
    with wave.open(io.BytesIO(wav), "rb") as w:
        sr, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width != 2 or ch != 1:
        raise ValueError("expected 16-bit mono PCM")
    x = np.frombuffer(raw, dtype="<i2")
    if len(x) <= max_s * sr:
        return [wav]
    hop = int(sr * hop_ms / 1000)
    cuts, start = [], 0
    while len(x) - start > max_s * sr:
        hi = start + int(max_s * sr)
        lo = max(start + int(sr * 2), hi - int(search_s * sr))          # never make a piece shorter than 2 s
        seg = x[lo:hi].astype(np.float32)
        frames = len(seg) // hop
        if frames == 0:
            cut = hi
        else:
            rms = np.sqrt((seg[:frames * hop].reshape(frames, hop) ** 2).mean(axis=1))
            # quietest 10 ms frame; ties resolve to the LATEST such frame (longer pieces, fewer API calls)
            idx = int(np.flatnonzero(rms == rms.min())[-1])
            cut = lo + idx * hop + hop // 2
        cuts.append((start, cut))
        start = cut
    cuts.append((start, len(x)))
    out = []
    for a, b in cuts:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
            w.writeframes(x[a:b].tobytes())
        out.append(buf.getvalue())
    return out
