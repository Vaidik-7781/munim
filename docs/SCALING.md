# Scaling notes (what is true today, and the path beyond)

**Measured here** (`python bench/scale_bench.py 200 40`, one sandbox core): 200 shops, 8,000 typed entries through
parse → audit → hash-chain write ≈ 550 messages/second, all sampled chains verified. This excludes Gnani latency and
Telegram limits, which dominate in practice; treat it as "the Python/SQLite core is not the bottleneck".

| Concern | Today | Next step if needed |
|---|---|---|
| Tenancy | One SQLite file per shop + registry DB; per-shop derived HMAC secret | Shard shop files across volumes by shop-id prefix |
| Concurrency | Single process; SQLite WAL; one Gnani request in flight per client | Run N workers, each owning a shard; Telegram webhooks instead of polling |
| Gnani budget | Per-shop + global daily call quota (`tenancy/quota.py`) | Queue per shop with priority (interactive > batch) already in the client |
| Channels | Telegram + REST (`api/server.py`) | WhatsApp/IVR gateway talks to the REST API |
| ML | Weights-only federation (`ml/federated.py`) | Secure aggregation + formal DP accounting (not implemented) |
| Backups | `/export` zip, verifiable without secrets; `restore()` into an empty ledger | Scheduled encrypted off-box copies |
| Observability | `/metrics` (counts only, hashed shop ids), `/stats` | Alerting on quota burn and chain-verify failures |

**Not claimed:** formal privacy guarantees for the federated update, multi-region deployment, or any number for real traffic.
