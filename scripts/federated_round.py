"""Run one federated round across ALL shops in this deployment (weights only; no rows leave any shop).

    python scripts/federated_round.py [rounds=2]
Writes <MUNIM_ROOT>/global_model.json, used by /score. Shops with too little history abstain. Schedule weekly (cron)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from munim.config import load_settings  # noqa: E402
from munim.tenancy.registry import Registry  # noqa: E402


def main() -> int:
    cfg = load_settings()
    reg = Registry(cfg.root_dir, cfg.hmac_secret, cfg)
    g = reg.federated_round(int(sys.argv[1]) if len(sys.argv) > 1 else 2)
    print("no shop had enough history yet" if g is None else f"global model saved ({g.n_train} samples across shops)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
