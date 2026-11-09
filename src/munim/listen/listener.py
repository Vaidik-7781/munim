"""Listener: Telegram voice note -> two independent transcripts + audio-quality facts.

Audio is converted to 16 kHz mono WAV (<=30 s, Gnani's STT limit) and is NEVER stored: the
bytes live in memory for the duration of the request only.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from munim.gnani.client import (
    PRIORITY_INTERACTIVE,
    GnaniAuthError,
    GnaniClient,
    GnaniCreditsExhausted,
    GnaniError,
)
from munim.gnani.stt import STTResult, parse_stt, pass_a_prisma_v3, pass_b_v25
from munim.scribe.indic_numbers import parse_amount
from munim.scribe.names import Customer, resolve

from .snr import duration_s, estimate_snr_db, read_wav

log = logging.getLogger("munim.listen")
MAX_SECONDS = 30.0


class AudioTooLong(Exception):
    pass


class AudioDecodeError(Exception):
    pass


@dataclass
class Prepared:
    wav: bytes
    seconds: float
    snr_db: float


# Languages the PRD verified for Prisma v3.0 beyond Hindi/English. (or-IN is NOT included until g0_check proves it.)
RETRY_LANGS = {"ta-IN", "te-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "bn-IN", "gu-IN"}
HOME_LANGS = {None, "", "hi-IN", "en-IN", "hi", "en", "hi-en"}


@dataclass
class Heard:
    a: STTResult | None
    b: STTResult | None
    prepared: Prepared
    b_error: str | None = None
    c: STTResult | None = None            # third ear (codemix), only on amount disagreement
    retried_lang: str | None = None       # language the shopkeeper actually spoke, if not Hindi/English
    d: STTResult | None = None            # name-rescue pass (format=transliterate), only when no customer was recognised
    calls: int = 0


async def to_wav16k(audio: bytes) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(audio)
    if proc.returncode != 0 or not out:
        raise AudioDecodeError(err.decode(errors="replace")[:200])
    return out


async def prepare(audio: bytes) -> Prepared:
    wav = await to_wav16k(audio)
    x, sr = read_wav(wav)
    secs = duration_s(x, sr)
    if secs > MAX_SECONDS:
        raise AudioTooLong(f"{secs:.1f}s > {MAX_SECONDS:.0f}s")
    return Prepared(wav, secs, estimate_snr_db(x, sr))


def keyterms_for(customers: list[Customer], limit: int = 200) -> list[str]:
    """Customer names bias both STT passes toward the shop's own vocabulary."""
    terms: list[str] = []
    for c in customers:
        for part in [c.name, *c.name.split(), *c.aliases]:
            if part and part not in terms:
                terms.append(part)
    return terms[:limit]


async def listen(client: GnaniClient, audio: bytes, customers: list[Customer],
                 previous_text: str | None = None, language_code: str = "hi-IN") -> Heard:
    prep = await prepare(audio)
    kt = keyterms_for(customers)
    # Pass A is essential: auth/credit failures propagate so the bot can degrade to typed input.
    a = await pass_a_prisma_v3(client, prep.wav, keyterms=kt, previous_text=previous_text,
                               language_code=language_code, priority=PRIORITY_INTERACTIVE)
    calls = 1
    retried: str | None = None
    if a.language in RETRY_LANGS and a.language != language_code:
        # Adaptive language handling: Prisma detected another Indian language -> re-listen in THAT language.
        try:
            a = await pass_a_prisma_v3(client, prep.wav, keyterms=kt, previous_text=previous_text,
                                       language_code=a.language, priority=PRIORITY_INTERACTIVE)
            retried = a.language or None
            language_code = retried or language_code
            calls += 1
        except (GnaniAuthError, GnaniCreditsExhausted):
            raise
        except GnaniError:
            pass
    b: STTResult | None = None
    b_err: str | None = None
    try:
        b = await pass_b_v25(client, prep.wav, bias_list=kt, language_code=language_code,
                             priority=PRIORITY_INTERACTIVE)
    except (GnaniAuthError, GnaniCreditsExhausted):
        raise
    except GnaniError as exc:
        b_err = type(exc).__name__
        log.warning("pass B failed: %s", b_err)
    calls += 1 if (b is not None or b_err) else 0
    c: STTResult | None = None
    if b is not None and parse_amount(a.transcript) != parse_amount(b.transcript):
        # Cost-aware escalation: a third, differently-formatted listen ONLY when the two ears disagree on money.
        try:
            raw = await client.stt_raw(prep.wav, language_code=language_code, model="prisma-v3.0", fmt="codemix",
                                       keyterms=kt, topic="kirana udhaar ledger: amounts, customer names",
                                       priority=PRIORITY_INTERACTIVE)
            c = parse_stt(raw, "prisma-v3.0/codemix")
            calls += 1
        except (GnaniAuthError, GnaniCreditsExhausted):
            raise
        except GnaniError as exc:
            log.warning("pass C failed: %s", type(exc).__name__)
    d: STTResult | None = None
    if customers and parse_amount(a.transcript) is not None and resolve(a.transcript, customers).status == "none":
        # Cost-aware name rescue: money was said but NO customer was recognised (e.g. the name came out in Devanagari or odd spelling).
        # Ask Prisma for a Roman-script transliteration, which our fuzzy name matcher handles best.
        try:
            raw = await client.stt_raw(prep.wav, language_code=language_code, model="prisma-v3.0", fmt="transliterate",
                                       keyterms=kt, topic="kirana udhaar ledger: customer names", priority=PRIORITY_INTERACTIVE)
            d = parse_stt(raw, "prisma-v3.0/transliterate")
            calls += 1
        except (GnaniAuthError, GnaniCreditsExhausted):
            raise
        except GnaniError as exc:
            log.warning("pass D failed: %s", type(exc).__name__)
    return Heard(a, b, prep, b_err, c, retried, d=d, calls=calls)


MAX_TOTAL_SECONDS = 90.0


@dataclass
class LongHeard:
    a_text: str
    b_text: str | None            # None if ANY chunk's second pass failed (a partial second ear proves nothing)
    seconds: float
    snr_db: float
    chunks: int
    calls: int
    language: str | None = None


async def listen_long(client: GnaniClient, audio: bytes, customers: list[Customer],
                      language_code: str = "hi-IN", max_total: float = MAX_TOTAL_SECONDS) -> LongHeard:
    """End-of-day dictation: cut at silences, one Gnani request at a time, chain chunks via previous_text."""
    from .chunker import split_wav

    wav = await to_wav16k(audio)
    x, sr = read_wav(wav)
    secs = duration_s(x, sr)
    if secs > max_total:
        raise AudioTooLong(f"{secs:.1f}s > {max_total:.0f}s")
    kt = keyterms_for(customers)
    a_parts: list[str] = []
    b_parts: list[str] = []
    b_ok, calls, lang = True, 0, None
    prev: str | None = None
    for piece in split_wav(wav):
        a = await pass_a_prisma_v3(client, piece, keyterms=kt, previous_text=prev, language_code=language_code,
                                   priority=PRIORITY_INTERACTIVE)
        calls += 1
        lang = lang or a.language
        a_parts.append(a.transcript.strip())
        prev = a.transcript[-300:] or prev
        if b_ok:
            try:
                b = await pass_b_v25(client, piece, bias_list=kt, language_code=language_code,
                                     priority=PRIORITY_INTERACTIVE)
                b_parts.append(b.transcript.strip())
            except (GnaniAuthError, GnaniCreditsExhausted):
                raise
            except GnaniError:
                b_ok = False
            calls += 1
    # ". " marks the cut so the batch splitter treats chunk borders as entry borders
    return LongHeard(". ".join(p for p in a_parts if p), ". ".join(b_parts) if b_ok else None,
                     secs, estimate_snr_db(x, sr), len(a_parts), calls, lang)
