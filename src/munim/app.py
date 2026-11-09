"""Entry point: python -m munim.app   (long polling; needs TELEGRAM_BOT_TOKEN + GNANI_API_KEY_ID)."""
from __future__ import annotations

import logging
import os
import sys

from munim.config import load_settings
from munim.gnani.client import CreditMeter, GnaniClient
from munim.tenancy.registry import Registry


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # never log secrets: httpx logs full URLs at INFO, which is fine (no key in URL), but keep it quiet
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_settings()
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", cfg.telegram_token), ("GNANI_API_KEY_ID", cfg.gnani_key)) if not v]
    if missing:
        print("Missing env vars: " + ", ".join(missing) + "  (see .env.example)", file=sys.stderr)
        return 2
    if cfg.hmac_secret == "dev-only-secret-change-me":
        print("WARNING: MUNIM_HMAC_SECRET not set — using an insecure dev secret.", file=sys.stderr)
    os.makedirs(cfg.root_dir, exist_ok=True)
    registry = Registry(cfg.root_dir, cfg.hmac_secret, cfg)
    meter = CreditMeter(remaining=cfg.starting_credits, floor=cfg.credit_floor)
    client = GnaniClient(cfg.gnani_key, cfg.gnani_base_url, meter=meter)
    from munim.bot.telegram_bot import build_app

    app = build_app(cfg, registry, client)
    from telegram.error import NetworkError, TimedOut
    try:
        app.run_polling(allowed_updates=["message", "callback_query"], bootstrap_retries=5)
    except (TimedOut, NetworkError) as exc:
        print("\nCannot reach Telegram (" + exc.__class__.__name__ + "). Your network may block api.telegram.org.\n"
              "Run: python scripts/check_telegram.py  - then try a phone hotspot/VPN, or set MUNIM_TELEGRAM_PROXY in .env.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
