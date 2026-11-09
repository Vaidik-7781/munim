"""Try Munim with NO API keys: type what you would say; see exactly how it decides.

    python data/seed.py && python scripts/demo_cli.py
Type a second transcript after '|' to simulate the second STT pass disagreeing, e.g.
    Sharma ji ko dedh sau udhaar | Sharma ji ko dedh hazaar udhaar
Prefix with '~' to simulate a noisy clip (SNR 4 dB).  Commands: :bal  :verify  :quit
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from munim.config import Settings  # noqa: E402
from munim.ledger.chain import Ledger  # noqa: E402
from munim.scribe.indic_numbers import format_inr  # noqa: E402
from munim.service import MunimService  # noqa: E402

DB = "data/munim_demo.db"


def main() -> None:
    if not os.path.exists(DB):
        print("run: python data/seed.py   first")
        return
    led = Ledger(DB, "demo-secret")
    svc = MunimService(led, Settings(), "Gupta Kirana")
    print(__doc__)
    buttons: list[tuple[str, str]] = []
    while True:
        try:
            line = input("you> ").strip()
        except EOFError:
            return
        if line == ":quit":
            return
        if line == ":verify":
            print(led.verify()[2]); continue
        if line == ":bal":
            for c in led.customers()[:8]:
                print(f"  {c.name:<18}{format_inr(led.balance(c.id))}")
            continue
        if line.startswith("/"):
            print("munim> Slash commands are Telegram-only. Type a sentence, e.g. 'Sharma ji ka kitna baaki hai?'")
            continue
        if line.isdigit() and 1 <= int(line) <= len(buttons):
            r = svc.on_callback(1, buttons[int(line) - 1][1])
            print("munim>", r.text)
            buttons = list(r.buttons or [])
            if buttons:
                print("       buttons:", " | ".join(f"{i}) {t}" for i, (t, _) in enumerate(buttons, 1)))
            continue

        noisy = line.startswith("~")
        line = line.lstrip("~").strip()
        a, _, b = line.partition("|")
        r = svc.on_heard(1, a.strip(), (b.strip() or a.strip()), snr=4.0 if noisy else 25.0, dur=3.0)
        print("munim>", r.text)
        buttons = list(r.buttons or [])
        if buttons:
            print("       buttons:", " | ".join(f"{i}) {t}" for i, (t, _) in enumerate(buttons, 1)),
                  "  (type the number or haan / nahi)")
if __name__ == "__main__":
    main()
