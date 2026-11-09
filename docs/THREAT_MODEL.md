# Threat model (short)

| Threat | Mitigation | Residual risk |
|---|---|---|
| STT mishears an amount/name | Two independent ears + audit; any doubt becomes a question; 3rd ear on disagreement | Both ears wrong in the same way → soft write with 30 s undo (measured as *silent error* in the benchmark) |
| LLM invents a number or name | LLM never writes money; outputs validated; names replaced by `C<id>` in `/ask`; token-checked rewrites | A valid-looking but unhelpful rewrite is still possible (owner approves) |
| Helper (munshi) fraud | Role limits, `by` label on every entry, `/stats` staff audit flags (questions, not verdicts) | Collusion; flags are statistical |
| Customer disputes | `/dispute` pack: provenance, weak-evidence list, chain verification | Evidence ≠ truth; conversation still required |
| Tampering with history | Hash chain, `/export` verifiable offline | Whole-DB replacement by someone with disk access |
| Harassing reminders | Governor (consent, hours, 72 h gap, 3/30 d, opt-out, dispute, promise) + tone linter + approval | Owner can still message outside the product |
| Stranger links to a shop | One-time hashed invite tokens with expiry; role bound at redeem | Token leaked before use |
| Abuse of signup / API | Daily signup cap; per-key rate limit; per-shop Gnani quota; kill switch for public demo | Needs real WAF/CDN in production |
| Photo of paper khata leaves device | Opt-in (`MUNIM_SCAN=1`); every row human-reviewed; our parser re-validates numbers | Provider-side retention is the provider's policy |
| Ambient mic records bystanders | Wake-word gate, ≤12 s clips, transcript segregation, audio never stored, hardware mute, signage | Wake-word stub until a model is trained; legal notice needs review |
| Forged device uploads (edge/SMS) | Per-device HMAC, skew limit, nonce replay block, rate limit, revocation | Device secret theft from flash |
| Mandate over-sweep or non-consensual sweep | Customer self-consent, caps, idempotent rail_ref, revoke/STOP, chain-logged lifecycle | Rail-side bugs; real rail unverified |
| Pool re-identifies a shop | HMAC pseudonyms, K≥5, 60% dominance suppression | Colluding shops in a tiny pincode |
| Insurance mis-selling | Cover active only after bind; narrate() rejects invented numbers/promises; illustrative rates labelled | Rates not regulator-approved |
| Forged WhatsApp/rail webhooks | Signature checks (401 on failure), replay set | Secret leakage |
| LLM negotiator over-commits | Rule classifier, Policy bounds, no payment writes, escalation | Policy set too loose by owner |
