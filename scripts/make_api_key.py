"""Create an API key for a shop:  python scripts/make_api_key.py <shop_id>   (the key is shown once; only its hash is stored)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from munim.config import load_settings  # noqa: E402
from munim.tenancy.registry import Registry  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: make_api_key.py <shop_id>")
    cfg = load_settings()
    print(Registry(cfg.root_dir, cfg.hmac_secret, cfg).create_api_key(sys.argv[1]))
