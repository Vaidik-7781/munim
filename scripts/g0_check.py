"""G0 gate: the few real-API facts the plan could not verify offline. Spends ~8 calls.

    export GNANI_API_KEY_ID=...    # never commit
    python scripts/g0_check.py --yes

Before running: note your credit balance on the Gnani dashboard. After: note it again.
(credits_per_call = difference / calls printed at the end.)
Writes data/g0_report.json and data/g0_samples/*.ogg so you can LISTEN to the TTS.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from munim.config import load_settings  # noqa: E402
from munim.gnani.client import GnaniClient, GnaniError  # noqa: E402
from munim.gnani.stt import parse_stt  # noqa: E402
from munim.scribe.indic_numbers import parse_amount, rupees_to_words  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "data")


async def ogg_to_wav(ogg: bytes) -> bytes:
    p = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
                       input=ogg, capture_output=True, check=True)
    return p.stdout


def codec_of(path: str) -> str:
    try:
        return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                               "stream=codec_name", "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        return "ffprobe-missing"


async def main() -> int:
    if "--yes" not in sys.argv:
        print(__doc__)
        print("Re-run with --yes to spend the credits.")
        return 1
    cfg = load_settings()
    c = GnaniClient(cfg.gnani_key, cfg.gnani_base_url)
    os.makedirs(os.path.join(OUT, "g0_samples"), exist_ok=True)
    report: dict = {}

    async def attempt(name, coro):
        try:
            report[name] = {"ok": True, **(await coro)}
        except GnaniError as e:
            report[name] = {"ok": False, "status": e.status, "error": str(e)[:600], "body": e.body[:600]}
        print(name, "->", json.dumps(report[name], ensure_ascii=False)[:700])

    words_deva = rupees_to_words(75440, "deva") + " रुपये"
    async def tts_words():
        a = await c.tts_raw(words_deva, voice="Shivani", language="hi-IN", container="ogg")
        path = os.path.join(OUT, "g0_samples", "tts_words.ogg")
        open(path, "wb").write(a)
        return {"bytes": len(a), "ogg_magic": a[:4] == b"OggS", "codec": codec_of(path), "file": path}

    async def tts_digits():
        a = await c.tts_raw("₹75,440 उधार", voice="Shivani", language="hi-IN", container="ogg")
        path = os.path.join(OUT, "g0_samples", "tts_digits.ogg")
        open(path, "wb").write(a)
        return {"bytes": len(a), "file": path, "note": "LISTEN: is 75,440 read correctly? (we always use words anyway)"}

    await attempt("tts_words_ogg", tts_words())
    await attempt("tts_digits_ogg", tts_digits())

    # round trip: TTS(words) -> STT; does the parser recover 75,440 from what Prisma hears?
    async def roundtrip():
        wav = await ogg_to_wav(open(os.path.join(OUT, "g0_samples", "tts_words.ogg"), "rb").read())
        raw3 = await c.stt_raw(wav, language_code="hi-IN", model="prisma-v3.0", fmt="transcribe")
        r3 = parse_stt(raw3, "prisma-v3.0")
        raw25 = await c.stt_raw(wav, language_code="hi-IN", model=None)
        r25 = parse_stt(raw25, "v2.5")
        return {"v3_transcript": r3.transcript, "v3_paise": parse_amount(r3.transcript),
                "v25_transcript": r25.transcript, "v25_paise": parse_amount(r25.transcript),
                "expected_paise": 7_544_000, "v3_lang": r3.language, "v3_latency_s": r3.latency_s}
    await attempt("roundtrip_stt_both_models", roundtrip())

    async def odia():
        wav = await ogg_to_wav(open(os.path.join(OUT, "g0_samples", "tts_words.ogg"), "rb").read())
        raw = await c.stt_raw(wav, language_code="or-IN", model="prisma-v3.0", fmt="transcribe")
        return {"transcript": parse_stt(raw, "prisma-v3.0").transcript}
    await attempt("stt_or_IN_supported", odia())

    report["meter"] = c.meter.summary()
    with open(os.path.join(OUT, "g0_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print("\nCALLS MADE:", report["meter"]["calls"], "-> compare with your dashboard credit drop for credits/call.")
    print("Now play data/g0_samples/*.ogg and send tts_words.ogg to Telegram as a voice note to confirm playback.")
    await c.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
