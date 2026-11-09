"""Generate benchmark audio (non-Gnani TTS = gTTS, so Gnani STT is not graded on its own voice),
degrade it under C0-C3, and transcribe with Gnani v3.0 and v2.5. Appends JSONL rows.

    pip install -e .[bench]   &&   python bench/run_gnani.py --yes [--limit 30]
Budget: 30 utt x 4 cond x 2 models = 240 calls. Tune on 30%, report the held-out 70% (see METHODS.md).
"""
from __future__ import annotations

import asyncio
import io
import json
import zlib
import os
import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from bench.make_noise import CONDITIONS, apply_condition, to_wav_bytes  # noqa: E402
from munim.config import load_settings  # noqa: E402
from munim.gnani.client import PRIORITY_BATCH, GnaniClient, GnaniError  # noqa: E402
from munim.gnani.stt import parse_stt  # noqa: E402
from munim.listen.snr import estimate_snr_db  # noqa: E402

AUDIO = ROOT / "bench" / "audio"
RESULTS = ROOT / "bench" / "results" / "results.jsonl"
TEMPLATE = {"udhaar": "{name} जी को {amt} रुपये उधार लिख दो", "diye": "{name} जी ने {amt} रुपये दिए"}


def synth(text: str, path: Path) -> np.ndarray:
    from gtts import gTTS
    import subprocess

    if not path.exists():
        buf = io.BytesIO()
        gTTS(text, lang="hi").write_to_fp(buf)
        wav = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
                             input=buf.getvalue(), capture_output=True, check=True).stdout
        path.write_bytes(wav)
    import wave
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


async def main() -> int:
    if "--yes" not in sys.argv:
        print(__doc__)
        return 1
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 30
    cfg = load_settings()
    client = GnaniClient(cfg.gnani_key, cfg.gnani_base_url)
    AUDIO.mkdir(parents=True, exist_ok=True)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    utts = yaml.safe_load(open(ROOT / "bench" / "utterances.yaml", encoding="utf-8"))[:limit]
    with RESULTS.open("a", encoding="utf-8") as out:
        for u in utts:
            text = TEMPLATE[u["kind"]].format(name=u["name"], amt=u["amt"])
            clean = synth(text, AUDIO / f"{u['id']}.wav")
            for cond in CONDITIONS:
                x, sr = apply_condition(clean, 16000, cond, seed=zlib.crc32(u["id"].encode()) % 10_000)
                wav = to_wav_bytes(x, sr)
                snr = estimate_snr_db(x, sr)
                texts = {}
                for system, model in (("S1_gnani_v3", "prisma-v3.0"), ("S2_gnani_v25", None)):
                    try:
                        raw = await client.stt_raw(wav, language_code="hi-IN", model=model, priority=PRIORITY_BATCH)
                        texts[system] = parse_stt(raw, model or "v2.5").transcript
                    except GnaniError as e:
                        texts[system] = ""
                        print("ERR", u["id"], cond, system, e)
                for system, key in (("S1_gnani_v3", "S1_gnani_v3"), ("S2_gnani_v25", "S2_gnani_v25")):
                    out.write(json.dumps({"system": system, "condition": cond, "utt_id": u["id"],
                                          "expect_paise": u["expect"] * 100, "transcript": texts[key]}, ensure_ascii=False) + "\n")
                out.write(json.dumps({"system": "MUNIM_dual", "condition": cond, "utt_id": u["id"],
                                      "expect_paise": u["expect"] * 100, "transcript": texts["S1_gnani_v3"],
                                      "transcript_b": texts["S2_gnani_v25"], "snr_db": snr, "dur_s": len(x) / sr},
                                     ensure_ascii=False) + "\n")
                out.flush()
    print(client.meter.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
