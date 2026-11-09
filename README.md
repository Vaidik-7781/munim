# MUNIM — a voice-native udhaar ledger that asks instead of guessing

Kirana shopkeepers speak their khata ("Sharma ji ko dedh sau udhaar likh do"). Munim turns that into a
tamper-evident ledger on Telegram, built on **Gnani Prisma v3.0 / v2.5 (STT)** and **Timbre v3.0 (TTS)**.
The design rule: **an AI may propose, but code computes money, two ears must agree, and doubt becomes a question.**

## What is built (all offline-tested, 539 tests)
| Module | What it does |
|---|---|
| `gnani/` | Async client: one request in flight, priority queue, retries (429/5xx), 402/401 handling, credit meter, STT dual-pass presets, TTS speaker (digits refused, cache, Timbre-v3 voices only) |
| `listen/` | OGG→16 kHz WAV, 30 s limit, SNR risk heuristic, two independent transcripts |
| `scribe/` | Hindi/Hinglish/Devanagari number parser (dedh, dhai, sava, paune, saadhe, sau/hazaar/lakh/crore, "2k"), name resolver (ask on ties: "kaun sa Sharma?"), intent rules |
| `audit/` | Flags: amount/intent/name disagreement, scale risk (×10ⁿ), low SNR, short clip, outlier, duplicate, multi-amount, ambiguous/missing name, language switch, missing 2nd pass → **BLOCK**; clean → write with undo |
| `ledger/` | SHA-256 hash-chained append-only SQLite; integer paise; undo = reversal entry; disputes; verify CLI; erasure without breaking the chain; pseudonymous refs (no names in chain) |
| `govern/` | Reminder rules: consent, 08:00–19:00 IST, ≥72 h gap, ≤3/30 days, pause/opt-out/dispute, tone linter, AI disclosure, one-tap shopkeeper approval |
| `public/` | "Break Munim" sandbox: per-user + global caps, kill switch, PII redaction, decision trace |
| `bench/` | Noise Shield: 30-utterance truth set, 4 degradation conditions, bootstrap CIs, silent-error metric |
| `listen/` (v2) | **Cost-aware 3rd ear**: a third STT pass (`format=codemix`, customer-name keyterms) runs *only* when passes A and B disagree on the amount; **language re-listen** for Tamil/Telugu/Kannada/Malayalam/Marathi/Punjabi/Bengali/Gujarati when detected language differs |
| `insights/` | Deterministic analytics: FIFO ageing, explainable risk tiers, anomaly (MAD), promise-to-pay tracking, suggested credit limit, 14-day collections forecast (+ `bench/backtest_forecast.py`) |
| `agent/` | **Agentic layer**: planner (rules, or Groq-LLM with validated JSON steps and fallback to rules) → tool runner (read/propose-only tools, step cap) → Critic; reminder drafts land in a persisted **approval queue** (chained as `agent_action` / `agent_decision`); Thompson-sampling bandit picks the reminder time slot |
| `llm/`, `scribe/llm_spans.py` | Optional LLM *third opinion on spans* (off by default, `MUNIM_LLM_SPANS=1`). Our parser recomputes the amount; disagreement ⇒ ask |
| `service.py` | Conversation memory ("us ko 200 aur"), promise-to-pay capture ("kal dunga"), customer-message triage (`/from`: opt-out/dispute apply instantly, money needs confirmation), English/Hindi reminders by customer language, per-reminder Timbre language + speed |
| `listen/chunker.py` + `listen_long` | **Long dictation (≤90 s)**: cut at the quietest moment near each 28 s limit, one Gnani request at a time, chunks chained with Gnani's `previous_text`; both ears run per chunk |
| `scribe/batch.py` + `service.on_batch` | **Batch entry** — "Sharma ko sau udhaar, Verma ne paanch sau diye…": every line audited separately; ear B must independently produce the same (customer, amount, intent); clean lines go to ONE review list (single tap), flagged lines are held back with reasons; udhaar-vs-jama is never guessed; batch undo reverses only that batch |
| `scribe/upi.py` + `service.on_upi` | **UPI reconciliation** — paste a bank/UPI/PhonePe/GPay credit SMS → proposed payment, payer fuzzy-matched, confirm tap; debits refused; **de-duplicated by a keyed pseudonym of the UPI reference** (raw reference never enters the chain) |
| `agent/qa.py` | **/ask agent** — LLM (optional) only routes to a read-only query tool; customer names are replaced by `C<id>` before the LLM sees the question; invented customers rejected; every number comes from the ledger; rules fallback |
| `insights/statement.py` | **/statement** — customer-facing summary (text + Timbre voice note, Hindi `hi-en` or English `en-IN` by customer language, amounts as words, AI disclosed); refused for erased / opted-out / disputed accounts and, unless `asked`, without consent |
| `tenancy/` | **Multi-shop**: registry (shop + role per chat), one ledger file per shop with a derived HMAC secret, owner/staff/customer roles (deny-by-default), per-shop Gnani quota inside a global budget, signup cap |
| `portal.py` | **Customer portal**: invite deep-link = the customer's *own* consent; ask balance (text or voice, replied in their language via Timbre), say "galat" (dispute) or "ruko" (stop) — both instant; promises / payment claims go to the owner as one-tap questions |
| `insights/staff_audit.py` | Helper activity audit: reversal rate, after-hours entries, round-number payments (questions, not accusations) |
| `ml/` | **Learned repayment model** (numpy logistic regression, fixed bounded features, explainable) + **weights-only federated averaging** across shops; `/score` |
| `govern/genreminder.py` | **GenAI reminders with guardrails**: LLM rewords/translates *templates* (8 Indic languages); name/shop/amount filled by code after generation; accepted only if tokens, AI disclosure, stop/wrong words and tone lint pass, else template |
| `ingest/` | **Onboarding**: CSV import (exact decimal parse) and **paper-khata photo scan** (vision LLM, opt-in, every row re-parsed by our code, one review list); voice onboarding "naye customer: …" |
| `insights/dispute_pack.py` | **Dispute evidence pack** (text + PDF): provenance per entry, weak-evidence list, chain verification |
| `pay/upi_link.py` | UPI pay link + QR (`/setvpa`, `/paylink`); payment SMS paste closes the loop; refused while disputed |
| `books/payables.py` | **Supplier payables** (`/dena`, `/lena`): what the shop owes vs is owed (net position) |
| `listen/` (name rescue) | Gnani `format=transliterate` pass only when money was said but no customer recognised; rescued names are always confirmed |
| `ops/`, `api/` | `/export` backup (verifiable without secrets) + safe restore; Prometheus metrics (no PII); **REST API** (per-shop key, rate-limited) so WhatsApp/web/IVR gateways can reuse the same brain |
| `data/real/`, `bench/real/` | Real-data pipeline (see below) |

### Telegram commands
`/newshop` `/invite` `/staff` `/stats` `/score` `/remind` `/dispute` `/paylink` `/setvpa` `/dena` `/lena` `/import` `/scan` `/export` · `/ask <sawaal>` · `/statement naam [asked]` · `/plan` agent proposes today's collections · `/briefing` (also pushed daily 08:30 IST) · `/risk [naam]` · `/forecast` · `/anomaly` · `/limit naam 5000` · `/from naam` (next message is the customer's) · `/lang naam en|hi` · `/voices`, `/voice Naam` (Timbre v3.0) · `/hisaab` · `/add` · `/consent` · `/break` (public sandbox)

## v4 additions (sandbox adapters; see `docs/V4_FEATURES.md`)
ONDC storefront from voice inventory · e₹/DBT consented mandates · pincode group-buying pool · micro-insurance · cash-flow passport + OCEN/AA lending (sandbox) · opt-in collections negotiator · reconciliation agent + SMS forwarder · supplier-invoice OCR · edge mic (`edge/`) · WhatsApp router · streaming STT / live call agent · consent-gated voice clone · Evon provider switch · PWA at `/app`.
Commands: `/stock` `/price` `/count` `/ondc` `/pincode` `/mandate` `/insure` `/pool` `/passport` `/loan` `/policy` `/recon` `/edge` `/smsdev` `/invoice` `/aa`.
**None of the v4 external rails has been called live.** Refused by design: "zero default risk", continuous cloud listening, silent ledger writes, LLM-driven distributor negotiation, hidden customer fees.

## Docs
`docs/EDGE.md` · `docs/V4_FEATURES.md` ·`docs/ARCHITECTURE.md` (diagram + where Gnani is used) · `docs/SCALING.md` (measured throughput, scaling path) · `docs/THREAT_MODEL.md` · `DECISIONS.md` · `METHODS.md`

## Run it
```bash
pip install -e .[dev]
make test                      # 539 tests, no network, no keys
python data/seed.py            # 25 synthetic customers, 90 days
python scripts/demo_cli.py     # talk to the brain with typed "transcripts" (no keys needed)
```
Live (needs your keys in `.env`, see `.env.example`):
```bash
python scripts/g0_check.py --yes   # ~6 real API calls: credits/call, TTS pronunciation, OGG, Odia, STT round-trip
python -m munim.app                # Telegram bot, long polling
python bench/run_gnani.py --yes    # ≈240 STT calls — check g0 cost first
python bench/analyze.py
python -m munim.ledger.chain data/munim.db   # prove the ledger is untampered
```

## Real data (run on your machine)
```bash
pip install datasets soundfile kagglehub pandas
python data/real/fetch_hf.py --source fleurs --n 60          # real Hindi speech with numbers (Kathbath needs HF_TOKEN)
python data/real/fetch_kaggle.py                              # BigBasket prices + Indian names
python data/real/build_realistic_ledger.py --products PATH.csv   # ledger with REAL prices, MODELLED behaviour
python bench/real/run_real.py --yes && python bench/real/analyze_real.py
python bench/backtest_forecast.py data/munim_real.db
```
Real: item prices, name vocabulary, speech clips. **Modelled:** who buys what and repayment behaviour — no public dataset of real kirana udhaar exists, so the forecast backtest (model Brier 0.136 vs 0.246 constant baseline; MAPE 0.53 vs 0.91 naive on the simulated ledger) validates the pipeline and its leakage-free evaluation, **not** real-world accuracy.

## Benchmark results (measured 9 Nov 2026, real Gnani API)

**Setup.** 30 spoken udhaar utterances (amounts include dedh / paune / sava / lakh forms; 5 of them are above Rs 10,000), synthesized with gTTS and played through 4 audio conditions (C0 clean, C1 10 dB noise, C2 0 dB noise, C3 8 kHz telephone codec plus 10 dB noise; see `bench/make_noise.py`). Every clip went to Gnani Prisma v3.0 and v2.5: 241 real STT calls. One shared parser reads every transcript, so no system gets extra post-processing.
Reproduce: `python bench/run_gnani.py --yes --limit 30` then `python bench/analyze.py`.

**Metric that matters: silent error** = the system wrote a wrong amount without asking. 4 conditions x 30 utterances = 120 trials per system.

| System | Silent wrong writes | Rate | After parser fixes |
|---|---|---|---|
| **MUNIM dual-ear** (asks when the ears disagree) | **0 / 120** | 0% (95% upper bound about 2.5%) | 0 / 120 |
| Gnani v3 alone | 4 / 120 | 3.3% | 4 / 120 |
| Gnani v2.5 alone | 13 / 120 | 10.8% | 2 / 120 |

MUNIM never wrote a wrong amount, including on the clips where a single ear did (v3 read Rs 175 as Rs 1,750, Rs 125 as Rs 1,250, Rs 2,500 as Rs 2,000).

**Asking is not free.** Share of the 30 utterances per condition that MUNIM did not auto-write:

| Condition | Auto-written | Asked: large first-time amount (policy) | Asked: other (ears disagree, name unclear, bad audio) |
|---|---|---|---|
| C0 | 22 | 5 | 3 |
| C1 | 23 | 5 | 2 |
| C2 | 14 | 4 | 12 |
| C3 | 16 | 5 | 9 |

Policy asks are by design: a first-time customer above Rs 10,000 gets a confirmation, and this test set deliberately includes lakh-scale amounts. In the noisiest conditions MUNIM asks about half the time; each ask is one button tap instead of a wrong ledger entry.

**Caveats (read these).**
- The audio is synthetic (gTTS plus synthetic noise), not real shopkeepers. The same 30 clips are reused across conditions, so trials are not independent. Real-speech numbers need `bench/real/`.
- Three fixes were made after seeing run 1 on this same data, so the "after" column is not a blind test: v2.5 writes "paune do sau" as "paune Rs200" and "saade teen sau" as "03:30 Rs100", digits glued to Devanagari ("350rupaye"), and the Devanagari name for Singh did not match "Singh". Both tables are kept in `bench/results/table_before_fix.txt` and `table_after_fix.txt`. v2.5's big improvement comes from the shared parser, not from the model.
- At 120 trials, 0 vs 2 or 0 vs 4 silent errors is not statistically significant. The result shows that the design wrote no wrong amount here, not a proven margin over a single ear.
- If both ears make the same wrong hearing, MUNIM writes it (soft, with undo); the silent-error metric counts that.
- Cost, from the Gnani dashboard: about Rs 0.05 per STT request (23 requests = 1.09 credits), so the 241-call run cost about Rs 12. A dual-ear voice entry is 2 STT calls plus a TTS reply (estimated Rs 0.10-0.15).
- Odia (`or-IN`) was rejected by the API when tested; supported language codes include hi-IN, en-IN, bn-IN, gu-IN, kn-IN, ml-IN, as-IN.

## Safety claims and the tests that back them
- Undo never deletes: reversal entries (`test_balance_and_undo_by_reversal`).
- Edit/delete/re-chain of history is detected and pinpointed (`test_tamper_*`).
- No names/phone in the chain (`test_chain_has_no_names`); erasure keeps it verifiable.
- Digits never reach TTS (`test_speaker_rules`, `test_draft_is_linted_and_disclosed`).
- One Gnani request in flight, interactive before batch (`test_only_one_request_in_flight…`).
- Reminders refuse without consent / outside hours / too soon / disputed (`test_ledger_governor.py`).

## Honest limits
- **No real Gnani call has been made from this code yet.** Response field names, credit cost per call, `or-IN` support and OGG playback are verified by `scripts/g0_check.py`, not assumed.
- If both STT passes make the *same* wrong hearing, Munim writes it (soft, with undo). `test_munim_cannot_hide_when_both_passes_wrong_same_way` documents this; the benchmark reports it as silent-error rate.
- SNR is a heuristic, not lab SNR. Benchmark noise is synthetic. Name matching is tuned for Indian names in roman/Devanagari, not every script.
- Reminders are drafts the shopkeeper forwards; Munim does not message customers on its own.
- Optional LLM spans are **off by default**; when on, transcripts (which contain names) go to Groq — disclose this. The LLM only proposes spans; our parser computes the amount; any disagreement asks.
- The agent never sends anything to a customer: it drafts, the Governor checks, you approve with one tap, you forward.
- This build environment could **not reach huggingface.co or kaggle.com**, so the real-data scripts were tested on schema-faithful fixtures only. Kaggle column schemas are auto-detected and fail loudly if they differ. Kathbath is gated.
- **Learned repayment model is experimental and, on the simulated ledgers, shows no real skill**: AUC 0.43–0.63 across four shops, Brier no better than a constant rate (federated model on an unseen shop: AUC 0.56). The earlier hazard-based forecast (Brier 0.136 vs 0.246 constant) is the one to trust for now. The ML path is there to be trained on real ledgers; `/score` labels itself an estimate. No formal differential privacy.
- Indic-language reminders are LLM translations, **not native-reviewed**, and their voice notes speak the amount as English words (code-mixed); Timbre's handling of that is unverified until `g0_check.py`.
- Paper-khata scan depends on a vision model id (`MUNIM_VISION_MODEL`, default is a guess for Groq) that was not called from this sandbox.
- `scale_bench.py` (~550 msg/s on one sandbox core) excludes Gnani/Telegram latency. One chat belongs to one shop; the REST API key acts as that shop's owner.
- Batch lines, UPI parsing and the `/ask` router are tested on representative synthetic inputs, **not** a corpus of real SMS or real dictation; bank SMS formats vary — unparsed messages are refused, never guessed. Screenshots of UPI apps are not read (no OCR).
- STT `previous_text` chaining (long notes; the last transcript within 120 s) is used per Gnani's documented parameter, but its effect on accuracy is **unmeasured** — compare with/without in `bench/` before relying on it.
- `/statement` voice exists for Hindi and English only; other Timbre languages need native-reviewed scripts and number words.
- Date phrases ("kal", "parso", "agle somvar") are parsed with IST rules; ambiguous ones ask.
- Not yet verified live: Gnani response field names, credit cost per call, `or-IN`, TTS pronunciation of words-for-numbers in `hi-en`/`en-IN`. Run `scripts/g0_check.py` first.
