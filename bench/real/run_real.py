"""Run Gnani v3.0 + v2.5 on the REAL clips in bench/real/manifest.jsonl. Spends 2 STT calls per clip.

    python bench/real/run_real.py --yes [--limit 60]
Then:  python bench/real/analyze_real.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from munim.config import load_settings  # noqa: E402
from munim.gnani.client import PRIORITY_BATCH, GnaniClient, GnaniError  # noqa: E402
from munim.gnani.stt import parse_stt  # noqa: E402
from munim.listen.snr import estimate_snr_db, read_wav  # noqa: E402


async def main() -> int:
    if "--yes" not in sys.argv:
        print(__doc__)
        return 1
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 10_000
    cfg = load_settings()
    c = GnaniClient(cfg.gnani_key, cfg.gnani_base_url)
    rows = [json.loads(line) for line in open(ROOT / "bench/real/manifest.jsonl", encoding="utf-8")][:limit]
    out = (ROOT / "bench/real/results.jsonl").open("a", encoding="utf-8")
    for r in rows:
        wav = (ROOT / r["file"]).read_bytes()
        x, sr = read_wav(wav)
        rec = {"utt_id": r["id"], "ref": r["ref"], "snr_db": estimate_snr_db(x, sr), "dur_s": len(x) / sr}
        for key, model in (("hyp_v3", "prisma-v3.0"), ("hyp_v25", None)):
            try:
                raw = await c.stt_raw(wav, language_code="hi-IN", model=model, priority=PRIORITY_BATCH)
                rec[key] = parse_stt(raw, model or "v2.5").transcript
            except GnaniError as e:
                rec[key] = None
                print("ERR", r["id"], key, e)
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        out.flush()
    print(c.meter.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
