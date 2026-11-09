"""STT presets: two independent passes so disagreement can be detected (PRD §Listener)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import PRIORITY_INTERACTIVE, GnaniClient


@dataclass
class STTResult:
    transcript: str
    language: str | None
    request_id: str | None
    latency_s: float | None
    truncated: bool
    model: str
    raw: dict[str, Any]


def parse_stt(raw: dict[str, Any], model: str) -> STTResult:
    transcript = raw.get("transcript")
    if transcript is None:
        out = raw.get("output") or {}
        transcript = out.get("literal") or out.get("transcript") or ""
    lat = raw.get("end_to_end_latency", raw.get("processing_time"))
    try:
        lat = float(lat) if lat is not None else None
    except (TypeError, ValueError):
        lat = None
    return STTResult(
        transcript=str(transcript).strip(),
        language=raw.get("detected_language_code"),
        request_id=raw.get("request_id"),
        latency_s=lat,
        truncated=bool(raw.get("truncated", False)),
        model=model,
        raw=raw,
    )


async def pass_a_prisma_v3(
    client: GnaniClient,
    audio: bytes,
    *,
    keyterms: list[str] | None = None,
    previous_text: str | None = None,
    language_code: str = "hi-IN",
    priority: int = PRIORITY_INTERACTIVE,
) -> STTResult:
    raw = await client.stt_raw(
        audio,
        language_code=language_code,
        model="prisma-v3.0",
        fmt="transcribe",
        keyterms=keyterms,
        topic="kirana udhaar ledger: amounts, customer names, payments",
        previous_text=previous_text,
        priority=priority,
    )
    return parse_stt(raw, "prisma-v3.0")


async def pass_b_v25(
    client: GnaniClient,
    audio: bytes,
    *,
    bias_list: list[str] | None = None,
    language_code: str = "hi-IN",
    priority: int = PRIORITY_INTERACTIVE,
) -> STTResult:
    """v2.5: single language, no model/format params, optional bias list."""
    extra: dict[str, str] = {}
    if bias_list:
        import json as _json

        extra["bias_list"] = _json.dumps(bias_list[:200], ensure_ascii=False)
        extra["bias_score"] = "5"
    raw = await client.stt_raw(
        audio, language_code=language_code, model=None, priority=priority, extra=extra
    )
    return parse_stt(raw, "v2.5")
