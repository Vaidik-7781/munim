"""Temporal backtest of the learned repayment model (and weights-only federation) on seeded ledgers.

    python bench/backtest_credit.py
Train on EARLY weekly cut-offs, test on LATER ones; compare with a constant-rate baseline. The seeded ledgers are
SIMULATED payers, so this validates the pipeline and leakage-free evaluation — it does NOT measure real-world accuracy.
Run it on a real shop's ledger (or the federated model on several) for that. Results are printed as they come, good or bad.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from data.seed import seed  # noqa: E402
from munim.ml.credit_model import auc, build_samples, temporal_backtest, weekly_cutoffs  # noqa: E402
from munim.ml.federated import run_round  # noqa: E402


def main() -> None:
    d = tempfile.mkdtemp()
    ledgers = [seed(os.path.join(d, f"{i}.db"), f"s{i}", 90, 2610 + i) for i in range(4)]
    for i, l in enumerate(ledgers):
        print(f"shop {i} local:", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in temporal_backtest(l).items()})
    held = ledgers[3]
    g = run_round(ledgers[:3], rounds=3)
    cuts = weekly_cutoffs(held)
    X, y, _ = build_samples(held, cuts[int(len(cuts) * 0.6):])
    print(f"federated (shops 0-2) on unseen shop 3: AUC={auc(y, g.predict(X))} n={len(y)}")


if __name__ == "__main__":
    main()
