# v4 features — what each does and where it stops

| Feature | Command | Built | Refused / not claimed |
|---|---|---|---|
| ONDC storefront | `/pincode`, `/ondc`, `/stock`, `/price`, `/count` | Beckn/RET10 catalog, publish blockers (price, MRP, manufacturer, fresh count, 20% buffer), incremental sync, outbox, sandbox sink | No live ONDC call; no auto-publish without owner opt-in; stock from voice is "unverified" until counted |
| Programmable udhaar (e₹/DBT) | `/mandate` | customer-consented mandate, cap 25% (hard 40%), per-sweep/total caps, idempotent, payment recorded only after rail confirms | No real rail; **not "zero default risk"** — coverage is an estimate; DBT source is opt-in only |
| Group buying | `/pool` | HMAC-pseudonymous demand, K≥5 + 60% dominance rule, forecast, RFQ, deterministic evaluate, draft counters | No LLM contacting distributors; shops pay distributors directly; a pincode has hundreds, not 10,000, shops |
| Micro-insurance | `/insure` | illustrative rate card, ₹0.25–2 accrual on qualifying digital payments (default ₹0.50), claim drafts | Cover starts only when an insurer binds; customers never charged; rates not filed/approved |
| Passport / OCEN / AA | `/passport`, `/loan`, `/aa` | rule-based score, HMAC-signed shares, EMI/APR, affordability ≤30%, AA JSON parser | Sandbox lender; AA needs FIU registration; Munim never moves loan money |
| Autonomous collections | `/policy` | opt-in policy (≤14 d extension, ≤3 instalments, ≤₹3,000, ≥₹100), auto-accepts only in-policy promises | Never records payments; escalates the rest; AI disclosed |
| Reconciliation | `/recon`, `/smsdev` | fuzzy match, amount-only never HIGH, ambiguity → human | Nothing written without owner tap |
| Supplier invoices | `/invoice` | PDF/photo → validated lines → payables + stock | Photo needs `MUNIM_SCAN=1` |
| Live call agent | `telephony/call_agent.py` | state machine over streaming STT, disclosure first, 08–19 IST, stop word opts out | Telephony carrier/streaming format unverified; no digits to TTS |
| Voice clone | `gnani/voiceclone.py` | consent-gated (owner's own voice), AI-disclosure token required, revocable, stock-voice fallback | API paths are guesses |
| Sovereign LLM | `llm/evon.py` | provider switch via env; privacy mode reported | Evon endpoint unverified |
