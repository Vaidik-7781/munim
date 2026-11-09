"""Noise Shield conditions. C0 clean | C1 10 dB | C2 0 dB | C3 8 kHz mu-law telephony + 10 dB.

Noise is synthetic 'market babble' (band-limited noise with slow amplitude modulation) — a
documented stand-in for real mandi noise, stated as a limitation in METHODS.md.
"""
from __future__ import annotations

import io
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

CONDITIONS = {"C0": None, "C1": 10.0, "C2": 0.0, "C3": 10.0}


def babble(n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    white = rng.standard_normal(n)
    spec = np.fft.rfft(white)
    f = np.fft.rfftfreq(n, 1 / sr)
    spec *= ((f > 200) & (f < 3800)) / np.maximum(f, 1.0) ** 0.5    # speech-band pink-ish
    x = np.fft.irfft(spec, n)
    t = np.arange(n) / sr
    x *= 0.6 + 0.4 * np.sin(2 * np.pi * (1.5 + rng.random()) * t + rng.random() * 6.28)
    return x / (np.sqrt((x ** 2).mean()) + 1e-12)


def add_noise(x: np.ndarray, snr_db: float, sr: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = babble(len(x), sr, rng)
    p_sig = float((x ** 2).mean()) + 1e-12
    n *= np.sqrt(p_sig / (10 ** (snr_db / 10)))
    return x + n


def mulaw_8k(x: np.ndarray, sr: int) -> tuple[np.ndarray, int]:
    # resample to 8 kHz (linear), mu-law companding round trip (256 levels)
    t_new = np.arange(0, len(x) / sr, 1 / 8000)
    y = np.interp(t_new, np.arange(len(x)) / sr, x)
    mu = 255.0
    comp = np.sign(y) * np.log1p(mu * np.abs(y)) / np.log1p(mu)
    q = np.round((comp + 1) / 2 * 255) / 255 * 2 - 1
    y = np.sign(q) * (np.expm1(np.abs(q) * np.log1p(mu))) / mu
    return y.astype(np.float32), 8000


def apply_condition(x: np.ndarray, sr: int, cond: str, seed: int = 0) -> tuple[np.ndarray, int]:
    snr = CONDITIONS[cond]
    if snr is None:
        return x, sr
    y = add_noise(x, snr, sr, seed)
    peak = float(np.abs(y).max())
    if peak > 0.99:
        y = y / peak * 0.95          # scales signal and noise equally, SNR unchanged
    if cond == "C3":
        return mulaw_8k(y, sr)
    return y, sr


def to_wav_bytes(x: np.ndarray, sr: int) -> bytes:
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr), w.writeframes(pcm)
    return buf.getvalue()
