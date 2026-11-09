"""Download a small set of REAL Hindi speech clips whose transcripts contain numbers.

    pip install datasets soundfile            # on YOUR machine (this sandbox cannot reach huggingface.co)
    python data/real/fetch_hf.py --source fleurs  --n 60
    HF_TOKEN=hf_xxx python data/real/fetch_hf.py --source kathbath --n 60   # gated: accept terms on the dataset page first

Writes bench/real/audio/*.wav (16 kHz mono) and bench/real/manifest.jsonl:
  {id, source, ref, seconds, file}
Sources (verify licence on the dataset card before redistributing audio; we only keep it locally):
  fleurs   google/fleurs  config hi_in   (public; CC-BY-4.0 per card)
  kathbath ai4bharat/Kathbath  Hindi     (GATED; needs HF token; card lists CC-BY-4.0 / CC0 wording)
NOTE: audio is read with decode=False and converted with ffmpeg, avoiding the torchcodec requirement of datasets>=4.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from munim.realdata import pick_text, select_number_utterances  # noqa: E402


def to_wav16k(raw: bytes) -> bytes:
    return subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
                          input=raw, capture_output=True, check=True).stdout


def iter_rows(source: str, split: str, scan: int):
    from datasets import Audio, load_dataset

    if source == "fleurs":
        ds = load_dataset("google/fleurs", "hi_in", split=split, streaming=True)
    elif source == "kathbath":
        ds = load_dataset("ai4bharat/Kathbath", "hindi", split=split, streaming=True)   # config name: check the card
    else:
        raise SystemExit(f"unknown source {source}")
    ds = ds.cast_column("audio", Audio(decode=False))
    for i, row in enumerate(ds):
        if i >= scan:
            return
        a = row["audio"]
        raw = a.get("bytes")
        if raw is None and a.get("path"):
            raw = Path(a["path"]).read_bytes()
        n = row.get("num_samples")
        secs = n / 16000 if n else row.get("duration")
        yield {"id": f"{source}_{split}_{i}", "audio_bytes": raw, "seconds": secs, **{k: v for k, v in row.items() if k != "audio"}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="fleurs", choices=["fleurs", "kathbath"])
    ap.add_argument("--split", default="test")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--scan", type=int, default=3000, help="max rows to stream while looking for number-bearing ones")
    args = ap.parse_args()
    rows = list(iter_rows(args.source, args.split, args.scan))
    chosen = select_number_utterances(rows, args.n)
    out = ROOT / "bench" / "real"
    (out / "audio").mkdir(parents=True, exist_ok=True)
    with (out / "manifest.jsonl").open("w", encoding="utf-8") as fh:
        for r in chosen:
            wav = to_wav16k(r["audio_bytes"])
            p = out / "audio" / f"{r['id']}.wav"
            p.write_bytes(wav)
            fh.write(json.dumps({"id": r["id"], "source": args.source, "ref": pick_text(r), "seconds": r.get("seconds"),
                                 "file": str(p.relative_to(ROOT))}, ensure_ascii=False) + "\n")
    print(f"scanned {len(rows)} rows, kept {len(chosen)} number-bearing clips -> {out}/manifest.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
