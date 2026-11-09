"""Cheap SNR estimate from a 16 kHz mono WAV: speech-level / noise-floor ratio over 20 ms frames.

Not a lab-grade SNR — a *risk heuristic*: below ~10 dB the Auditor forces explicit confirmation.
"""
from __future__ import annotations

import io
import wave

import numpy as np


def read_wav(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sr, n, ch, width = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    if width != 2:
        raise ValueError("expected 16-bit PCM")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def estimate_snr_db(x: np.ndarray, sr: int, frame_ms: int = 20) -> float:
    n = max(1, int(sr * frame_ms / 1000))
    frames = len(x) // n
    if frames < 5:
        return 0.0
    rms = np.sqrt((x[: frames * n].reshape(frames, n) ** 2).mean(axis=1)) + 1e-9
    noise = float(np.percentile(rms, 10))
    speech = float(np.percentile(rms, 90))
    return float(20.0 * np.log10(speech / noise))


def duration_s(x: np.ndarray, sr: int) -> float:
    return len(x) / float(sr)
