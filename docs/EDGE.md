# Edge mic, SMS forwarder and channels

| Path | Auth | What the server does |
|---|---|---|
| `POST /v1/edge/clip?wake_ms=` | per-device HMAC, ±60 s skew, nonce replay block, 30/min | requires `wake_ms`, ≤12 s WAV; transcribes with both ears; `segregate()` drops anything without "Munim ji" + ledger intent; accepted → owner confirm card (`ambient_edge` flag forces a confirm; never auto-written); audio never stored |
| `POST /v1/sms` | per-device HMAC | keeps credit SMS only (OTP/debit dropped); matched to a customer → approvals queue, written only after the owner taps |
| `GET/POST /v1/whatsapp` | Meta `X-Hub-Signature-256`, message-id replay set | link flow `link CODE`; same MunimService path; buttons → WhatsApp interactive |
| `GET /app`, `/v1/dashboard/*` | API key | zero-build PWA; no `innerHTML`/`eval`, no external scripts |

Enable: `MUNIM_EDGE=1`, `MUNIM_SMS=1`, `MUNIM_WA_APP_SECRET` (+ `MUNIM_WA_TOKEN`, `MUNIM_WA_PHONE_ID`, `MUNIM_WA_VERIFY_TOKEN`). Provision devices from Telegram: `/edge provision`, `/smsdev`.

Unverified: WhatsApp Cloud API payload details, Gnani streaming wire format (`gnani/streaming.py`, configurable), voice-clone endpoints (`GNANI_VC_PATH`), Evon endpoint (`MUNIM_LLM_BASE_URL`).
