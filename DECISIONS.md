# Decisions
| # | Decision | Why |
|---|---|---|
| 1 | Integer paise everywhere | No float money; round-trip property test over 6k random amounts |
| 2 | Code computes amounts; LLM (if ever added) only proposes spans | Hallucinated numbers are the worst failure for a ledger |
| 3 | Two STT passes (v3.0 + v2.5); disagreement ⇒ ask | Cheap independent evidence; free signal for the Auditor |
| 4 | Any flag ⇒ BLOCK; none ⇒ write + undo | Asymmetric cost: a question is cheap, a wrong debit loses trust |
| 5 | Undo = reversal entry, hash-chained | History is never rewritten; tampering is detectable |
| 6 | Chain stores HMAC pseudonyms, not names | Erasure/consent without breaking verification |
| 7 | Ties between names are never broken silently | "Kaun sa Sharma?" beats a 50% guess |
| 8 | Amounts converted to words before TTS | Digit strings are mispronounced; refuse digits in `Speaker` |
| 9 | One in-flight Gnani request, priority queue | API returns 429 beyond one active request; shopkeeper's voice beats batch jobs |
| 10 | Credit floor reserved for interactive calls | Benchmark must never starve the live demo |
| 11 | Reminders are drafts needing one-tap approval; linted; AI-disclosed | Recovery-conduct norms: no pressure, no shaming, consent first |
| 12 | stdlib `sqlite3` instead of SQLAlchemy | Fewer deps, explicit transactions for the chain |
| 13 | 'das' (10) vs surname Das: mask resolved customer tokens before number parsing | Real collision found while testing |
| 14 | Third STT pass only on amount disagreement | Cost-aware: spends a credit exactly where it can change the outcome |
| 15 | Agent tools are read/propose-only; Governor runs *inside* tools | The model cannot reach a side-effect without the safety layer |
| 16 | LLM planner output is schema-validated, tool-whitelisted, ids-only; fallback to rules | A bad/failed LLM call degrades to a rule plan, never to an unsafe action |
| 17 | Critic re-verifies each proposed action (balance, consent, window) | Agent grades its own plan with deterministic checks, not another LLM |
| 18 | Reminder-slot bandit (Thompson sampling) | Learns when customers pay after reminders; only chooses *time*, never *whether* |
| 19 | Customer replies are triaged: opt-out/dispute act instantly, money waits for confirmation | Actions that reduce Munim's activity are always safe; money is never auto-applied |
| 20 | LLM spans off by default | Transcripts contain names; sending them to a third party must be an explicit choice |
| 21 | Batch dictation is never written silently; one review tap; ear B must independently reproduce each line | A multi-entry note multiplies the cost of one bad hearing, so the human confirm is the gate |
| 22 | Intent (udhaar vs jama) is inherited across batch lines only if every explicit line agrees | Direction of money is the costliest thing to guess |
| 23 | UPI de-dup uses a keyed pseudonym of the reference; reversed entries free the reference | Same SMS pasted twice must not double-credit; chain stays free of raw identifiers |
| 24 | `/ask` LLM sees `C<id>` placeholders, only picks a tool, cannot invent customers | Read-only + names never leave the machine + numbers never come from a model |
| 25 | Statements obey the reminder respect rules (opt-out, dispute, consent unless customer asked) | Customer-facing money messages must be as restrained as reminders |
| 26 | One SQLite file per shop; per-shop derived HMAC secret | A kirana is small; isolation and cheap backup beat a shared DB; pseudonyms never correlate across shops |
| 27 | Roles deny by default; staff cannot undo, set limits, grant consent or message customers | The riskiest actions stay with the owner |
| 28 | Customer invite link = the customer's own consent | Stronger and more auditable than the shopkeeper attesting it |
| 29 | Federated learning shares weights only, with fixed feature transforms | No rows leave a shop; parameter spaces stay compatible. Not formal DP |
| 30 | GenAI only rewords/translates fixed templates; facts inserted by code after generation | Fluent messages without letting a model touch money, names or tone rules |
| 31 | Paper-khata scan is opt-in, re-parsed by our code, and one human review gates the write | Handwritten numbers are the riskiest input; the photo leaves the device |
| 32 | Name rescue (`transliterate`) only when no customer was recognised, and always confirmed | Spend a credit only where it can change the outcome; never trust a rescued name silently |
| 33 | REST API reuses MunimService; no shortcut around audit/Governor | New channels must not create a weaker path |
| 34 | Ambient mic is wake-gated, ≤12 s, signed, and never writes silently | Continuous listening is a DPDP and cost liability; owner tap stays the gate |
| 35 | Mandates need the customer's own consent; rail confirmation precedes any payment entry | Money moves only on evidence from the rail; coverage is stated as an estimate, never a guarantee |
| 36 | Pool shares pseudonymous demand only, with K-min and dominance suppression | Small pincodes can re-identify a shop; suppress rather than leak |
| 37 | Insurance premium is accrued from shop-side micro-fees; customers never charged; cover only after insurer binds | No hidden customer fees; no cover claimed that was not bound |
| 38 | Negotiator is opt-in, bounded by Policy, never records payments | Autonomy limited to scheduling promises inside owner-set bounds |
| 39 | Reconciliation: amount-only match is never HIGH; ambiguity goes to a human | Wrong credit attribution is the costliest silent error |
| 40 | ONDC publish blockers + owner opt-in; stock from voice is unverified until counted | Never list stock we cannot stand behind |
| 41 | All external rails (ONDC, e₹, insurer, lender, AA, WhatsApp, streaming, voice-clone, Evon) sit behind adapters with sandbox defaults | Nothing unverified can touch real money or customers by default |
