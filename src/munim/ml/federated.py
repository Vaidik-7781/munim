"""Weights-only federation across shops (FedAvg).

Each shop trains locally on ITS OWN ledger (no rows ever leave); only (weights, bias, n_samples) are shared and averaged,
weighted by sample count. Fixed feature transforms make the parameter spaces compatible. Optional clipping + Gaussian
noise on the shared update is provided as a knob — it is NOT a formal differential-privacy guarantee (no accounting).
A shop with fewer than MIN_SAMPLES contributes nothing; shared weights contain no names, amounts or dates.
"""
from __future__ import annotations

import numpy as np

from munim.ledger.chain import Ledger

from .credit_model import CreditModel, build_samples, weekly_cutoffs

MIN_SAMPLES = 40


def local_update(ledger: Ledger, global_model: CreditModel | None, *, steps: int = 150, clip: float | None = None,
                 noise_std: float = 0.0, rng: np.random.Generator | None = None) -> CreditModel | None:
    X, y, _ = build_samples(ledger, weekly_cutoffs(ledger))
    if len(y) < MIN_SAMPLES:
        return None
    m = CreditModel().fit(X, y, steps=steps, init=global_model)
    if clip is not None:
        delta_w = m.w - (global_model.w if global_model else 0.0)
        norm = float(np.linalg.norm(delta_w))
        if norm > clip:
            m.w = (global_model.w if global_model else 0.0) + delta_w * (clip / norm)
    if noise_std > 0:
        rng = rng or np.random.default_rng()
        m.w = m.w + rng.normal(0, noise_std, m.w.shape)
        m.b = m.b + float(rng.normal(0, noise_std))
    return m


def fedavg(updates: list[CreditModel]) -> CreditModel | None:
    ups = [u for u in updates if u is not None and u.n_train > 0]
    if not ups:
        return None
    n = np.array([u.n_train for u in ups], dtype=float)
    wts = n / n.sum()
    return CreditModel(sum(wt * u.w for wt, u in zip(wts, ups)), float(sum(wt * u.b for wt, u in zip(wts, ups))), int(n.sum()))


def run_round(ledgers: list[Ledger], global_model: CreditModel | None = None, rounds: int = 3, **kw) -> CreditModel | None:
    g = global_model
    for _ in range(rounds):
        g = fedavg([local_update(l, g, **kw) for l in ledgers]) or g
    return g
