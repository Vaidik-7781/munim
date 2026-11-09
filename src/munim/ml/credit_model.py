"""Learned repayment model: P(customer pays something in the next 14 days | their history up to now).

Pure numpy logistic regression with L2 — small, inspectable, fast, and trainable inside a single kirana's data.
Features use FIXED, bounded transforms (no data-dependent scaling) so models trained in different shops live in the same
parameter space and can be averaged (see federated.py). Every prediction is explainable: contribution = weight x feature.
This model ADVISES (risk ranking, /score); it never writes money and never decides to message anyone.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

from munim.ledger.chain import MONEY_KINDS, Ledger

DAY = 86400.0
FEATURES = ["log_balance", "days_since_pay", "n_payments", "mean_gap", "overdue_ratio", "oldest_open", "pay_ratio"]
HORIZON_DAYS = 14

NAMES_HI = {
    "log_balance": "baaki raqam", "days_since_pay": "pichli payment ko kitne din hue", "n_payments": "ab tak kitni payment ki",
    "mean_gap": "do payment ke beech ka aam antar", "overdue_ratio": "aam antar ke mukable der", "oldest_open": "sabse purana baaki",
    "pay_ratio": "ek baar mein aam payment (baaki ke mukable)",
}


def _live(ledger: Ledger):
    rev = ledger._reversed_seqs()
    return [e for e in ledger.entries() if e.kind in MONEY_KINDS and e.seq not in rev]


def features_at(adds: list[tuple[float, int]], pays: list[tuple[float, int]], t: float) -> list[float] | None:
    """adds/pays: (ts, paise) sorted by ts. Uses only data <= t. None if nothing is owed at t."""
    a = [x for x in adds if x[0] <= t]
    p = [x for x in pays if x[0] <= t]
    bal = sum(x[1] for x in a) - sum(x[1] for x in p)
    if bal <= 0:
        return None
    last = p[-1][0] if p else None
    since = min((t - last) / DAY, 120.0) if last is not None else 120.0
    gaps = [(q[0] - r[0]) / DAY for r, q in zip(p, p[1:])]
    mean_gap = float(np.mean(gaps)) if gaps else 30.0
    over = min(since / max(mean_gap, 1.0), 5.0)
    remaining, oldest = bal, a[-1][0]
    for ts, amt in reversed(a):                     # FIFO from the newest back: what is still open and how old is its oldest part
        if remaining <= 0:
            break
        oldest, remaining = ts, remaining - amt
    pay_ratio = min((float(np.mean([x[1] for x in p])) / bal) if p else 0.0, 1.0)
    return [math.log1p(bal / 100.0) / 10.0, since / 120.0, math.log1p(len(p)) / 3.0, min(mean_gap, 90.0) / 90.0,
            over / 5.0, min((t - oldest) / DAY, 180.0) / 180.0, pay_ratio]


def build_samples(ledger: Ledger, cutoffs: list[float], horizon_days: int = HORIZON_DAYS):
    """One row per (customer, cutoff) with money owed; label = any payment in the next `horizon_days`."""
    ents = _live(ledger)
    by_ref: dict[str, tuple[list, list]] = {}
    for e in ents:
        adds, pays = by_ref.setdefault(e.customer_ref, ([], []))
        (adds if e.kind == "add_udhaar" else pays).append((e.ts, e.amount_paise))
    X, y, meta = [], [], []
    for ref, (adds, pays) in by_ref.items():
        for t in cutoffs:
            f = features_at(adds, pays, t)
            if f is None:
                continue
            X.append(f)
            y.append(1.0 if any(t < ts <= t + horizon_days * DAY for ts, _ in pays) else 0.0)
            meta.append((ref, t))
    return np.array(X, dtype=float).reshape(-1, len(FEATURES)), np.array(y, dtype=float), meta


def weekly_cutoffs(ledger: Ledger, warmup_days: int = 28, horizon_days: int = HORIZON_DAYS) -> list[float]:
    ts = [e.ts for e in _live(ledger)]
    if not ts:
        return []
    lo, hi = min(ts) + warmup_days * DAY, max(ts) - horizon_days * DAY
    out, t = [], lo
    while t <= hi:
        out.append(t)
        t += 7 * DAY
    return out


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


@dataclass
class CreditModel:
    w: np.ndarray = field(default_factory=lambda: np.zeros(len(FEATURES)))
    b: float = 0.0
    n_train: int = 0

    def predict(self, X) -> np.ndarray:
        X = np.atleast_2d(np.asarray(X, dtype=float))
        return _sigmoid(X @ self.w + self.b)

    def fit(self, X, y, *, steps: int = 400, lr: float = 0.5, l2: float = 1e-2, init: "CreditModel | None" = None) -> "CreditModel":
        X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
        w = (init.w.copy() if init else np.zeros(X.shape[1]))
        b = init.b if init else 0.0
        n = max(len(y), 1)
        for _ in range(steps):
            p = _sigmoid(X @ w + b)
            g = p - y
            w -= lr * (X.T @ g / n + l2 * w)
            b -= lr * float(g.mean())
        self.w, self.b, self.n_train = w, float(b), len(y)
        return self

    def explain(self, x) -> list[tuple[str, float]]:
        c = self.w * np.asarray(x, dtype=float)
        return sorted(zip(FEATURES, c.tolist()), key=lambda kv: -abs(kv[1]))

    def to_json(self) -> str:
        return json.dumps({"w": self.w.tolist(), "b": self.b, "n": self.n_train, "features": FEATURES})

    @staticmethod
    def from_json(s: str) -> "CreditModel":
        d = json.loads(s)
        if d.get("features") != FEATURES:
            raise ValueError("model was trained on a different feature set")
        return CreditModel(np.array(d["w"], dtype=float), float(d["b"]), int(d.get("n", 0)))


def auc(y, p) -> float | None:
    y, p = np.asarray(y), np.asarray(p)
    pos, neg = p[y == 1], p[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    ranks = p.argsort().argsort() + 1.0
    # average ranks for ties
    for v in np.unique(p):
        m = p == v
        if m.sum() > 1:
            ranks[m] = ranks[m].mean()
    return float((ranks[y == 1].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def brier(y, p) -> float:
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def temporal_backtest(ledger: Ledger, train_frac: float = 0.6) -> dict:
    """Train on EARLY cutoffs only, test on LATER ones. Compared with a constant-rate baseline."""
    cuts = weekly_cutoffs(ledger)
    k = max(1, int(len(cuts) * train_frac))
    Xtr, ytr, _ = build_samples(ledger, cuts[:k])
    Xte, yte, _ = build_samples(ledger, cuts[k:])
    if len(ytr) < 20 or len(yte) < 20:
        return {"error": "not enough samples", "n_train": len(ytr), "n_test": len(yte)}
    m = CreditModel().fit(Xtr, ytr)
    base = float(ytr.mean())
    return {"n_train": len(ytr), "n_test": len(yte), "auc": auc(yte, m.predict(Xte)),
            "brier_model": brier(yte, m.predict(Xte)), "brier_constant": brier(yte, np.full(len(yte), base)),
            "base_rate_test": float(yte.mean())}


def score_customer(ledger: Ledger, customer_id: int, now: float, model: CreditModel):
    ref = ledger.ref(customer_id)
    adds, pays = [], []
    for e in _live(ledger):
        if e.customer_ref == ref:
            (adds if e.kind == "add_udhaar" else pays).append((e.ts, e.amount_paise))
    f = features_at(adds, pays, now)
    if f is None:
        return None
    return float(model.predict(f)[0]), model.explain(f)
