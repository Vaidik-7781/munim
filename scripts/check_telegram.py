"""Is Telegram reachable from this machine, and is the bot token valid?   python scripts/check_telegram.py"""
from __future__ import annotations

import os
import socket
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from munim.config import load_settings  # noqa: E402


def main() -> int:
    cfg = load_settings()
    if not cfg.telegram_token:
        print("TELEGRAM_BOT_TOKEN missing in .env"); return 2
    proxy = os.environ.get("MUNIM_TELEGRAM_PROXY") or None
    try:
        ip = socket.gethostbyname("api.telegram.org")
        print("1) DNS ok ->", ip)
    except OSError as e:
        print("1) DNS FAILED:", e, "\n   -> network/DNS blocks Telegram. Try phone hotspot or VPN."); return 1
    t = time.time()
    try:
        r = httpx.get("https://api.telegram.org/bot" + cfg.telegram_token + "/getMe", timeout=30, proxy=proxy)
    except httpx.HTTPError as e:
        print("2) CONNECT FAILED after %.0fs: %s\n   -> Telegram blocked or too slow on this network. Try phone hotspot or VPN, or set MUNIM_TELEGRAM_PROXY." % (time.time() - t, e.__class__.__name__)); return 1
    print("2) HTTP %s in %.1fs" % (r.status_code, time.time() - t))
    if r.status_code == 401:
        print("   -> token invalid. Re-copy it from @BotFather."); return 1
    if r.status_code == 200:
        print("3) OK. Bot: @" + r.json()["result"]["username"], "\n   Now run: python -m munim.app"); return 0
    print("   body:", r.text[:200]); return 1


if __name__ == "__main__":
    raise SystemExit(main())
