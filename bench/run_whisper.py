"""Open baseline (S3): faster-whisper small on the SAME degraded audio. Appends JSONL rows (no API cost)."""
from __future__ import annotations

import json
import zlib
import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.make_noise import CONDITIONS, apply_condition  # noqa: E402

TEMPLATE_NOTE = "Run bench/run_gnani.py first so bench/audio/*.wav exist."


def main() -> int:
    from faster_whisper import WhisperModel
    import wave

    model = WhisperModel("small", compute_type="int8")
    utts = yaml.safe_load(open(ROOT / "bench" / "utterances.yaml", encoding="utf-8"))
    out = (ROOT / "bench" / "results" / "results.jsonl").open("a", encoding="utf-8")
    for u in utts:
        p = ROOT / "bench" / "audio" / f"{u['id']}.wav"
        if not p.exists():
            print(TEMPLATE_NOTE)
            return 1
        with wave.open(str(p)) as w:
            clean = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        for cond in CONDITIONS:
            x, sr = apply_condition(clean, 16000, cond, seed=zlib.crc32(u["id"].encode()) % 10_000)
            if sr != 16000:
                x = np.interp(np.arange(0, len(x) / sr, 1 / 16000), np.arange(len(x)) / sr, x).astype(np.float32)
            segs, _ = model.transcribe(x, language="hi", beam_size=1)
            out.write(json.dumps({"system": "S3_whisper_small", "condition": cond, "utt_id": u["id"],
                                  "expect_paise": u["expect"] * 100, "transcript": " ".join(s.text for s in segs)},
                                 ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
