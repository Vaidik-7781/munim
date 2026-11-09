# Munim Edge Mic (optional hardware)

| Item | Value |
|---|---|
| Board | ESP32-S3 (PSRAM) + I2S MEMS mic (INMP441 class) + push-button + mute switch |
| Cost | Parts roughly ₹700–1,200 at hobby quantities; **the ₹500 figure is not reachable without volume, a custom PCB and no wake-word silicon licence** — verify with a quote |
| Firmware | `firmware/munim_edge_mic/munim_edge_mic.ino` — reference only, **not field-tested** |
| Wake word | **Not included.** A stub + push-button fallback ships; "Munim ji" needs a trained model |
| Behaviour | Wake → ≤8 s clip → signed HTTPS POST → server transcribes, keeps only ledger-shaped speech after "Munim ji", **owner taps to confirm**. Never writes silently |
| Not built | Continuous cloud streaming (cost, DPDP exposure, and it breaks the credit budget) |

## Shop signage (DPDP-style notice — have counsel review)
"Yahan awaaz-khata mic hai. Yeh sirf 'Munim ji' bolne ke baad ki baat sunta hai. Awaaz save nahi hoti. Mic band karne ka switch yahan hai."

## Not verified
Wake-word accuracy in a noisy shop · TLS pinning · power/thermal in a counter enclosure · legal sufficiency of the notice · real BOM cost.
