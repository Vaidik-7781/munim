# Noise Shield — methods (pre-registered before any real run)

**Question.** When audio degrades, how often does a voice-ledger *silently* write a wrong amount, and does two-ear agreement + audit reduce it?

**Data.** 30 scripted utterances (`bench/utterances.yaml`): idioms (dedh/dhai/sava/paune/saadhe), plain, big (hazaar/lakh), long, small. Audio is synthesised with **gTTS (non-Gnani)** so Gnani is not graded on its own voice. *Limitation:* synthetic speech ≠ real shopkeepers; one voice.

**Conditions.** C0 clean · C1 10 dB · C2 0 dB · C3 8 kHz µ-law telephony + 10 dB. Noise = synthetic band-limited modulated babble (stand-in for market noise).

**Systems.** S1 Gnani Prisma v3.0 · S2 Gnani v2.5 · S3 faster-whisper small · MUNIM (S1 + S2 + Auditor).

**Metrics (all with 95% bootstrap CIs, 2000 resamples).**
- amount accuracy — parsed amount == truth, using the *same* parser for every system;
- **silent error** — system writes a wrong amount without asking (primary harm metric);
- ask rate — Munim declines to auto-write on amount-related flags.

**Protocol.** Tune thresholds only on a fixed 30% split (utterances u01–u09); report the other 70%. No threshold changes after viewing held-out results. Negative results are published as-is, including cases where both passes err identically.

**Not claimed.** Generalisation to real accents/noise, to other languages, or any statement about Gnani's models beyond this setup.

## Part 2 — real speech (added)
**Data.** Real Hindi clips whose reference transcripts contain numbers: Google FLEURS `hi_in` (public) and AI4Bharat Kathbath (gated). Read-speech, not shopkeepers in a market — an upper bound on field accuracy.
**Metric.** *Numeric match*: parse the reference and each hypothesis with the **same** parser (format-agnostic: "150", "एक सौ पचास", "dedh sau" all compare equal); report per-system accuracy and **MUNIM_dual silent error** (both ears agree on a wrong number) with bootstrap CIs.
**Third ear.** Reported separately: how often pass C is triggered, and how often it resolves vs. leaves a disagreement.
**Ledger data.** BigBasket prices (real) + simulated payers (modelled). The forecast is evaluated with rolling-origin, leakage-free cut-offs against a constant-rate and a naive baseline; results describe the pipeline, not real payer behaviour.
